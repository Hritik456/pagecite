"""eval_runs rows (V1 + V4): one per config per invocation."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.models import EvalRun

from .config import EvalConfig
from .metrics import Summary


def _dec(value: float | None, places: int) -> Decimal | None:
    return None if value is None else round(Decimal(str(value)), places)


def save_run(
    session: Session, cfg: EvalConfig, summary: Summary, *, git_sha: str, judge_model: str, cost_usd: float,
) -> int:
    run = EvalRun(
        config=cfg.as_json(),
        label=cfg.label,
        n_questions=summary.n_questions,
        hit_at_5=_dec(summary.hit_at_5, 4),
        mrr=_dec(summary.mrr, 4),
        faithfulness=_dec(summary.faithfulness, 4),
        abstention_accuracy=_dec(summary.abstention_accuracy, 4),
        false_abstain_rate=_dec(summary.false_abstain_rate, 4),
        avg_tokens=_dec(summary.avg_tokens, 1),
        avg_latency_ms=_dec(summary.avg_latency_ms, 1),
        n_errors=summary.n_errors,
        est_cost_usd=_dec(cost_usd, 4),
        git_sha=git_sha[:40],
        judge_model=judge_model,
    )
    session.add(run)
    session.flush()
    return run.id


def latest_runs(session: Session, git_sha: str | None = None) -> list[EvalRun]:
    """Newest row per label (optionally for one commit), in id order."""
    stmt = select(EvalRun).order_by(EvalRun.id.desc())
    if git_sha:
        stmt = stmt.where(EvalRun.git_sha == git_sha)
    newest: dict[str, EvalRun] = {}
    for run in session.scalars(stmt):
        newest.setdefault(run.label or str(run.id), run)
    return sorted(newest.values(), key=lambda r: r.id)
