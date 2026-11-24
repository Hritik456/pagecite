"""Per-request token log: one line per model call and one summary line. Ids, counts and timings only:
never the question, prompts, completions, document text or the API key."""

import logging
from dataclasses import dataclass, field

from .llm import Usage

log = logging.getLogger("pagecite.ask")


@dataclass
class CallRecord:
    call: str  # decompose | embed_query | rerank | answer
    model: str
    usage: Usage
    ms: float


@dataclass
class UsageLog:
    request_id: str
    tenant_id: int
    calls: list[CallRecord] = field(default_factory=list)
    notes: dict[str, object] = field(default_factory=dict)

    def record(self, call: str, model: str, usage: Usage, ms: float) -> None:
        self.calls.append(CallRecord(call, model, usage, ms))
        log.info(
            "llm_call request_id=%s tenant_id=%s call=%s model=%s prompt_tokens=%s completion_tokens=%s "
            "reasoning_tokens=%s cached_tokens=%s ms=%.0f",
            self.request_id, self.tenant_id, call, model, usage.prompt_tokens, usage.completion_tokens,
            usage.reasoning_tokens, usage.cached_tokens, ms,
        )

    def note(self, **values: object) -> None:
        """Counters and outcome codes for the summary line (e.g. rerank_fallback=UPSTREAM_TIMEOUT)."""
        self.notes.update(values)

    def summary(self, total_ms: float) -> None:
        prompt = sum(c.usage.prompt_tokens for c in self.calls)
        completion = sum(c.usage.completion_tokens for c in self.calls)
        extra = " ".join(f"{k}={v}" for k, v in self.notes.items())
        log.info(
            "ask_done request_id=%s tenant_id=%s calls=%s prompt_tokens=%s completion_tokens=%s ms=%.0f %s",
            self.request_id, self.tenant_id, len(self.calls), prompt, completion, total_ms, extra,
        )
