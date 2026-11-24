"""P4 retrieval + /ask against the real index (MySQL test DB, MinIO test bucket, Redis test index).

Embeddings are FakeEmbedder vectors and the chat model is FakeLLM, so these tests check the
plumbing (tenant isolation, SSE contract, citations, abstain, token log, connection lifetime,
timeouts), not answer quality. Answer quality on real models is scripts/smoke_ask.py.
"""

import json
import logging
import re
import threading

import pytest
import requests

from apps.api.retrieval.llm import LLMTimeout
from apps.api.retrieval.pipeline import RetrievalConfig, RetrievalDeps, retrieve
from apps.api.retrieval.prompt import ABSTAIN, UNTRUSTED_RULE
from apps.api.retrieval.usage import UsageLog
from apps.api.search_index import drop_index
from apps.worker.embedding import FakeEmbedder
from apps.worker.relay import relay_once
from tests.conftest import make_test_app
from tests.fakes import FakeLLM
from tests.helpers import auth
from tests.integration.conftest import MANIFEST, upload_samples

pytestmark = pytest.mark.integration

CIRCULARS = "06-circulars.pdf"
INJECTION_PAGE = 8


@pytest.fixture
def llm():
    return FakeLLM(answer="Rs 2,400 per day in Category A cities [1]. Claims within 30 days [2][1]. Also [9].")


@pytest.fixture
def ask_app(test_env, s3_storage, redis_index, llm):
    """An app whose /ask uses the test index, FakeEmbedder and FakeLLM, on the app's own DB pool."""
    r, cfg = redis_index

    def deps(state):
        return RetrievalDeps(
            settings=state.settings, sessions=state.sessions, redis=r, index=cfg,
            embedder=FakeEmbedder(dim=cfg.dim), llm=llm,
        )

    app = make_test_app(test_env, s3_storage, retrieval=deps, s3_bucket=s3_storage.bucket)
    yield app
    app.extensions["pagecite"].engine.dispose()


@pytest.fixture
def tenant_a(ingested, make_tenant):
    tenant_id, ids = ingested
    return tenant_id, ids


@pytest.fixture
def key_for(clean_db):
    """Tests need a key for an already-created tenant: rotate it to a known one."""
    from apps.api.auth import hash_key
    from sqlalchemy import text

    def _key(tenant_id: int) -> str:
        key = f"pc_test_key_{tenant_id}"
        with clean_db.begin() as conn:
            conn.execute(text("UPDATE tenants SET api_key_hash = :h WHERE id = :t"), {"h": hash_key(key), "t": tenant_id})
        return key

    return _key


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def ask(app, key: str, question: str, stream: bool = True):
    return app.test_client().post("/ask", json={"question": question, "stream": stream}, headers=auth(key))


def deps_of(app) -> RetrievalDeps:
    return app.extensions["pagecite"].retrieval


# --- tenant isolation ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["vector_only", "hybrid", "hybrid_rerank"])
def test_isolation_tenant_b_never_sees_tenant_a_chunks(
    strategy, tenant_a, make_tenant, s3_app, worker_ctx, ask_app, db_scalar
):
    a_id, a_docs = tenant_a
    b_id, b_key = make_tenant("tenant-b", max_file_mb=5, max_batch_files=5)
    b_docs = upload_samples(s3_app.test_client(), b_key, only=["04-travel-rules.pdf"])
    with worker_ctx.sessions() as session:
        relay_once(session, worker_ctx.enqueue)
    phrase_a = next(m["phrase"] for m in MANIFEST if m["file"] == "01-leave-policy.pdf")

    cfg = RetrievalConfig(strategy=strategy, decompose=False)
    with ask_app.app_context():
        got_b = retrieve(deps_of(ask_app), b_id, f"What is the {phrase_a}?", cfg, UsageLog("t", b_id))
        got_a = retrieve(deps_of(ask_app), a_id, f"What is the {phrase_a}?", cfg, UsageLog("t", a_id))

    b_doc_ids = set(b_docs.values())
    assert got_b.blocks, "tenant B has documents, so something is retrieved"
    assert {blk.document_id for blk in got_b.blocks} <= b_doc_ids
    assert not {blk.document_id for blk in got_b.blocks} & set(a_docs.values())
    if strategy != "vector_only":  # the text half finds the exact document for its owner
        # (FakeEmbedder vectors are random, so RRF order is not asserted here; real ranking is smoke_ask)
        assert a_docs["01-leave-policy.pdf"] in {blk.document_id for blk in got_a.blocks}


def test_isolation_through_the_api(tenant_a, make_tenant, ask_app, llm):
    _a_id, _ = tenant_a
    _b_id, b_key = make_tenant("tenant-empty")
    phrase_a = MANIFEST[0]["phrase"]
    events = parse_sse(ask(ask_app, b_key, f"What is the {phrase_a}?").get_data(as_text=True))
    assert events[-1] == ("citations", {"citations": [], "abstained": True})
    assert llm.stream_calls == 0  # nothing retrieved for tenant B: abstain without a model call


# --- SSE contract, citations, abstain -----------------------------------------------------------


def blocks_in_prompt(user_prompt: str) -> dict[int, tuple[str, int]]:
    return {
        int(n): (title, int(page))
        for n, title, page in re.findall(r'<block n="(\d+)" title="([^"]+)" pages="p\.(\d+)', user_prompt)
    }


def test_sse_stream_tokens_then_one_citations_event(tenant_a, key_for, ask_app, llm, db_scalar):
    a_id, _ = tenant_a
    resp = ask(ask_app, key_for(a_id), "What daily allowance does circular FIAS/HR/2025-26/014 set?")
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["Cache-Control"] == "no-cache"

    events = parse_sse(resp.get_data(as_text=True))
    names = [name for name, _ in events]
    assert set(names[:-1]) == {"token"} and names[-1] == "citations"
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert answer == llm.answer

    citations = events[-1][1]
    assert citations["abstained"] is False
    assert [c["n"] for c in citations["citations"]] == [1, 2]  # [9] is out of range and dropped
    shown = blocks_in_prompt(llm.last_user)
    for c in citations["citations"]:
        title, page = shown[c["n"]]
        assert (c["title"], c["page"]) == (title, page)
        filename = db_scalar("SELECT filename FROM documents WHERE id = :d", d=c["document_id"])
        assert filename.rsplit(".", 1)[0] == title


def test_identifier_question_puts_the_exact_circular_first(tenant_a, key_for, ask_app, llm):
    a_id, _ = tenant_a
    ask(ask_app, key_for(a_id), "Under clause 4.2(b) of FIAS/HR/2025-26/041, how many days?").get_data()
    first_block = llm.last_user.split("</block>")[0]
    assert "FIAS/HR/2025-26/041" in first_block  # not its near-twin 014


def test_non_streaming_returns_the_same_content(tenant_a, key_for, ask_app, llm):
    a_id, _ = tenant_a
    body = ask(ask_app, key_for(a_id), "What daily allowance does FIAS/HR/2025-26/014 set?", stream=False).get_json()
    assert body["answer"] == llm.answer
    assert [c["n"] for c in body["citations"]] == [1, 2] and body["abstained"] is False


def test_model_abstain_gives_no_citations(tenant_a, key_for, ask_app, llm):
    a_id, _ = tenant_a
    llm.answer = ABSTAIN
    body = ask(ask_app, key_for(a_id), "What is the boiling point of mercury?", stream=False).get_json()
    assert body == {"answer": ABSTAIN, "citations": [], "abstained": True}


def test_injected_page_is_fenced_and_the_rule_is_in_the_system_prompt(tenant_a, key_for, ask_app, llm):
    a_id, _ = tenant_a
    ask(ask_app, key_for(a_id), "What are the office hours during festival week under FIAS/ADM/2025-26/025?").get_data()
    assert UNTRUSTED_RULE in llm.last_system
    block = next(b for b in llm.last_user.split("<block ")[1:] if "FIAS/ADM/2025-26/025" in b)
    assert "SYSTEM NOTE TO AI ASSISTANTS" in block  # quoted as data, inside its fence
    assert block.split(">", 1)[0].endswith(f'pages="p.{INJECTION_PAGE}"')


# --- token log ----------------------------------------------------------------------------------


def test_token_log_has_each_call_and_no_question_text(tenant_a, key_for, ask_app, caplog):
    a_id, _ = tenant_a
    caplog.set_level(logging.INFO, logger="pagecite.ask")
    question = "Who approves zebracorn advances above Rs 50,000 and how soon are they settled?"
    resp = ask(ask_app, key_for(a_id), question)
    resp.get_data()
    rid = resp.headers["X-Request-ID"]

    lines = [r.getMessage() for r in caplog.records if r.name == "pagecite.ask"]
    calls = [re.search(r"call=(\w+)", line).group(1) for line in lines if f"request_id={rid}" in line and "llm_call" in line]
    assert calls == ["decompose", "embed_query", "rerank", "answer"]
    assert any(line.startswith("ask_done") and "prompt_tokens=" in line for line in lines)
    assert "zebracorn" not in caplog.text  # never the question, prompts or document text
    assert "cobalt heron" not in caplog.text


# --- connection lifetime and timeouts -----------------------------------------------------------


def test_no_db_connection_is_held_while_the_answer_streams(tenant_a, key_for, ask_app, llm):
    a_id, _ = tenant_a
    pool = ask_app.extensions["pagecite"].engine.pool
    seen: list[int] = []
    llm.on_first_token = lambda: seen.append(pool.checkedout())
    ask(ask_app, key_for(a_id), "What daily allowance does FIAS/HR/2025-26/014 set?").get_data()
    assert seen == [0]


def test_more_concurrent_streams_than_pool_connections(tenant_a, key_for, ask_app, llm):
    """18 asks are all mid-stream at the same moment; the pool has 5 + 10 overflow = 15 connections."""
    a_id, _ = tenant_a
    key = key_for(a_id)
    pool = ask_app.extensions["pagecite"].engine.pool
    n = pool.size() + pool._max_overflow + 3
    barrier = threading.Barrier(n, timeout=30)
    llm.on_first_token = barrier.wait  # every stream waits here until all n are streaming
    results: list[str | None] = [None] * n

    def worker(i: int) -> None:
        try:
            results[i] = ask(ask_app, key, "What daily allowance does FIAS/HR/2025-26/014 set?").get_data(as_text=True)
        except Exception as exc:  # BrokenBarrierError if streams were stuck waiting for a connection
            results[i] = f"FAILED {type(exc).__name__}"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(r and r.rstrip().split("\n")[-2] == "event: citations" for r in results), results[:3]


def test_answer_timeout_mid_stream_sends_an_error_event(tenant_a, key_for, ask_app, llm):
    a_id, _ = tenant_a
    llm.answer = "one two three four"
    llm.fail_after = (2, LLMTimeout("slow"))
    resp = ask(ask_app, key_for(a_id), "What daily allowance does FIAS/HR/2025-26/014 set?")
    events = parse_sse(resp.get_data(as_text=True))
    assert [name for name, _ in events] == ["token", "token", "error"]
    assert events[-1][1] == {"code": "UPSTREAM_TIMEOUT", "message": "the answer model timed out",
                             "request_id": resp.headers["X-Request-ID"]}

    body = ask(ask_app, key_for(a_id), "What daily allowance does FIAS/HR/2025-26/014 set?", stream=False)
    assert body.status_code == 504 and body.get_json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_missing_index_is_503(tenant_a, key_for, ask_app, redis_index):
    a_id, _ = tenant_a
    r, cfg = redis_index
    drop_index(r, cfg)
    resp = ask(ask_app, key_for(a_id), "What daily allowance does FIAS/HR/2025-26/014 set?")
    assert resp.status_code == 503 and resp.get_json()["error"]["code"] == "INDEX_UNAVAILABLE"


def test_blank_question_is_422(tenant_a, key_for, ask_app):
    a_id, _ = tenant_a
    resp = ask(ask_app, key_for(a_id), "   ")
    assert resp.status_code == 422


# --- citation chip URL against real MinIO -------------------------------------------------------


def test_presigned_get_serves_the_pdf_inline(tenant_a, key_for, ask_app):
    a_id, docs = tenant_a
    body = ask_app.test_client().get(f"/documents/{docs[CIRCULARS]}/url", headers=auth(key_for(a_id))).get_json()
    assert body["url"].startswith("http://localhost:9000/")
    got = requests.get(body["url"], timeout=30)
    assert got.status_code == 200
    assert got.headers["Content-Type"] == "application/pdf"
    assert got.headers["Content-Disposition"].startswith("inline")
    assert got.content[:5] == b"%PDF-"
