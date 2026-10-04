"""The single composition point for the supported source, pricing and catalog.

Use explicit Runtime values for tests/integrations. No dynamic plugins or global
mutable registry. Production remains Claude-only; multiple roots use one adapter.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_metrics.adapters.base import JSONLSource
from claude_metrics.adapters.claude import PARSER_VERSION, ClaudeAdapter
from claude_metrics.pricing import calculator
from claude_metrics.pricing.base import PricingEngine
from claude_metrics.pricing.catalog import Catalog
from claude_metrics.pricing.store import active_catalog


@dataclass(frozen=True, slots=True)
class Runtime:
    source: JSONLSource
    pricing: PricingEngine
    catalog_loader: Callable[[Path], Catalog]

    def __post_init__(self) -> None:
        if self.source.agent_type != self.pricing.AGENT_TYPE:
            raise ValueError("source and pricing engine must target the same agent type")
        if self.pricing.FORMULA_VERSION not in self.pricing.SUPPORTED_FORMULAS:
            raise ValueError("pricing engine must replay its current formula")


def default_runtime() -> Runtime:
    return Runtime(
        source=JSONLSource("claude", PARSER_VERSION, ClaudeAdapter),
        pricing=calculator,
        catalog_loader=active_catalog,
    )
