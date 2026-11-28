"""Demo limits (P5B): client bucketing, window math, fail-closed when Redis is unreachable."""

import time

import pytest
import redis

from apps.api.errors import AppError
from apps.api.limits import (
    Limits,
    build_limits,
    client_bucket,
    human_wait,
    make_limits_redis,
    micro,
    seconds_to_day_end,
    seconds_to_minute_end,
    utc_day,
)
from apps.api.config import Settings


@pytest.mark.parametrize("ip,bucket", [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:aaaa:bbbb:1:2:3:4", "2001:db8:aaaa:bbbb::/64"),
    ("2001:db8:aaaa:bbbb:ffff::1", "2001:db8:aaaa:bbbb::/64"),  # same /64: same bucket
    ("::ffff:198.51.100.9", "198.51.100.9"),  # IPv4-mapped counts as the IPv4 client
    ("not-an-ip", "unknown"),
    (None, "unknown"),
])
def test_client_bucket(ip, bucket):
    assert client_bucket(ip) == bucket


def test_window_math():
    t = 1_795_651_230.4  # some UTC instant
    assert 1 <= seconds_to_minute_end(t) <= 60
    assert (int(t) + seconds_to_minute_end(t)) % 60 == 0
    assert (int(t) + seconds_to_day_end(t)) % 86_400 == 0
    assert utc_day(0) == "19700101"
    assert human_wait(30) == "30 seconds" and human_wait(600) == "10 minutes" and human_wait(7200) == "2 hours"
    assert micro(0.5) == 500_000 and micro(0.0000015) == 2


def test_limits_are_off_unless_configured():
    assert build_limits(Settings(database_url="x", s3_bucket="b")) is None
    assert build_limits(Settings(database_url="x", s3_bucket="b", ask_rate_per_minute=10)) is not None


def test_unreachable_redis_fails_closed_fast():
    lim = Limits(make_limits_redis("redis://127.0.0.1:1/0"), 10, 20, 0.5, 0.01)
    started = time.monotonic()
    for call in (lambda: lim.check_rate("203.0.113.7"), lim.reserve):
        with pytest.raises(AppError) as exc:
            call()
        assert exc.value.status == 503 and exc.value.code == "LIMITS_UNAVAILABLE"
    assert time.monotonic() - started < 5  # connect timeout 1 s, not a hang


def test_settle_failure_only_logs(caplog):
    lim = Limits(make_limits_redis("redis://127.0.0.1:1/0"), None, None, 0.5, 0.01)
    from apps.api.limits import Reservation

    lim.settle(Reservation("pagecite:spend:x", 10_000), 0.001, complete=True)  # no exception
    assert "spend_settle_failed" in caplog.text


def test_spend_cap_requires_a_priced_model_and_a_sane_reserve():
    with pytest.raises(ValueError, match="no price"):
        Settings(database_url="x", s3_bucket="b", daily_spend_cap_usd=0.5, openai_fast_model="gpt-9")
    with pytest.raises(ValueError, match="SPEND_RESERVE_USD"):
        Settings(database_url="x", s3_bucket="b", daily_spend_cap_usd=0.005, spend_reserve_usd=0.01)


def test_redis_errors_are_the_ones_caught():
    assert issubclass(redis.ConnectionError, redis.RedisError)
