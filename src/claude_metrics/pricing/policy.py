"""Shared policy defaults and canonical serialization for immutable pricing receipts."""

import hashlib
import json
from dataclasses import asdict, dataclass

from claude_metrics.domain import counter

DEFAULT_COST_MODE = "auto"
DEFAULT_DISCREPANCY_TOLERANCE_NANOS = 1000


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class PricingPolicy:
    assume_provider: str | None = None
    assume_cache_5m: bool = False
    discrepancy_tolerance_nanos: int = DEFAULT_DISCREPANCY_TOLERANCE_NANOS

    def __post_init__(self):
        if self.assume_provider not in (None, "anthropic"):
            raise ValueError("only an explicit Anthropic API-equivalent assumption is supported")
        if type(self.assume_cache_5m) is not bool:
            raise ValueError("assume_cache_5m must be boolean")
        counter(self.discrepancy_tolerance_nanos, "discrepancy_tolerance_nanos")
        if self.discrepancy_tolerance_nanos is None:
            raise ValueError("discrepancy tolerance is required")

    @property
    def key(self) -> str:
        return hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()


# Product default, not a change to evidence or historical policy deserialization.
# Keep PricingPolicy() strict so old frozen receipts and pure callers retain semantics.
AUTO_POLICY = PricingPolicy(assume_provider="anthropic", assume_cache_5m=True)


def policy_for_mode(
    mode: str = DEFAULT_COST_MODE,
    *,
    assume_provider: str | None = None,
    assume_cache_5m: bool = False,
    discrepancy_tolerance_nanos: int = DEFAULT_DISCREPANCY_TOLERANCE_NANOS,
) -> PricingPolicy:
    """Resolve CLI policy choices without changing strict receipt deserialization."""
    if mode not in ("auto", "strict"):
        raise ValueError("cost mode must be auto or strict")
    base = AUTO_POLICY if mode == "auto" else PricingPolicy()
    return PricingPolicy(
        assume_provider or base.assume_provider,
        assume_cache_5m or base.assume_cache_5m,
        discrepancy_tolerance_nanos,
    )


def default_policy() -> PricingPolicy:
    """Use the same product default for CLI, web startup and direct pricing calls."""
    return policy_for_mode(DEFAULT_COST_MODE)
