"""Small calculation/replay contract consumed by receipt persistence.

A module can implement this protocol; no base class, registry or container is needed.
Implementations must preserve old formula replay and the normalized receipt shape.
"""

from typing import Protocol

from claude_metrics.pricing.catalog import Catalog
from claude_metrics.pricing.policy import PricingPolicy


class PricingEngine(Protocol):
    AGENT_TYPE: str
    FORMULA_VERSION: str
    SUPPORTED_FORMULAS: set[str]

    def pricing_input(self, request: dict, units: dict[str, int]) -> dict: ...

    def calculate(
        self,
        inputs: dict,
        catalog: Catalog,
        policy: PricingPolicy | None = None,
        *,
        formula_version: str = ...,
    ) -> dict: ...

    def recompute_nanos(self, payload: dict) -> int | None: ...
