"""OpenAI cost from recorded token usage. Pure.

Prices are USD per 1M tokens, standard (non-batch) tier, from OpenAI's model pages
(developers.openai.com/api/docs/models/gpt-5.4-mini, .../gpt-5.4-nano), checked 2026-11-25.
Cached input is billed at the cached rate; reasoning tokens are part of completion tokens.
"""

import re
from dataclasses import dataclass

from apps.api.retrieval.llm import Usage


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


PRICES_PER_1M: dict[str, Price] = {
    "gpt-5.4-mini": Price(input=0.75, cached_input=0.075, output=4.50),
    "gpt-5.4-nano": Price(input=0.20, cached_input=0.02, output=1.25),
    "text-embedding-3-small": Price(input=0.02, cached_input=0.02, output=0.0),
}


class UnknownModelPrice(KeyError):
    pass


_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def price_for(model: str) -> Price:
    """A dated snapshot (gpt-5.4-mini-2026-03-17) is billed like its base model; anything else unknown is refused."""
    price = PRICES_PER_1M.get(model) or PRICES_PER_1M.get(_SNAPSHOT.sub("", model))
    if price is None:
        raise UnknownModelPrice(f"no price for model {model!r}; add it to eval/cost.py before spending")
    return price


def cost_usd(model: str, usage: Usage) -> float:
    p = price_for(model)
    cached = min(usage.cached_tokens, usage.prompt_tokens)
    fresh = usage.prompt_tokens - cached
    return (fresh * p.input + cached * p.cached_input + usage.completion_tokens * p.output) / 1_000_000


def cost_of_calls(calls) -> float:
    """calls: CallRecord-like objects with .model and .usage (apps/api/retrieval/usage.py)."""
    return sum(cost_usd(c.model, c.usage) for c in calls)


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Cumulative spend across a whole eval invocation. `charge` raises once the cap is passed."""

    def __init__(self, cap_usd: float):
        self.cap_usd = cap_usd
        self.spent_usd = 0.0

    def charge(self, usd: float) -> None:
        self.spent_usd += usd
        if self.spent_usd > self.cap_usd:
            raise BudgetExceeded(f"spent ${self.spent_usd:.4f} > cap ${self.cap_usd:.2f}")
