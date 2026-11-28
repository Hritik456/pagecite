"""Eval cost accounting: the price table lives in apps/api/retrieval/pricing.py (the API's spend cap
uses it too); this module re-exports it and adds the per-invocation budget."""

from apps.api.retrieval.pricing import (  # noqa: F401  (re-exported)
    PRICES_PER_1M,
    Price,
    UnknownModelPrice,
    cost_of_calls,
    cost_usd,
    price_for,
)


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
