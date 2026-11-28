"""Demo limits on the real Redis (P5B): the 11th ask in a minute is 429, the daily window, the spend cap
with reserve/settle (also under concurrency), and "demo limit reached" through the API."""

import threading

import pytest
import redis
from sqlalchemy import text

from apps.api.auth import hash_key
from apps.api.errors import AppError
from apps.api.limits import DEMO_LIMIT_MESSAGE, Limits, make_limits_redis
from apps.api.retrieval.pipeline import RetrievalDeps
from apps.worker.embedding import FakeEmbedder
from tests.conftest import make_test_app
from tests.fakes import FakeLLM, FakeStorage
from tests.helpers import auth

pytestmark = pytest.mark.integration

LIMITS_URL = "redis://localhost:6379/15"  # test-only DB; its pagecite:* keys are deleted around each test
LONG = "x" * 600  # over ASK_MAX_CHARS: rejected with 422 after being counted, before any model call


@pytest.fixture
def limits_redis():
    r = make_limits_redis(LIMITS_URL)
    try:
        r.ping()
    except redis.ConnectionError as exc:
        pytest.fail(f"cannot reach Redis ({exc}); run `docker compose up -d redis`", pytrace=False)
    # Never FLUSHDB here: in Redis 8 a FLUSHDB of ANY database drops every search index (FT.*), so it
    # would wipe the chunk index in DB 0. Delete this test's own keys instead.
    def clear():
        keys = list(r.scan_iter(match="pagecite:*", count=1000))
        if keys:
            r.unlink(*keys)

    clear()
    yield r
    clear()


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_eleventh_ask_in_a_minute_is_429_with_retry_after(limits_redis, clean_db, test_env, make_tenant):
    app = make_test_app(test_env, FakeStorage(), ask_rate_per_minute=10, ask_rate_per_day=20,
                        limits_redis_url=LIMITS_URL)
    _, key = make_tenant("demo")
    client = app.test_client()
    ip = {"X-Forwarded-For": "203.0.113.7", **auth(key)}
    for _ in range(10):
        resp = client.post("/ask", json={"question": LONG}, headers=ip)
        assert resp.status_code == 422 and resp.get_json()["error"]["code"] == "QUESTION_TOO_LONG"
    resp = client.post("/ask", json={"question": LONG}, headers=ip)
    assert resp.status_code == 429 and resp.get_json()["error"]["code"] == "RATE_LIMITED"
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    # another client is not affected; a spoofed first hop doesn't help (ProxyFix trusts the last hop)
    assert client.post("/ask", json={"question": LONG}, headers={"X-Forwarded-For": "198.51.100.1", **auth(key)}).status_code == 422
    spoofed = {"X-Forwarded-For": "198.51.100.2, 203.0.113.7", **auth(key)}
    assert client.post("/ask", json={"question": LONG}, headers=spoofed).status_code == 429
    app.extensions["pagecite"].engine.dispose()


def test_daily_window(limits_redis):
    clock = Clock(1_795_651_200.0)  # 00:00:00 UTC
    lim = Limits(limits_redis, per_minute=10, per_day=20, cap_usd=None, reserve_usd=0.01, clock=clock)
    for i in range(20):
        clock.t += 61  # a new minute each time: only the daily limit can bite
        lim.check_rate("203.0.113.7")
    clock.t += 61
    with pytest.raises(AppError) as exc:
        lim.check_rate("203.0.113.7")
    assert exc.value.code == "RATE_LIMITED"
    assert int(exc.value.headers["Retry-After"]) == 86_400 - int(clock.t) % 86_400
    clock.t += 86_400  # next UTC day
    lim.check_rate("203.0.113.7")


def test_reservations_never_overshoot_the_cap_under_concurrency(limits_redis):
    lim = Limits(limits_redis, None, None, cap_usd=0.03, reserve_usd=0.01)
    ok, refused = [], []

    def one():
        try:
            ok.append(lim.reserve())
        except AppError as exc:
            refused.append(exc.code)

    threads = [threading.Thread(target=one) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 3 and refused == ["DEMO_LIMIT_REACHED"] * 9
    assert lim.spent_today_usd() == pytest.approx(0.03)


def test_settle_replaces_the_reservation_or_keeps_it_when_the_cost_is_unknown(limits_redis):
    lim = Limits(limits_redis, None, None, cap_usd=0.05, reserve_usd=0.01)
    lim.settle(lim.reserve(), 0.004, complete=True)
    assert lim.spent_today_usd() == pytest.approx(0.004)
    lim.settle(lim.reserve(), 0.001, complete=False)  # client left mid-answer: the reservation stands
    assert lim.spent_today_usd() == pytest.approx(0.014)
    lim.settle(lim.reserve(), 0.02, complete=False)  # known part already above the reservation
    assert lim.spent_today_usd() == pytest.approx(0.034)


@pytest.fixture
def capped_app_for(test_env, s3_storage, redis_index, limits_redis):
    r, cfg = redis_index
    apps = []

    def _make(embed_model: str):
        def deps(state):
            return RetrievalDeps(settings=state.settings, sessions=state.sessions, redis=r, index=cfg,
                                 embedder=FakeEmbedder(dim=cfg.dim, model=embed_model),
                                 llm=FakeLLM(answer="The rate is 5.50 per cent [1]."))

        # A very low cap: the first ask's actual cost passes it, so the second one must be refused.
        app = make_test_app(test_env, s3_storage, retrieval=deps, s3_bucket=s3_storage.bucket,
                            daily_spend_cap_usd=0.000001, spend_reserve_usd=0.000001, limits_redis_url=LIMITS_URL)
        apps.append(app)
        return app

    yield _make
    for app in apps:
        app.extensions["pagecite"].engine.dispose()


def _rotate_key(clean_db, tenant_id: int) -> str:
    key = f"pc_test_key_{tenant_id}"
    with clean_db.begin() as conn:
        conn.execute(text("UPDATE tenants SET api_key_hash = :h WHERE id = :t"), {"h": hash_key(key), "t": tenant_id})
    return key


def _spent_micro(limits_redis) -> int:
    return int(limits_redis.get(next(iter(limits_redis.scan_iter("pagecite:spend:*")))))


def test_an_unpriced_call_keeps_the_reservation_and_never_breaks_the_stream(ingested, capped_app_for, clean_db,
                                                                            limits_redis):
    tenant_id, _ = ingested
    resp = capped_app_for("fake-embedder").test_client().post(
        "/ask", json={"question": "What is the daily allowance?"}, headers=auth(_rotate_key(clean_db, tenant_id)))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert "event: citations" in resp.get_data(as_text=True)
    assert _spent_micro(limits_redis) == 1  # unknown cost: the 1 micro-USD reservation stands


def test_very_low_spend_cap_gives_demo_limit_reached(ingested, capped_app_for, clean_db, limits_redis):
    tenant_id, _ = ingested
    key = _rotate_key(clean_db, tenant_id)
    client = capped_app_for("text-embedding-3-small").test_client()

    first = client.post("/ask", json={"question": "What is the daily allowance?"}, headers=auth(key))
    assert first.status_code == 200
    body = first.get_data(as_text=True)  # consume the stream: settle runs in its finally
    assert "event: citations" in body
    spent = _spent_micro(limits_redis)
    assert spent > 1  # settled to the real (priced) cost of the fake calls, above the 1 micro-USD cap

    second = client.post("/ask", json={"question": "What is the daily allowance?"}, headers=auth(key))
    assert second.status_code == 429
    err = second.get_json()["error"]
    assert err["code"] == "DEMO_LIMIT_REACHED" and err["message"] == DEMO_LIMIT_MESSAGE
    assert 1 <= int(second.headers["Retry-After"]) <= 86_400
