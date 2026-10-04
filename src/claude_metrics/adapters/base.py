from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from claude_metrics.config import Settings
from claude_metrics.domain import NormalizedEvent, SourceRef

ADAPTER_CONTRACT_VERSION = "usage-adapter/1"


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    agent_type: str


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """Transient raw input: never serialize this object into the database."""

    payload: Mapping[str, object]
    source: SourceRef


class AgentAdapter(Protocol):
    """Frozen v1 boundary; see docs/data-contract.md for evidence and identity rules.

    Discovery and parsing never price, persist raw content, or decide cross-file
    ownership. The shared ingestion core validates and projects normalized events.
    Only the Claude implementation is supported; no plugin loader is implied.
    """

    def discover(self, config: Settings) -> Iterable[SourceFile]: ...

    def parse(self, record: SourceRecord) -> Iterable[NormalizedEvent]: ...


class JSONLAdapter(AgentAdapter, Protocol):
    """Extra lifecycle required by the resumable JSONL reader, separate from v1 events.

    Context must contain content-free, JSON-serializable string identifiers only.
    The reader checkpoints it after a file and restores it on a rejected record.
    Each file gets a new adapter instance; discovery state is never shared with parsing.
    """

    context: dict[str, str]
    discovery_warnings: list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class JSONLSource:
    agent_type: str
    parser_version: str
    factory: Callable[[dict[str, str] | None], JSONLAdapter]

    def __post_init__(self) -> None:
        if not self.agent_type or not self.parser_version:
            raise ValueError("source requires an agent type and parser version")
