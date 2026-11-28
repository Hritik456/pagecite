"""Ask-only keys (V5, the public demo): read + POST /ask, everything else 403. Deny by default."""

import re

import pytest

from apps.api.auth import scope_allows
from apps.api.retrieval.pipeline import RetrievalDeps
from tests.conftest import make_test_app
from tests.fakes import FakeLLM
from tests.helpers import auth, file_spec, pdf_bytes


class BrokenEmbedder:
    """Fails at once, before any Redis search: proves /ask got past auth without OpenAI or Redis."""

    model = "broken"

    def embed_with_usage(self, texts):
        raise RuntimeError("no network in DB tests")


@pytest.fixture
def ask_app(clean_db, test_env, fake_storage):
    def deps(state):
        return RetrievalDeps(settings=state.settings, sessions=state.sessions, redis=None, index=None,
                             embedder=BrokenEmbedder(), llm=FakeLLM())

    app = make_test_app(test_env, fake_storage, retrieval=deps)
    yield app
    app.extensions["pagecite"].engine.dispose()


def new_ask_key(app, tenant_id: int) -> str:
    result = app.test_cli_runner().invoke(args=["create-ask-key", "--tenant", str(tenant_id)])
    assert result.exit_code == 0, result.output
    key = next(line.split("=", 1)[1] for line in result.output.splitlines() if line.startswith("ask_key="))
    assert key.startswith("pca_")
    return key


def mutating_rules(app):
    """Every (method, url) that is not a read, with integer placeholders filled in."""
    for rule in app.url_map.iter_rules():
        if rule.endpoint in {"static"}:
            continue
        url = re.sub(r"<(?:int:)?[^>]+>", "1", rule.rule)
        for method in sorted(rule.methods - {"GET", "HEAD", "OPTIONS"}):
            yield rule.endpoint, method, url


def test_ask_key_is_403_on_every_mutating_route_except_ask(ask_app, make_tenant):
    tenant_id, _ = make_tenant("demo")
    key = new_ask_key(ask_app, tenant_id)
    client = ask_app.test_client()
    checked = []
    for endpoint, method, url in mutating_rules(ask_app):
        resp = client.open(url, method=method, json={}, headers=auth(key))
        if endpoint == "ask.ask":
            assert resp.status_code not in (401, 403), resp.get_json()
            continue
        assert resp.status_code == 403, (endpoint, resp.get_json())
        assert resp.get_json()["error"]["code"] == "FORBIDDEN_FOR_KEY"
        checked.append(endpoint)
    # upload (create + complete), replay and cancel are the routes P5B names; anything added later is
    # covered by the deny-by-default rule, and this list makes a new mutating route visible here
    assert sorted(checked) == ["batches.cancel_batch", "batches.create_batch",
                               "documents.complete_upload", "documents.replay_document"]


def test_ask_key_can_read_and_reaches_the_ask_handler(ask_app, make_tenant):
    tenant_id, _ = make_tenant("demo")
    key = new_ask_key(ask_app, tenant_id)
    client = ask_app.test_client()
    assert client.get("/batches/999", headers=auth(key)).status_code == 404  # authenticated, not found
    assert client.get("/dead-letters", headers=auth(key)).status_code == 200
    resp = client.post("/ask", json={"question": "What is the repo rate?", "stream": False}, headers=auth(key))
    assert resp.status_code == 502 and resp.get_json()["error"]["code"] == "UPSTREAM_ERROR"  # past auth


def test_full_key_is_unaffected(ask_app, make_tenant):
    _, full_key = make_tenant("owner")
    resp = ask_app.test_client().post("/batches", json={"files": [file_spec("a.pdf", pdf_bytes())]},
                                      headers=auth(full_key))
    assert resp.status_code == 201, resp.get_json()


def test_ask_key_only_sees_its_own_tenant(ask_app, make_tenant):
    a, a_key = make_tenant("a")
    b, _ = make_tenant("b")
    batch_id = ask_app.test_client().post(
        "/batches", json={"files": [file_spec("a.pdf", pdf_bytes())]}, headers=auth(a_key)).get_json()["batchId"]
    b_ask = new_ask_key(ask_app, b)
    assert ask_app.test_client().get(f"/batches/{batch_id}", headers=auth(b_ask)).status_code == 404
    assert ask_app.test_client().get(f"/batches/{batch_id}", headers=auth(new_ask_key(ask_app, a))).status_code == 200


def test_revoked_ask_key_is_unknown(ask_app, make_tenant):
    tenant_id, _ = make_tenant("demo")
    key = new_ask_key(ask_app, tenant_id)
    result = ask_app.test_cli_runner().invoke(args=["revoke-ask-keys", "--tenant", str(tenant_id)])
    assert result.exit_code == 0 and "revoked=1" in result.output
    resp = ask_app.test_client().get("/dead-letters", headers=auth(key))
    assert resp.status_code == 401


def test_create_ask_key_for_a_missing_tenant_fails(ask_app):
    result = ask_app.test_cli_runner().invoke(args=["create-ask-key", "--tenant", "999999"])
    assert result.exit_code != 0 and "does not exist" in result.output


@pytest.mark.parametrize("scope,method,endpoint,ok", [
    ("full", "POST", "batches.create_batch", True),
    ("ask", "GET", "batches.get_batch", True),
    ("ask", "HEAD", "documents.get_document", True),
    ("ask", "POST", "ask.ask", True),
    ("ask", "POST", "batches.create_batch", False),
    ("ask", "DELETE", "documents.anything_added_later", False),
    ("unknown", "GET", "batches.get_batch", False),
])
def test_scope_allows_is_deny_by_default(scope, method, endpoint, ok):
    assert scope_allows(scope, method, endpoint) is ok


def test_question_length_is_capped_even_without_demo_limits(ask_app, make_tenant):
    _, key = make_tenant("owner")
    client = ask_app.test_client()
    too_long = client.post("/ask", json={"question": "a" * 501, "stream": False}, headers=auth(key))
    assert too_long.status_code == 422 and too_long.get_json()["error"]["code"] == "QUESTION_TOO_LONG"
    at_limit = client.post("/ask", json={"question": "a" * 500, "stream": False}, headers=auth(key))
    assert at_limit.status_code == 502  # accepted, then failed at the (broken) embedder: past validation
