"""HTTP contracts. Exact integers stay integers; the browser displays server-formatted values."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    start: date | None = Field(None, alias="from")
    end: date | None = Field(None, alias="to")
    tz: str = Field(max_length=100)
    project: str | None = Field(None, max_length=1024)
    model: str | None = Field(None, max_length=200)
    cursor: int = Field(0, ge=0, le=2**63 - 1)
    limit: int = Field(30, ge=1, le=200)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Aggregate(Contract):
    requests: int
    sessions: int
    cost_status: Literal["COMPLETE", "PARTIAL", "UNPRICED"]
    known_cost_nanos: int
    total_cost_nanos: int | None
    complete_requests: int
    partial_requests: int
    unpriced_requests: int
    estimated_requests: int
    reported_complete_requests: int
    tokens: dict[str, int]
    known_tokens: int
    all_tokens: int | None
    incomplete_token_requests: int
    priced_tokens: int
    unpriced_tokens: int
    unknown_counters: dict[str, int]
    unpriced_units: dict[str, int]
    warnings: dict[str, int]


class Group(Aggregate):
    key: str
    label: str | None = None


class Overview(Contract):
    timezone: str
    from_: str | None = Field(alias="from")
    to: str | None
    total: Aggregate
    daily: list[Group]
    models: list[Group]
    projects: list[Group]
    sources: list[Group]
    group_counts: dict[str, int]
    next_cursor: int | None


class Session(Aggregate):
    session_pk: int
    session_id: str
    project_id: str | None
    project_name: str | None
    first_selected_request_us: int
    last_selected_request_us: int
    models: list[str]
    sources: dict[str, Aggregate]


class Sessions(Contract):
    items: list[Session]
    next_cursor: int | None


class Detail(Contract):
    session_pk: int
    total: Aggregate
    requests: list[dict[str, JsonValue]]
    next_cursor: int | None
    tool_summary: list[dict[str, JsonValue]]


class Health(Contract):
    last_complete_scan: dict[str, JsonValue] | None
    last_failed_scan: dict[str, JsonValue] | None
    current_receipts: dict[str, int]
    receipt_history_count: int
    low_confidence_requests: int
    sources: list[dict[str, JsonValue]]
    catalogs: list[dict[str, JsonValue]]
    formula_versions: list[str]
    historical_diagnostics_count: int
    recent_diagnostics: list[dict[str, JsonValue]]
    warnings: dict[str, int]
    incomplete_models: list[dict[str, JsonValue]]
    group_counts: dict[str, int]
    next_cursor: int | None
    roots: list[dict[str, str]]
    parser_version: str
    formula_version: str
    active_catalog: dict[str, JsonValue]
    new_request_policy: dict[str, JsonValue]
    last_refresh: dict[str, JsonValue] | None


class ScanResult(Contract):
    ok: bool
    status: Literal["complete", "busy", "failed"]
    message: str | None = None
    scan: dict[str, JsonValue] | None = None
    pricing: dict[str, JsonValue] | None = None
    policy: dict[str, JsonValue] | None = None
