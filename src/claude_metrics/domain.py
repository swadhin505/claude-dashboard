"""Content-free normalized contracts. No filesystem, database, or vendor parsing."""

from dataclasses import dataclass, fields
from enum import StrEnum

SQLITE_MAX_INT = 2**63 - 1

# Exclusive, persisted token buckets. Reasoning is a subset of output, not a seventh bucket.
TOKEN_COLUMNS = (
    "input_uncached_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cache_write_unknown_tokens",
    "output_tokens",
)


def counter(value: int | None, name: str) -> None:
    if value is not None and (type(value) is not int or not 0 <= value <= SQLITE_MAX_INT):
        raise ValueError(f"{name} must be a non-negative signed-64-bit integer or None")


class QuerySource(StrEnum):
    MAIN = "main"
    SUBAGENT = "subagent"
    AUXILIARY = "auxiliary"
    UNKNOWN = "unknown"


class CostStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNPRICED = "UNPRICED"


class BillingSurface(StrEnum):
    API = "api"
    SUBSCRIPTION = "subscription"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    input_uncached_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    # Legacy combined writes stay counted without inventing their TTL.
    cache_write_unknown_tokens: int | None = None
    output_tokens: int | None = None
    # A breakdown of output, never added to the total again.
    reasoning_tokens: int | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            counter(getattr(self, field.name), field.name)
        if (
            self.reasoning_tokens is not None
            and self.output_tokens is not None
            and self.reasoning_tokens > self.output_tokens
        ):
            raise ValueError("reasoning_tokens cannot exceed inclusive output_tokens")

    @property
    def input_total(self) -> int | None:
        values = (
            self.input_uncached_tokens,
            self.cache_read_tokens,
            self.cache_write_5m_tokens,
            self.cache_write_1h_tokens,
            self.cache_write_unknown_tokens,
        )
        return None if None in values else sum(values)  # type: ignore[arg-type]

    @property
    def all_tokens(self) -> int | None:
        total = self.input_total
        return None if total is None or self.output_tokens is None else total + self.output_tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRef:
    path: str
    line: int
    byte_offset: int
    generation: int
    parser_version: str

    def __post_init__(self) -> None:
        for name in ("line", "byte_offset", "generation"):
            counter(getattr(self, name), name)
        if self.line < 1 or self.generation < 1 or not self.path or not self.parser_version:
            raise ValueError("source requires a path, parser version, and positive line/generation")


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    agent_type: str
    project_id: str
    canonical_root: str
    display_name: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    agent_type: str
    session_id: str
    source: SourceRef
    project_id: str | None = None
    parent_session_id: str | None = None
    started_at_us: int | None = None
    ended_at_us: int | None = None
    cwd: str | None = None
    branch: str | None = None
    worktree: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Turn:
    agent_type: str
    session_id: str
    turn_id: str
    source: SourceRef
    prompt_id: str | None = None
    started_at_us: int | None = None
    ended_at_us: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Request:
    agent_type: str
    session_id: str
    occurred_at_us: int
    model_raw: str
    tokens: TokenUsage
    source: SourceRef
    request_id: str | None = None
    message_id: str | None = None
    turn_id: str | None = None
    model_resolved: str | None = None
    provider: str = "unknown"
    provider_region: str | None = None
    inference_geo: str | None = None
    query_source: QuerySource = QuerySource.UNKNOWN
    agent_id: str | None = None
    parent_agent_id: str | None = None
    speed: str | None = None
    service_tier: str | None = None
    effort: str | None = None
    billing_surface: BillingSurface = BillingSurface.UNKNOWN
    reported_cost_nanos: int | None = None
    reported_cost_original: str | None = None
    reported_cost_unit: str | None = None
    is_final: bool | None = None
    # Set only for an explicitly marked copied record, never inferred from content.
    replay_of_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.agent_type or not self.session_id or not self.model_raw:
            raise ValueError("request requires agent, session, and raw model identifiers")
        counter(self.occurred_at_us, "occurred_at_us")
        counter(self.reported_cost_nanos, "reported_cost_nanos")


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall:
    agent_type: str
    session_id: str
    tool_call_id: str
    name: str
    source: SourceRef
    request_id: str | None = None
    turn_id: str | None = None
    success: bool | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        counter(self.duration_ms, "duration_ms")


@dataclass(frozen=True, slots=True, kw_only=True)
class BillableUnit:
    agent_type: str
    session_id: str
    unit_type: str
    quantity: int
    source: SourceRef
    request_id: str | None = None

    def __post_init__(self) -> None:
        counter(self.quantity, "quantity")


type NormalizedEvent = Project | Session | Turn | Request | ToolCall | BillableUnit
