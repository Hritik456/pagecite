"""Public demo limits on POST /ask (P5B, docs/adr/0005-public-demo-limits.md).

1. Per-IP rate limit: fixed windows rl:{ip}:m:{minute} and rl:{ip}:d:{utc-date}. One MULTI pipeline
   does INCR + EXPIRE for both, and every attempt counts (also ones later rejected as invalid), so
   hammering the endpoint with bad requests doesn't get around it. Over the limit: 429 RATE_LIMITED
   with Retry-After = seconds until the window ends. IPv6 clients are bucketed by /64.
2. Global daily spend cap: spend:{utc-date} in micro-dollars. Before any model call a Lua script
   reserves the worst-case cost of one request (SPEND_RESERVE_USD) and refuses when
   spent + reserve > cap: 429 DEMO_LIMIT_REACHED until UTC midnight. After the answer the
   reservation is replaced by the actual cost. If the actual cost is unknown (the client left before
   the answer's usage arrived) the reservation stands. Concurrent requests can't overshoot the cap.

Both fail closed: if Redis can't answer within 1 s, /ask is 503 LIMITS_UNAVAILABLE rather than
unmetered. They use their own Redis DB (LIMITS_REDIS_URL), apart from the index and the broker.
"""

import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import redis

from .errors import AppError

log = logging.getLogger("pagecite.limits")

PREFIX = "pagecite:"
DAY_TTL_S = 2 * 86_400
MINUTE_TTL_S = 120

DEMO_LIMIT_MESSAGE = "The demo has reached today's question limit. Please try again tomorrow."
RATE_MESSAGE = "Too many questions from your network. Please wait {wait} and try again."

# KEYS[1] spend key; ARGV[1] reserve (micro-USD), ARGV[2] cap (micro-USD), ARGV[3] ttl (s).
# Returns -1 when the reservation would pass the cap, else the new total.
_RESERVE_LUA = """
local spent = tonumber(redis.call('GET', KEYS[1]) or '0')
local reserve = tonumber(ARGV[1])
if spent + reserve > tonumber(ARGV[2]) then
  return -1
end
local total = redis.call('INCRBY', KEYS[1], reserve)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
return total
"""


def micro(usd: float) -> int:
    return int(round(usd * 1_000_000))


def client_bucket(ip: str | None) -> str:
    """The rate-limit identity of a client: the IPv4 address, or the IPv6 /64 it is in."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return "unknown"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{addr}/64", strict=False))
    return str(addr)


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d")


def seconds_to_minute_end(now: float) -> int:
    return max(1, 60 - int(now) % 60)


def seconds_to_day_end(now: float) -> int:
    return max(1, 86_400 - int(now) % 86_400)


def human_wait(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{round(seconds / 3600)} hours"


@dataclass(frozen=True)
class Reservation:
    key: str
    reserved: int  # micro-USD


def _unavailable(exc: Exception) -> AppError:
    err = AppError("LIMITS_UNAVAILABLE", "the demo limits are unavailable; try again later", 503)
    err.__cause__ = exc
    return err


class Limits:
    def __init__(
        self, r: redis.Redis, per_minute: int | None, per_day: int | None, cap_usd: float | None,
        reserve_usd: float, clock: Callable[[], float] = time.time,
    ):
        self.r, self.per_minute, self.per_day = r, per_minute, per_day
        self.cap = None if cap_usd is None else micro(cap_usd)
        self.reserve_micro = micro(reserve_usd)
        self.clock = clock
        self._reserve = r.register_script(_RESERVE_LUA)

    def check_rate(self, ip: str | None) -> None:
        if self.per_minute is None and self.per_day is None:
            return
        now = self.clock()
        who = client_bucket(ip)
        m_key = f"{PREFIX}rl:{who}:m:{int(now) // 60}"
        d_key = f"{PREFIX}rl:{who}:d:{utc_day(now)}"
        try:
            with self.r.pipeline(transaction=True) as pipe:
                pipe.incr(m_key).expire(m_key, MINUTE_TTL_S).incr(d_key).expire(d_key, DAY_TTL_S)
                n_minute, _, n_day, _ = pipe.execute()
        except redis.RedisError as exc:
            raise _unavailable(exc) from exc
        if self.per_minute is not None and n_minute > self.per_minute:
            wait = seconds_to_minute_end(now)
        elif self.per_day is not None and n_day > self.per_day:
            wait = seconds_to_day_end(now)
        else:
            return
        raise AppError("RATE_LIMITED", RATE_MESSAGE.format(wait=human_wait(wait)), 429, {"Retry-After": str(wait)})

    def reserve(self) -> Reservation | None:
        if self.cap is None:
            return None
        now = self.clock()
        key = f"{PREFIX}spend:{utc_day(now)}"
        try:
            total = int(self._reserve(keys=[key], args=[self.reserve_micro, self.cap, DAY_TTL_S]))
        except redis.RedisError as exc:
            raise _unavailable(exc) from exc
        if total < 0:
            raise AppError("DEMO_LIMIT_REACHED", DEMO_LIMIT_MESSAGE, 429,
                           {"Retry-After": str(seconds_to_day_end(now))})
        return Reservation(key, self.reserve_micro)

    def settle(self, reservation: Reservation | None, actual_usd: float, complete: bool) -> None:
        """Replace the reservation by the actual cost. Unknown actual cost (complete=False): keep at least
        the reservation. Best effort: a failure here only leaves the (conservative) reservation."""
        if reservation is None:
            return
        actual = micro(actual_usd)
        delta = actual - reservation.reserved if complete else max(0, actual - reservation.reserved)
        if delta == 0:
            return
        try:
            self.r.incrby(reservation.key, delta)
        except redis.RedisError as exc:
            log.warning("spend_settle_failed type=%s delta_micro=%s", type(exc).__name__, delta)

    def spent_today_usd(self) -> float:
        value = self.r.get(f"{PREFIX}spend:{utc_day(self.clock())}")
        return int(value or 0) / 1_000_000


def make_limits_redis(url: str) -> redis.Redis:
    return redis.Redis.from_url(url, protocol=2, decode_responses=True, socket_timeout=1.0,
                                socket_connect_timeout=1.0, health_check_interval=30)


def limits_enabled(settings) -> bool:
    return any(v is not None for v in (settings.ask_rate_per_minute, settings.ask_rate_per_day,
                                       settings.daily_spend_cap_usd))


def build_limits(settings) -> "Limits | None":
    if not limits_enabled(settings):
        return None
    return Limits(make_limits_redis(settings.limits_redis_url), settings.ask_rate_per_minute,
                  settings.ask_rate_per_day, settings.daily_spend_cap_usd, settings.spend_reserve_usd)
