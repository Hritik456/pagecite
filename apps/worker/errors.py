"""Stage error taxonomy (P3).

- TransientError: may succeed later (timeouts, 429, 5xx, S3/DB/Redis connectivity). Retried with
  exponential backoff + full jitter, honouring Retry-After, up to MAX_RETRIES; then dead-lettered.
- PermanentError: can never succeed for this input (corrupt, encrypted, over a limit, rejected by the
  API). Dead-lettered at once, no retry.
classify() maps library exceptions onto these; anything it does not recognise is permanent
(UNEXPECTED), so a bug is not retried five times.
"""

import email.utils
import random
import time


class PermanentError(Exception):
    """The input can never succeed (corrupt, encrypted, unsupported, no text, over a limit)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class TransientError(Exception):
    """A failure that may go away: retry later. `retry_after` (seconds) comes from the upstream, if any."""

    def __init__(self, code: str, message: str, retry_after: float | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retry_after = retry_after


def retry_after_from(headers) -> float | None:
    """Seconds to wait from `retry-after-ms`, or `retry-after` as seconds or an HTTP date."""
    if headers is None:
        return None
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:  # an HTTP date; anything malformed is ignored (plain backoff applies)
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())


def classify(exc: BaseException) -> TransientError | PermanentError:
    """Map an exception raised inside a stage onto the taxonomy."""
    if isinstance(exc, (TransientError, PermanentError)):
        return exc

    import openai
    import redis
    from sqlalchemy.exc import DisconnectionError, OperationalError

    from apps.api.errors import AppError

    name = type(exc).__name__
    if isinstance(exc, openai.RateLimitError):
        return TransientError("RATE_LIMITED", "upstream rate limit (429)", retry_after_from(exc.response.headers))
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):  # timeout is a connection error subclass
        return TransientError("UPSTREAM_UNAVAILABLE", f"upstream not reachable ({name})")
    if isinstance(exc, openai.InternalServerError):
        return TransientError("UPSTREAM_5XX", f"upstream server error ({exc.status_code})",
                              retry_after_from(exc.response.headers))
    if isinstance(exc, openai.APIStatusError):  # 400/401/403/404/422...: retrying will not help
        return PermanentError("UPSTREAM_REJECTED", f"upstream rejected the request ({exc.status_code})")
    if isinstance(exc, AppError):
        if exc.code == "STORAGE_UNAVAILABLE":
            return TransientError("STORAGE_UNAVAILABLE", exc.message)
        return PermanentError(exc.code, exc.message)
    if isinstance(exc, (OperationalError, DisconnectionError)):
        return TransientError("DATABASE_UNAVAILABLE", f"database not reachable ({name})")
    if isinstance(exc, (redis.ConnectionError, redis.TimeoutError)):
        return TransientError("INDEX_UNAVAILABLE", f"redis not reachable ({name})")
    return PermanentError("UNEXPECTED", f"{name}: {str(exc)[:300]}")


def retry_countdown(
    retries: int, retry_after: float | None, backoff_max: int, rng: random.Random | None = None
) -> int:
    """Seconds before retry number `retries + 1`.

    Full-jitter exponential backoff (uniform in [0, min(backoff_max, 2**retries)]), like Celery's
    retry_backoff + retry_jitter, but never earlier than the upstream's Retry-After. Capped at
    backoff_max, which is kept below the broker visibility timeout (an ETA task is held unacked).
    """
    rng = rng or random
    ceiling = min(backoff_max, 2 ** retries)
    delay = rng.uniform(0, ceiling)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return int(min(backoff_max, max(1, round(delay))))
