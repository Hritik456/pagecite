"""P3 failure policy: error classification, Retry-After, backoff, settings invariants, fault injection."""

import random
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import httpx2
import openai
import pytest
import redis
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from apps.api.config import Settings
from apps.api.errors import AppError
from apps.api.quotas import PDF
from apps.worker.embedding import FakeEmbedder, FaultyEmbedder
from apps.worker.errors import PermanentError, TransientError, classify, retry_after_from, retry_countdown
from apps.worker.parsing import OcrLimits, parse_document
from tests.unit.test_parsing import make_pdf

REQ = httpx2.Request("POST", "https://api.openai.com/v1/embeddings")


def status_error(cls, code: int, headers: dict | None = None):
    return cls("x", response=httpx2.Response(code, headers=headers or {}, request=REQ), body=None)


# --- classify -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind", "code"),
    [
        (status_error(openai.RateLimitError, 429, {"retry-after": "7"}), TransientError, "RATE_LIMITED"),
        (openai.APITimeoutError(request=REQ), TransientError, "UPSTREAM_UNAVAILABLE"),
        (openai.APIConnectionError(request=REQ), TransientError, "UPSTREAM_UNAVAILABLE"),
        (status_error(openai.InternalServerError, 503), TransientError, "UPSTREAM_5XX"),
        (status_error(openai.BadRequestError, 400), PermanentError, "UPSTREAM_REJECTED"),
        (status_error(openai.AuthenticationError, 401), PermanentError, "UPSTREAM_REJECTED"),
        (AppError("STORAGE_UNAVAILABLE", "s3 down", 503), TransientError, "STORAGE_UNAVAILABLE"),
        (AppError("OBJECT_NOT_FOUND", "gone", 404), PermanentError, "OBJECT_NOT_FOUND"),
        (OperationalError("SELECT 1", {}, Exception("gone away")), TransientError, "DATABASE_UNAVAILABLE"),
        (redis.ConnectionError("refused"), TransientError, "INDEX_UNAVAILABLE"),
        (ValueError("a bug"), PermanentError, "UNEXPECTED"),
        (PermanentError("CORRUPT", "bad pdf"), PermanentError, "CORRUPT"),
    ],
)
def test_classify(exc, kind, code):
    err = classify(exc)
    assert isinstance(err, kind) and err.code == code


def test_rate_limit_carries_retry_after():
    assert classify(status_error(openai.RateLimitError, 429, {"retry-after": "7"})).retry_after == 7.0


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "3"}, 3.0),
        ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),  # ms wins
        ({"retry-after": "soon"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_retry_after_from(headers, expected):
    assert retry_after_from(headers) == expected


def test_retry_after_http_date():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
    assert 15 <= retry_after_from({"retry-after": when}) <= 21


# --- retry_countdown ----------------------------------------------------------------------------


@pytest.mark.parametrize("retries", range(0, 9))
def test_countdown_is_full_jitter_within_the_exponential_ceiling(retries):
    rng = random.Random(retries)
    values = [retry_countdown(retries, None, 300, rng) for _ in range(200)]
    assert all(1 <= v <= min(300, 2**retries) or v == 1 for v in values)
    if 2**retries >= 8:
        assert len(set(values)) > 3  # jitter actually spreads retries


def test_retry_after_wins_over_a_shorter_backoff():
    assert retry_countdown(0, 12.0, 300, random.Random(1)) == 12


def test_countdown_is_capped_below_the_visibility_timeout():
    assert retry_countdown(20, None, 30, random.Random(1)) <= 30
    assert retry_countdown(0, 999.0, 30, random.Random(1)) == 30  # even a long Retry-After is capped


# --- settings invariants ------------------------------------------------------------------------

BASE = {"database_url": "mysql+pymysql://u:p@h/db", "s3_bucket": "b", "_env_file": None}


def test_default_timing_is_consistent():
    s = Settings(**BASE)
    assert s.ocr_budget_s < s.stage_soft_time_limit_s < s.stage_time_limit_s < s.broker_visibility_timeout_s
    assert s.retry_backoff_max_s < s.broker_visibility_timeout_s


@pytest.mark.parametrize(
    "overrides",
    [
        {"broker_visibility_timeout_s": 1200},  # == soft limit
        {"stage_time_limit_s": 1100},  # hard < soft
        {"ocr_budget_s": 1300},  # OCR budget > soft limit
        {"retry_backoff_max_s": 1800},  # an ETA retry would outlive the visibility timeout
    ],
)
def test_inconsistent_timing_refuses_to_start(overrides):
    with pytest.raises(ValidationError):
        Settings(**BASE, **overrides)


def test_chaos_timing_is_valid():
    Settings(**BASE, app_env="chaos", broker_visibility_timeout_s=120, stage_soft_time_limit_s=60,
             stage_time_limit_s=90, ocr_budget_s=45, retry_backoff_max_s=30, fault_embed_429_prefix="fail-")


def test_production_refuses_fault_injection_setting():
    with pytest.raises(ValidationError, match="fault injection"):
        Settings(**BASE, app_env="production", fault_embed_429_prefix="fail-")


def test_production_refuses_any_fault_env_var(monkeypatch):
    monkeypatch.setenv("FAULT_SOMETHING_NEW", "1")
    with pytest.raises(ValidationError, match="FAULT_SOMETHING_NEW"):
        Settings(**BASE, app_env="production")


def test_production_without_faults_starts(monkeypatch):
    for key in [k for k in __import__("os").environ if k.startswith("FAULT_")]:
        monkeypatch.delenv(key)
    assert Settings(**BASE, app_env="production").fault_embed_429_prefix is None


def test_the_api_app_refuses_to_start_in_production_with_faults(monkeypatch):
    from apps.api import create_app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("FAULT_EMBED_429_PREFIX", "fail-")
    with pytest.raises(ValidationError):
        create_app()


# --- fault injection, page limit ----------------------------------------------------------------


def test_faulty_embedder_fails_only_version_one_of_matching_files():
    inner = FakeEmbedder(dim=8)
    faulty = FaultyEmbedder(inner, "fail-")
    with pytest.raises(openai.RateLimitError) as exc:
        faulty.for_document("fail-x.pdf", 1).embed(["a"])
    err = classify(exc.value)
    assert isinstance(err, TransientError) and err.retry_after == 1.0
    assert faulty.for_document("fail-x.pdf", 2) is inner  # a replay passes
    assert faulty.for_document("report.pdf", 1) is inner


def test_page_limit_is_checked_before_extraction():
    calls = []
    with pytest.raises(PermanentError) as exc:
        parse_document(make_pdf(["a"] * 3), PDF, lambda i, t: calls.append(1) or "",
                       OcrLimits(max_document_pages=2))
    assert exc.value.code == "PAGE_LIMIT" and calls == []
