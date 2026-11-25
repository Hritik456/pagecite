"""The P5 eval runner: the golden set through each retrieval config, scored and stored in eval_runs.

    uv run python -m eval.run --judge-check                      # the judge vs 8 hand-labelled cases
    uv run python -m eval.run --config configs/hybrid.yaml --estimate
    uv run python -m eval.run --config configs/vector_only.yaml --config configs/hybrid.yaml \\
        --config configs/hybrid_rerank.yaml --config configs/hybrid_rerank_decomp.yaml --write-results

Each row runs in-process: retrieve() then answer_events() (apps/api/retrieval/pipeline.py), exactly
the /ask path, because the API's citations event hides the block ranking that hit@5 and MRR need.
Non-abstained answers then go to the faithfulness judge (eval/judge.py).

Safety:
- Pre-flight before any paid call: the eval-rbi tenant has every corpus file INDEXED once and the
  MySQL chunk count equals Redis. Otherwise nothing runs.
- --max-cost-usd (default 2.0) is a hard cap on the actual cumulative cost of this invocation.
- A failed call is an error on that row (a miss in every metric), never dropped. A config with more
  than 10% errored rows is not written to eval_runs.
- DB connections are short: retrieve() closes its own; the runner opens one only for the final insert.
- Console output is ids, metrics, tokens and timings only. Answers and judgements go to eval/out/
  (gitignored), never to the log.
"""

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.config import Settings
from apps.api.retrieval.decompose import needs_decomposition
from apps.api.retrieval.llm import LLM, LLMError, OpenAILLM
from apps.api.retrieval.pipeline import RetrievalDeps, UpstreamError, answer_events, retrieve
from apps.api.retrieval.prompt import answer_messages
from apps.api.retrieval.rerank import Candidate, system_prompt, user_prompt
from apps.api.retrieval.search import IndexUnavailable
from apps.api.retrieval.usage import UsageLog
from apps.api.search_index import index_config, make_redis
from apps.worker.chunking import get_encoding

from . import judge as judge_mod
from .build_corpus import load_manifest
from .config import EvalConfig, load_config
from .cost import Budget, BudgetExceeded, cost_of_calls, price_for
from .golden import GoldenRow, load_golden
from .ingest_corpus import TENANT, preflight, tenant_id_by_name
from .metrics import Expected, RankedBlock, RowResult, Summary, hit_at_k, reciprocal_rank, summarise
from .overlap import overlap
from .store import save_run

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "out"
RESULTS = ROOT / "docs" / "results.md"
JUDGE_CHECK = ROOT / "eval" / "judge_check.jsonl"
MAX_ERROR_SHARE = 0.10
EVAL_START, EVAL_END = "<!-- eval:start -->", "<!-- eval:end -->"


@dataclass
class Runner:
    deps: RetrievalDeps
    tenant_id: int
    doc_names: dict[int, str]  # document id -> file name
    judge_llm: LLM
    judge_model: str
    budget: Budget
    cost_fn: Callable[[list], float] = cost_of_calls  # injectable: fakes have no price


@dataclass
class ConfigResult:
    cfg: EvalConfig
    summary: Summary
    rows: list[RowResult]
    details: list[dict]
    cost_usd: float  # retrieval + answer calls
    judge_cost_usd: float
    run_id: int | None = None
    written: bool = False

    @property
    def total_cost_usd(self) -> float:
        return self.cost_usd + self.judge_cost_usd


def _judge_calls(judgement) -> list:
    from apps.api.retrieval.usage import CallRecord

    return [CallRecord("judge", model, usage, ms) for model, usage, ms in judgement.calls]


def run_row(runner: Runner, cfg: EvalConfig, row: GoldenRow) -> tuple[RowResult, dict, float, float]:
    """One golden row through one config. Returns (result, detail for eval/out, cost, judge cost)."""
    usage = UsageLog(f"eval-{cfg.label}-{row.id}", runner.tenant_id)
    result = RowResult(row.id, row.kind, row.answerable, [Expected(e.document, e.page) for e in row.expected])
    detail: dict = {"id": row.id, "kind": row.kind, "config": cfg.label}
    started = time.perf_counter()
    answer, citations, blocks = "", [], []
    try:
        retrieved = retrieve(runner.deps, runner.tenant_id, row.question, cfg.retrieval, usage)
        blocks = retrieved.blocks
        result.blocks = [RankedBlock(runner.doc_names.get(b.document_id, f"doc-{b.document_id}"), b.page_from, b.page_to)
                         for b in blocks]
        detail["sub_queries"] = len(retrieved.sub_queries)
        parts = []
        for name, data in answer_events(runner.deps, row.question, blocks, usage):
            if name == "token":
                parts.append(data["text"])
            elif name == "citations":
                citations, result.abstained = data["citations"], data["abstained"]
            elif name == "error":
                result.error = data["code"]
        answer = "".join(parts)
    except (UpstreamError, IndexUnavailable) as exc:
        result.error = type(exc).__name__
    result.latency_ms = (time.perf_counter() - started) * 1000
    if result.error is None:
        result.tokens = sum(c.usage.prompt_tokens + c.usage.completion_tokens for c in usage.calls)
    cost = runner.cost_fn(usage.calls)
    runner.budget.charge(cost)

    judge_cost = 0.0
    judgement = None
    if result.error is None and result.abstained is False:
        try:
            judgement = judge_mod.judge_answer(runner.judge_llm, runner.judge_model, answer,
                                               {b.n: b.text for b in blocks})
            result.sentences_total, result.sentences_supported = judgement.total, judgement.n_supported
        except (judge_mod.JudgeError, LLMError) as exc:
            result.error = f"JUDGE_{exc.code}"
            result.sentences_total = len(judge_mod.split_sentences(answer))  # all count as unsupported
        if judgement is not None:
            judge_cost = runner.cost_fn(_judge_calls(judgement))
            runner.budget.charge(judge_cost)

    detail.update(
        question=row.question,
        answer=answer,
        abstained=result.abstained,
        error=result.error,
        blocks=[{"n": b.n, "chunk_id": b.chunk_id, "document": runner.doc_names.get(b.document_id),
                 "pages": [b.page_from, b.page_to]} for b in blocks],
        cited=[c["n"] for c in citations],
        expected=[asdict(e) for e in result.expected],
        hit_at_5=hit_at_k(result.blocks, result.expected) if row.answerable else None,
        rr=reciprocal_rank(result.blocks, result.expected) if row.answerable else None,
        judgement=None if judgement is None else [
            {"sentence": s.text, "cites": s.cites, "verdict": v} for s, v in zip(judgement.sentences, judgement.reasons)
        ],
        tokens=result.tokens,
        latency_ms=round(result.latency_ms),
        calls=[{"call": c.call, "model": c.model, "prompt": c.usage.prompt_tokens,
                "completion": c.usage.completion_tokens, "ms": round(c.ms)} for c in usage.calls],
        cost_usd=round(cost, 6),
        judge_cost_usd=round(judge_cost, 6),
        notes={k: v for k, v in usage.notes.items()},
    )
    return result, detail, cost, judge_cost


def _line(r: RowResult, detail: dict) -> str:
    """Console line: ids and numbers only."""
    hit = "-" if detail["hit_at_5"] is None else int(detail["hit_at_5"])
    rr = "-" if detail["rr"] is None else f"{detail['rr']:.2f}"
    abst = {True: "abstain", False: "answer", None: "-"}[r.abstained]
    sent = f"{r.sentences_supported}/{r.sentences_total}" if r.sentences_total else "-"
    err = f" ERROR={r.error}" if r.error else ""
    return (f"  {r.id:<4} {r.kind:<12} hit@5={hit} rr={rr} {abst:<7} supported={sent:<5} "
            f"tokens={r.tokens or '-'} {r.latency_ms / 1000:.1f}s{err}")


def run_config(runner: Runner, cfg: EvalConfig, rows: list[GoldenRow], verbose: bool = True) -> ConfigResult:
    results, details, cost, judge_cost = [], [], 0.0, 0.0
    for row in rows:
        r, d, c, jc = run_row(runner, cfg, row)
        results.append(r)
        details.append(d)
        cost += c
        judge_cost += jc
        if verbose:
            print(_line(r, d), flush=True)
    return ConfigResult(cfg, summarise(results), results, details, cost, judge_cost)


# --- environment ------------------------------------------------------------------------------------


def git_sha() -> tuple[str, bool]:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                                capture_output=True, text=True).stdout.strip())
    return sha, dirty


def build_deps(settings: Settings) -> RetrievalDeps:
    """The same components as apps/api/state.build_retrieval, with a pool of its own."""
    from apps.worker.embedding import OpenAIEmbedder

    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is not set")
    key = settings.openai_api_key.get_secret_value()
    engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=2)
    return RetrievalDeps(
        settings=settings,
        sessions=sessionmaker(engine, expire_on_commit=False),
        redis=make_redis(settings.redis_url),
        index=index_config(settings),
        embedder=OpenAIEmbedder(key, settings.openai_embed_model, settings.embed_dim,
                                timeout_s=settings.embed_timeout_s, max_retries=1),
        llm=OpenAILLM(key, json_timeout_s=settings.json_timeout_s, answer_timeout_s=settings.answer_timeout_s),
    )


def eval_tenant(settings: Settings) -> tuple[int, dict[int, str]]:
    engine = create_engine(settings.database_url)
    try:
        tenant_id = tenant_id_by_name(engine, TENANT)
        if tenant_id is None:
            raise SystemExit(f"tenant {TENANT} does not exist: run `python -m eval.ingest_corpus` first")
        files = [f["file"] for f in load_manifest()["files"]]
        pf = preflight(engine, settings, tenant_id, files)
    finally:
        engine.dispose()
    if not pf.ok:
        raise SystemExit("pre-flight failed, nothing was spent:\n  " + "\n  ".join(pf.problems))
    print(f"pre-flight OK: tenant {TENANT} id={tenant_id}, {len(pf.documents)} documents INDEXED, "
          f"chunks MySQL={pf.mysql_chunks} = Redis={pf.redis_chunks}")
    return tenant_id, {doc_id: name for name, doc_id in pf.documents.items()}


# --- judge check ------------------------------------------------------------------------------------


def judge_check(llm: LLM, model: str, budget: Budget) -> int:
    cases = [json.loads(line) for line in JUDGE_CHECK.read_text(encoding="utf-8").splitlines() if line.strip()]
    for case in cases:  # free pre-check: the splitter must produce one sentence per label
        n = len(judge_mod.split_sentences(case["answer"]))
        if n != len(case["expected"]):
            print(f"{case['id']}: the splitter gives {n} sentences, the labels have {len(case['expected'])}")
            return 2
    agree, results, cost = 0, [], 0.0
    for case in cases:
        blocks = {int(k): v for k, v in case["blocks"].items()}
        j = judge_mod.judge_answer(llm, model, case["answer"], blocks)
        c = cost_of_calls(_judge_calls(j))
        budget.charge(c)
        cost += c
        ok = j.supported == case["expected"]
        agree += ok
        results.append({"id": case["id"], "expected": case["expected"], "got": j.supported, "agree": ok})
        print(f"  {'AGREE' if ok else 'DISAGREE':<8} {case['id']:<28} expected={case['expected']} got={j.supported}")
    OUT.mkdir(parents=True, exist_ok=True)
    sha, dirty = git_sha()
    record = {"judge_model": model, "agree": agree, "total": len(cases), "cost_usd": round(cost, 6),
              "git_sha": sha, "dirty": dirty, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "cases": results}
    (OUT / "judge_check.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"judge check: {agree}/{len(cases)} agree, cost ${cost:.4f}")
    return 0 if agree == len(cases) else 1


# --- estimate ---------------------------------------------------------------------------------------

ASSUMED_ANSWER_OUT = 200
ASSUMED_JUDGE_OUT = 150
ASSUMED_RERANK_OUT = 60
ASSUMED_DECOMPOSE = (150, 60)


def estimate(deps: RetrievalDeps, tenant_id: int, configs: list[EvalConfig], rows: list[GoldenRow],
             judge_model: str) -> float:
    """Token counts on the real retrieved blocks; only query embeddings are paid (about $0.0001).

    Retrieval runs as plain hybrid with 20 candidates (no LLM): the first 6 stand in for the answer's
    context blocks, all 20 for the rerank prompt. Output tokens are assumptions (stated above).
    """
    s = deps.settings
    enc = get_encoding(s.tokenizer)
    tok = lambda text: len(enc.encode(text, disallowed_special=()))  # noqa: E731
    probe = replace(configs[0].retrieval, strategy="hybrid", decompose=False, top_n=20)
    per_row = []
    embed_tokens = 0
    for row in rows:
        usage = UsageLog(f"estimate-{row.id}", tenant_id)
        blocks = retrieve(deps, tenant_id, row.question, probe, usage).blocks
        embed_tokens += sum(c.usage.prompt_tokens for c in usage.calls)
        system, user = answer_messages(row.question, blocks[:6])
        answer_in = tok(system) + tok(user)
        cands = [Candidate(b.chunk_id, b.document_id, b.title, b.page_from, b.page_to, b.text, 0.0) for b in blocks]
        rerank_in = tok(system_prompt(6)) + tok(user_prompt(row.question, cands, probe.passage_tokens))
        judge_in = tok(judge_mod.SYSTEM) + tok("\n".join(b.text for b in blocks[:3])) + ASSUMED_ANSWER_OUT + 100
        per_row.append((row, answer_in, rerank_in, judge_in))

    def usd(model: str, tokens_in: int, tokens_out: int) -> float:
        p = price_for(model)
        return (tokens_in * p.input + tokens_out * p.output) / 1_000_000

    total = 0.0
    print(f"{'config':<22} {'answer':>8} {'rerank':>8} {'decomp':>8} {'judge':>8} {'total':>8}")
    for cfg in configs:
        a = r = d = j = 0.0
        for row, answer_in, rerank_in, judge_in in per_row:
            a += usd(s.openai_chat_model, answer_in, ASSUMED_ANSWER_OUT)
            if cfg.retrieval.strategy == "hybrid_rerank":
                r += usd(s.openai_fast_model, rerank_in, ASSUMED_RERANK_OUT)
            if cfg.retrieval.decompose and needs_decomposition(row.question):
                d += usd(s.openai_fast_model, *ASSUMED_DECOMPOSE)
            if row.answerable:  # unanswerable rows are expected to abstain: no judge call
                j += usd(judge_model, judge_in, ASSUMED_JUDGE_OUT)
        sub = a + r + d + j
        total += sub
        print(f"{cfg.label:<22} {a:>8.4f} {r:>8.4f} {d:>8.4f} {j:>8.4f} {sub:>8.4f}")
    embed_usd = usd(s.openai_embed_model, embed_tokens, 0) * (len(configs) + 1)
    total += embed_usd
    print(f"{'query embeddings':<22} {embed_usd:>44.4f}")
    print(f"estimated total for {len(rows)} rows x {len(configs)} configs: ${total:.2f} "
          f"(output tokens assumed: answer {ASSUMED_ANSWER_OUT}, judge {ASSUMED_JUDGE_OUT}, rerank {ASSUMED_RERANK_OUT})")
    return total


# --- results ----------------------------------------------------------------------------------------


def best_config(results: list[ConfigResult]) -> ConfigResult:
    """Fixed before running: highest hit@5, then faithfulness, then fewer tokens."""
    return max(results, key=lambda r: (r.summary.hit_at_5 or 0, r.summary.faithfulness or 0,
                                       -(r.summary.avg_tokens or float("inf"))))


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def results_markdown(results: list[ConfigResult], rows: list[GoldenRow], settings: Settings, judge_model: str,
                     at: datetime | None = None, sha_dirty: tuple[str, bool] | None = None) -> str:
    sha, dirty = sha_dirty or git_sha()
    at = at or datetime.now(timezone.utc)
    best = best_config(results)
    jc_path = OUT / "judge_check.json"
    jc = json.loads(jc_path.read_text(encoding="utf-8")) if jc_path.exists() else None
    kinds = ["semantic", "identifier", "bank_type", "multi"]
    overlaps: dict[str, list[float]] = {k: [] for k in kinds}
    for row in rows:
        score = overlap(row.question, [e.quote for e in row.expected], row.kind)
        if score is not None and row.kind in overlaps:
            overlaps[row.kind].append(score)
    n_ans = sum(r.answerable for r in rows)
    lines = [
        EVAL_START,
        f"## Eval: 4 retrieval configs on the RBI golden set ({at:%Y-%m-%d %H:%M} UTC)",
        "",
        f"- Commit `{sha[:7]}`{' + uncommitted changes' if dirty else ''}; `python -m eval.run` with "
        f"{', '.join(f'`configs/{r.cfg.label}.yaml`' for r in results)}.",
        f"- Corpus: 60 RBI notification PDFs, 395 pages (`eval/corpus.json`), tenant `eval-rbi`. Golden set: "
        f"{len(rows)} rows ({n_ans} answerable, {len(rows) - n_ans} unanswerable), reviewed against the PDFs "
        f"(`eval/golden_review.md`).",
        f"- Models: answer `{settings.openai_chat_model}`, decompose/rerank `{settings.openai_fast_model}`, "
        f"embeddings `{settings.openai_embed_model}`, faithfulness judge `{judge_model}`"
        + (f" (agreed with {jc['agree']}/{jc['total']} hand-labelled judge-check cases, including two prompt-injection cases)."
           if jc else "."),
        "- One run per config: models are not deterministic, so differences of a row or two are within noise.",
        "",
        "| Config | hit@5 | MRR | recall@5 (multi) | Faithfulness | Abstention acc. | False abstain | Unanswerable abstained | Avg tokens | Avg latency | Errors | Cost |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        s = r.summary
        mark = " **(best)**" if r is best else ""
        lines.append(
            f"| `{r.cfg.label}`{mark} | {_pct(s.hit_at_5)} | {s.mrr:.3f} | {_pct(s.recall_at_5_multi)} | "
            f"{_pct(s.faithfulness)} ({s.sentences_judged} sentences) | {_pct(s.abstention_accuracy)} | "
            f"{_pct(s.false_abstain_rate)} | {_pct(s.unanswerable_abstained)} | {s.avg_tokens:,.0f} | "
            f"{s.avg_latency_ms / 1000:.1f} s | {s.n_errors} | ${r.total_cost_usd:.3f} |"
        )
    total = sum(r.total_cost_usd for r in results)
    lines += [
        "",
        f"Metrics: hit@5 and MRR over the {n_ans} answerable rows (a block hits if it is the expected document "
        "and its page range contains the expected page); faithfulness = judged answer sentences supported by the "
        "block(s) they cite, uncited sentences counted as unsupported; abstention accuracy over all rows. "
        "Tokens and latency per question cover retrieval + answer (decompose, query embedding, rerank, answer), "
        f"not the judge. Cost per config includes its judge calls; total ${total:.2f}.",
        "",
        f"**Best config (rule fixed before the run: hit@5, then faithfulness, then tokens): `{best.cfg.label}`** "
        f"with hit@5 {_pct(best.summary.hit_at_5)}, MRR {best.summary.mrr:.3f}, faithfulness "
        f"{_pct(best.summary.faithfulness)}.",
        "",
        "### hit@5 by question kind, next to the lexical overlap of the questions",
        "",
        "Overlap = share of the question's BM25 terms that also appear in the source quote (identifier and bank-type "
        "words excluded for those kinds). Low overlap means the text half of hybrid search gets no help from copied wording.",
        "",
        "| Kind | Rows | Mean overlap | " + " | ".join(f"`{r.cfg.label}`" for r in results) + " |",
        "|---|---|---|" + "---|" * len(results),
    ]
    for k in kinds:
        vals = overlaps[k]
        cells = " | ".join(_pct(r.summary.hit_at_5_by_kind.get(k)) for r in results)
        lines.append(f"| {k} | {len(vals)} | {sum(vals) / len(vals):.2f} | {cells} |")
    lines += ["", EVAL_END]
    return "\n".join(lines)


def write_results(markdown: str) -> None:
    text = RESULTS.read_text(encoding="utf-8")
    if EVAL_START in text and EVAL_END in text:
        head, rest = text.split(EVAL_START, 1)
        _, tail = rest.split(EVAL_END, 1)
        text = head + markdown + tail
    else:
        marker = "\n## Chaos:"
        head, sep, tail = text.partition(marker)
        text = head.rstrip() + "\n\n" + markdown + "\n" + (sep + tail if sep else "")
    RESULTS.write_text(text, encoding="utf-8", newline="\n")


# --- rebuild from stored details (no model calls) ------------------------------------------------------


def rows_from_details(details: list[dict], golden: dict[str, GoldenRow]) -> list[RowResult]:
    """eval/out/*.jsonl -> RowResults, so a run can be re-scored or re-rendered without spending again."""
    out = []
    for d in details:
        g = golden[d["id"]]
        r = RowResult(g.id, g.kind, g.answerable, [Expected(e.document, e.page) for e in g.expected])
        r.blocks = [RankedBlock(b["document"], b["pages"][0], b["pages"][1]) for b in d["blocks"]]
        r.abstained, r.error, r.tokens, r.latency_ms = d["abstained"], d["error"], d["tokens"], d["latency_ms"]
        if d["judgement"] is not None:
            r.sentences_total = len(d["judgement"])
            r.sentences_supported = sum(v["verdict"] == "supported" for v in d["judgement"])
        elif (d["error"] or "").startswith("JUDGE_"):
            r.sentences_total = len(judge_mod.split_sentences(d["answer"]))
        out.append(r)
    return out


def rebuild(stamp: str, configs: list[EvalConfig], settings: Settings, judge_model: str, write: bool) -> int:
    """Re-score a finished run from eval/out/<stamp>-<label>.jsonl, check it against its eval_runs rows,
    set their est_cost_usd to the full cost (judge included), and optionally re-render docs/results.md."""
    from sqlalchemy import select

    from apps.api.models import EvalRun

    golden = {r.id: r for r in load_golden()}
    engine = create_engine(settings.database_url)
    results, sha, run_judge = [], None, judge_model
    try:
        for cfg in configs:
            path = OUT / f"{stamp}-{cfg.label}.jsonl"
            details = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            rows = rows_from_details(details, golden)
            res = ConfigResult(cfg, summarise(rows), rows, details,
                               sum(d["cost_usd"] for d in details), sum(d["judge_cost_usd"] for d in details))
            with sessionmaker(engine)() as session, session.begin():
                run = session.scalars(select(EvalRun).where(EvalRun.label == cfg.label)
                                      .order_by(EvalRun.id.desc()).limit(1)).first()
                if run is None:
                    raise SystemExit(f"no eval_runs row for {cfg.label}")
                s = res.summary
                same = (run.n_questions == s.n_questions
                        and abs(float(run.hit_at_5) - s.hit_at_5) < 1e-3 and abs(float(run.mrr) - s.mrr) < 1e-3
                        and abs(float(run.faithfulness) - s.faithfulness) < 1e-3)
                if not same:
                    raise SystemExit(f"{path.name} does not match eval_runs id={run.id}; refusing to touch it")
                old = run.est_cost_usd
                run.est_cost_usd = round(Decimal(str(res.total_cost_usd)), 4)
                res.run_id, res.written, sha = run.id, True, run.git_sha
                run_judge = run.judge_model or judge_model  # the judge that run used, not today's setting
            print(f"{cfg.label}: matches eval_runs id={res.run_id}; est_cost_usd {old} -> {res.total_cost_usd:.4f}")
            results.append(res)
    finally:
        engine.dispose()
    if write:
        at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        rows = load_golden()
        write_results(results_markdown(results, rows, settings, run_judge, at=at, sha_dirty=(sha, git_sha()[1])))
        print(f"updated {RESULTS.relative_to(ROOT)}")
    return 0


# --- main ---------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", action="append", default=[], help="a configs/*.yaml file (repeatable)")
    parser.add_argument("--limit", type=int, help="only the first N golden rows (development)")
    parser.add_argument("--estimate", action="store_true", help="print the expected cost and exit")
    parser.add_argument("--judge-check", action="store_true", help="run the judge on eval/judge_check.jsonl")
    parser.add_argument("--max-cost-usd", type=float, default=2.0)
    parser.add_argument("--write-results", action="store_true", help="update docs/results.md")
    parser.add_argument("--rebuild", metavar="STAMP", help="re-score eval/out/STAMP-*.jsonl (no model calls)")
    args = parser.parse_args()

    settings = Settings()
    judge_model = settings.eval_judge_model
    price_for(judge_model)  # refuse unknown prices before anything runs
    budget = Budget(args.max_cost_usd)

    if args.judge_check:
        deps = build_deps(settings)
        return judge_check(deps.llm, judge_model, budget)

    if not args.config:
        parser.error("give at least one --config (or --judge-check)")
    configs = [load_config(p) for p in args.config]
    if args.rebuild:
        return rebuild(args.rebuild, configs, settings, judge_model, args.write_results)
    rows = load_golden()[: args.limit] if args.limit else load_golden()
    tenant_id, doc_names = eval_tenant(settings)  # pre-flight before any paid call
    deps = build_deps(settings)

    if args.estimate:
        total = estimate(deps, tenant_id, configs, rows, judge_model)
        print("OVER the cap" if total > args.max_cost_usd else "within the cap", f"(${args.max_cost_usd:.2f})")
        return 0

    runner = Runner(deps, tenant_id, doc_names, deps.llm, judge_model, budget)
    sha, dirty = git_sha()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[ConfigResult] = []
    try:
        for cfg in configs:
            print(f"\n== {cfg.label} ({len(rows)} rows)", flush=True)
            res = run_config(runner, cfg, rows)
            out = OUT / f"{stamp}-{cfg.label}.jsonl"
            out.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in res.details) + "\n", encoding="utf-8")
            s = res.summary
            print(f"  -> hit@5={_pct(s.hit_at_5)} MRR={s.mrr:.3f} faithfulness={_pct(s.faithfulness)} "
                  f"abstention={_pct(s.abstention_accuracy)} errors={s.n_errors} cost=${res.total_cost_usd:.4f} "
                  f"(spent so far ${budget.spent_usd:.4f}); details in {out.relative_to(ROOT)}", flush=True)
            if s.n_errors > MAX_ERROR_SHARE * s.n_questions:
                print(f"  NOT written to eval_runs: {s.n_errors} errors > {MAX_ERROR_SHARE:.0%} of rows")
            else:
                with deps.sessions() as session, session.begin():  # short: only the insert
                    res.run_id = save_run(session, cfg, s, git_sha=sha, judge_model=judge_model,
                                          cost_usd=res.total_cost_usd)
                res.written = True
                print(f"  eval_runs id={res.run_id}")
            results.append(res)
    except BudgetExceeded as exc:
        print(f"\nABORTED: {exc}. Configs finished before the cap: {[r.cfg.label for r in results]}")
        return 3

    if args.write_results and results:
        if len(results) == len(configs) and all(r.written for r in results):
            write_results(results_markdown(results, rows, settings, judge_model))
            print(f"\nupdated {RESULTS.relative_to(ROOT)}")
        else:
            print("\ndocs/results.md NOT updated: not every config finished and was written")
    best = best_config(results)
    print(f"\nbest config: {best.cfg.label} (hit@5 {_pct(best.summary.hit_at_5)}, "
          f"faithfulness {_pct(best.summary.faithfulness)}); total cost ${budget.spent_usd:.4f}")
    return 0 if all(r.written for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
