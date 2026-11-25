"""P5: the eval runner end to end on the samples, on the real index, with FakeEmbedder + FakeLLM.

This checks the plumbing (retrieve -> answer -> judge -> metrics -> eval_runs row), not quality: the
real-model numbers come from `python -m eval.run` against the RBI corpus.
"""

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.retrieval.llm import LLMTimeout
from apps.api.retrieval.pipeline import RetrievalDeps
from apps.worker.embedding import FakeEmbedder
from eval.config import parse_config
from eval.cost import Budget, BudgetExceeded
from eval.golden import ExpectedPage, GoldenRow
from eval.run import Runner, run_config
from eval.store import latest_runs, save_run
from tests.conftest import test_settings
from tests.fakes import FakeLLM

pytestmark = pytest.mark.integration

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
CFG = {"label": "hybrid", "strategy": "hybrid", "decompose": False, "k": 20, "rerank_candidates": 20,
       "top_n": 6, "passage_tokens": 200}


def golden_rows() -> list[GoldenRow]:
    rows = []
    for q in json.loads((SAMPLES / "questions.json").read_text()):
        kind = "semantic" if q["answerable"] else "unanswerable"
        rows.append(GoldenRow(q["id"], q["question"], [ExpectedPage(e["file"], e["page"], "-") for e in q["expected"]],
                              "-", q["answerable"], kind))
    return rows


def all_supported(user_prompt: str) -> dict:
    return {"verdicts": [{"i": int(i), "supported": True} for i in re.findall(r'<claim i="(\d+)"', user_prompt)]}


@pytest.fixture
def runner_for(ingested, redis_index, test_env):
    tenant_id, ids = ingested
    r, cfg = redis_index
    engine = create_engine(test_env.test_database_url)

    def _make(llm, cap: float = 5.0, cost_fn=lambda calls: 0.0) -> Runner:
        deps = RetrievalDeps(settings=test_settings(test_env), sessions=sessionmaker(engine), redis=r, index=cfg,
                             embedder=FakeEmbedder(dim=cfg.dim), llm=llm)
        return Runner(deps, tenant_id, {doc_id: name for name, doc_id in ids.items()}, llm, "judge", Budget(cap),
                      cost_fn=cost_fn)

    yield _make
    engine.dispose()


def test_runner_scores_every_row_and_writes_an_eval_run(runner_for, clean_db):
    llm = FakeLLM(answer="The allowance is Rs 2,400 [1]. Claims go in within 30 days [2].",
                  json_responses={"faithfulness": all_supported})
    rows = golden_rows()
    res = run_config(runner_for(llm), parse_config(CFG), rows, verbose=False)
    s = res.summary
    assert s.n_questions == len(rows) and s.n_errors == 0
    assert 0.0 <= s.hit_at_5 <= 1.0 and 0.0 <= s.mrr <= 1.0
    assert s.faithfulness == 1.0 and s.sentences_judged == 2 * len(rows)  # FakeLLM never abstains
    assert s.avg_tokens > 0 and s.avg_latency_ms > 0
    assert all(len(d["blocks"]) == 6 for d in res.details)
    assert all(b["document"] and b["document"].endswith(".pdf") for d in res.details for b in d["blocks"])

    with sessionmaker(clean_db)() as session, session.begin():
        save_run(session, parse_config(CFG), s, git_sha="test", judge_model="judge", cost_usd=res.cost_usd)
    with sessionmaker(clean_db)() as session:
        (run,) = latest_runs(session)
        assert run.label == "hybrid" and run.n_questions == len(rows)


def test_a_failed_answer_is_an_error_row_not_a_dropped_one(runner_for):
    llm = FakeLLM(answer="One [1]. Two [1].", fail_after=(1, LLMTimeout("slow")))
    rows = golden_rows()[:2]
    res = run_config(runner_for(llm), parse_config(CFG), rows, verbose=False)
    assert res.summary.n_questions == 2 and res.summary.n_errors == 2
    assert all(r.error == "UPSTREAM_TIMEOUT" and r.abstained is None for r in res.rows)
    assert res.summary.abstention_accuracy == 0.0  # errors are wrong, not skipped
    assert llm.json_calls == []  # nothing to judge


def test_misaligned_judge_output_is_an_error_with_unsupported_sentences(runner_for):
    llm = FakeLLM(answer="A [1]. B [2].", json_responses={"faithfulness": {"verdicts": [{"i": 0, "supported": True}]}})
    res = run_config(runner_for(llm), parse_config(CFG), golden_rows()[:1], verbose=False)
    (row,) = res.rows
    assert row.error == "JUDGE_MISALIGNED"
    assert (row.sentences_total, row.sentences_supported) == (2, 0)


def test_the_budget_cap_stops_the_run(runner_for):
    llm = FakeLLM(answer="A [1].", json_responses={"faithfulness": all_supported})
    runner = runner_for(llm, cap=0.05, cost_fn=lambda calls: 0.02 * len(calls))
    with pytest.raises(BudgetExceeded):
        run_config(runner, parse_config(CFG), golden_rows(), verbose=False)
