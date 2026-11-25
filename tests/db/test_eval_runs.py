"""eval_runs with the V4 metric columns (P5)."""

from decimal import Decimal

from sqlalchemy.orm import Session

from eval.config import parse_config
from eval.metrics import Summary
from eval.store import latest_runs, save_run

CFG = {"label": "hybrid", "strategy": "hybrid", "decompose": False, "k": 20, "rerank_candidates": 20,
       "top_n": 6, "passage_tokens": 200}


def summary(**overrides) -> Summary:
    values = dict(
        n_questions=45, n_answerable=40, hit_at_5=0.825, mrr=0.71234, recall_at_5_multi=0.6,
        faithfulness=0.93333, sentences_judged=120, abstention_accuracy=0.9556, false_abstain_rate=0.025,
        unanswerable_abstained=1.0, avg_tokens=3456.78, avg_latency_ms=2345.67, n_errors=1,
        hit_at_5_by_kind={"semantic": 0.9},
    )
    return Summary(**{**values, **overrides})


def test_save_run_writes_every_v4_column(clean_db):
    with Session(clean_db) as session, session.begin():
        run_id = save_run(session, parse_config(CFG), summary(), git_sha="a" * 40, judge_model="gpt-5.4-mini",
                          cost_usd=0.123456)
    with Session(clean_db) as session:
        (run,) = latest_runs(session)
        assert run.id == run_id and run.label == "hybrid"
        assert run.config == CFG
        assert run.hit_at_5 == Decimal("0.8250") and run.mrr == Decimal("0.7123")
        assert run.faithfulness == Decimal("0.9333") and run.abstention_accuracy == Decimal("0.9556")
        assert run.false_abstain_rate == Decimal("0.0250")
        assert run.avg_tokens == Decimal("3456.8") and run.avg_latency_ms == Decimal("2345.7")
        assert run.n_errors == 1 and run.est_cost_usd == Decimal("0.1235")
        assert run.git_sha == "a" * 40 and run.judge_model == "gpt-5.4-mini"
        assert run.created_at is not None


def test_latest_runs_keeps_the_newest_row_per_label_and_filters_by_commit(clean_db):
    with Session(clean_db) as session, session.begin():
        save_run(session, parse_config(CFG), summary(hit_at_5=0.5), git_sha="old", judge_model="j", cost_usd=0)
        newest = save_run(session, parse_config(CFG), summary(hit_at_5=0.8), git_sha="new", judge_model="j", cost_usd=0)
        other = save_run(session, parse_config({**CFG, "label": "vector_only", "strategy": "vector_only"}),
                         summary(faithfulness=None), git_sha="new", judge_model="j", cost_usd=0)
    with Session(clean_db) as session:
        assert [r.id for r in latest_runs(session)] == [newest, other]
        assert [r.id for r in latest_runs(session, git_sha="old")] == [newest - 1]
        assert latest_runs(session, git_sha="new")[1].faithfulness is None
