"""Read-only reports over the same canonical request set; no session-lifetime leakage."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from claude_metrics.domain import TOKEN_COLUMNS
from claude_metrics.units import EPOCH, date_bounds


@dataclass(frozen=True)
class ReportFilter:
    timezone: str
    start: date | None = None
    end: date | None = None
    project: str | None = None
    model: str | None = None

    def where(self, session_pk: int | None = None) -> tuple[str, list]:
        ZoneInfo(self.timezone)
        if (self.start is None) != (self.end is None):
            raise ValueError("supply both --from and --to, or neither for all retained history")
        conditions, parameters = ["r.superseded_by IS NULL"], []
        if self.start is not None:
            start, end = date_bounds(self.start, self.end, self.timezone)
            conditions += ["r.occurred_at_us>=?", "r.occurred_at_us<?"]
            parameters += [start, end]
        for name, value in (
            ("s.project_id", self.project),
            ("r.model_raw", self.model),
            ("r.session_pk", session_pk),
        ):
            if value is not None:
                conditions.append(f"{name}=?")
                parameters.append(value)
        return " AND ".join(conditions), parameters


def _rows(conn, filters: ReportFilter, session_pk: int | None = None):
    where, params = filters.where(session_pk)
    return conn.execute(
        """SELECT r.*,s.session_id,s.project_id,p.display_name AS project_name,
        c.receipt_id,c.snapshot_json,c.status AS cost_status,c.effective_cost_nanos,
        o.observation_id,o.normalized_candidate_json
        FROM requests r JOIN sessions s USING(session_pk)
        LEFT JOIN projects p USING(project_id)
        LEFT JOIN request_observations o ON o.request_pk=r.request_pk AND o.is_current_winner=1
        LEFT JOIN cost_receipts c ON c.request_pk=r.request_pk AND c.is_current=1
            AND c.request_revision=r.revision WHERE """
        + where
        + " ORDER BY r.request_pk",
        params,
    )


def _cost(row) -> dict:
    if row["snapshot_json"]:
        snapshot = json.loads(row["snapshot_json"])
        if "result" in snapshot:
            return snapshot["result"]
    # A missing or stale receipt is pending/unpriced, never a zero-cost request.
    tokens = {k: row[k] for k in TOKEN_COLUMNS}
    obs = json.loads(row["normalized_candidate_json"] or "{}")
    return {
        "effective_cost_nanos": None,
        "status": "UNPRICED",
        "estimates": [],
        "warnings": [],
        "missing": ["pricing_pending"],
        "coverage": {
            "priced_tokens": 0,
            "unpriced_tokens": sum(v or 0 for v in tokens.values()),
            "unknown_counters": [k for k, v in tokens.items() if v is None],
            "unpriced_units": {k: v for k, v in obs.get("billable_units", {}).items() if v},
            "reported_scope_complete": False,
        },
    }


class Aggregate:
    def __init__(self):
        self.requests = self.known_cost = self.priced_tokens = self.unpriced_tokens = 0
        self.estimated = self.reported = self.incomplete = 0
        self.tokens, self.unknown, self.units, self.statuses, self.reasons = (
            Counter(),
            Counter(),
            Counter(),
            Counter(),
            Counter(),
        )
        self.sessions = set()

    def add(self, row, payload: dict) -> None:
        self.requests += 1
        self.sessions.add(row["session_pk"])
        self.statuses[payload["status"]] += 1
        self.known_cost += payload["effective_cost_nanos"] or 0
        coverage = payload["coverage"]
        self.priced_tokens += coverage["priced_tokens"]
        self.unpriced_tokens += coverage["unpriced_tokens"]
        self.units.update(coverage["unpriced_units"])
        self.unknown.update(coverage["unknown_counters"])
        self.incomplete += bool(coverage["unknown_counters"])
        self.estimated += bool(payload["estimates"])
        self.reported += coverage["reported_scope_complete"]
        self.reasons.update(payload["missing"])
        self.reasons.update(payload["warnings"])
        for key in TOKEN_COLUMNS:
            self.tokens[key] += row[key] or 0

    def result(self) -> dict:
        status = (
            "COMPLETE"
            if self.statuses["COMPLETE"] == self.requests
            else ("UNPRICED" if self.statuses["UNPRICED"] == self.requests else "PARTIAL")
        )
        known_tokens = sum(self.tokens.values())
        return {
            "requests": self.requests,
            "sessions": len(self.sessions),
            "cost_status": status,
            "known_cost_nanos": self.known_cost,
            "total_cost_nanos": self.known_cost if status == "COMPLETE" else None,
            "complete_requests": self.statuses["COMPLETE"],
            "partial_requests": self.statuses["PARTIAL"],
            "unpriced_requests": self.statuses["UNPRICED"],
            "estimated_requests": self.estimated,
            "reported_complete_requests": self.reported,
            "tokens": {k: self.tokens[k] for k in TOKEN_COLUMNS},
            "known_tokens": known_tokens,
            "all_tokens": None if self.incomplete else known_tokens,
            "incomplete_token_requests": self.incomplete,
            "priced_tokens": self.priced_tokens,
            "unpriced_tokens": self.unpriced_tokens,
            "unknown_counters": dict(sorted(self.unknown.items())),
            "unpriced_units": dict(sorted(self.units.items())),
            "warnings": dict(sorted(self.reasons.items())),
        }


def overview(conn, filters: ReportFilter) -> dict:
    total = Aggregate()
    groups = {"daily": {}, "models": {}, "projects": {}, "sources": {}}
    zone = ZoneInfo(filters.timezone)
    for row in _rows(conn, filters):
        cost = _cost(row)
        total.add(row, cost)
        day = (
            (EPOCH + timedelta(microseconds=row["occurred_at_us"]))
            .astimezone(zone)
            .date()
            .isoformat()
        )
        for group, key in (
            ("daily", day),
            ("models", row["model_raw"]),
            ("projects", row["project_id"] or "unknown"),
            ("sources", row["query_source"]),
        ):
            groups[group].setdefault(key, Aggregate()).add(row, cost)
    return {
        "timezone": filters.timezone,
        "from": str(filters.start) if filters.start else None,
        "to": str(filters.end) if filters.end else None,
        "total": total.result(),
        **{
            group: [{"key": key, **value.result()} for key, value in sorted(values.items())]
            for group, values in groups.items()
        },
    }


def _page(limit: int, after: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 200 or type(after) is not int or after < 0:
        raise ValueError("limit must be 1..200 and cursor must be a non-negative integer")


def sessions(conn, filters: ReportFilter, *, limit: int = 50, after: int = 0) -> dict:
    _page(limit, after)
    where, params = filters.where()
    keys = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT r.session_pk FROM requests r JOIN sessions s USING(session_pk) WHERE "
            + where
            + " AND r.session_pk>? ORDER BY r.session_pk LIMIT ?",
            (*params, after, limit + 1),
        )
    ]
    items = []
    for pk in keys[:limit]:
        total, models, sources = Aggregate(), set(), {}
        first = last = None
        identity = {}
        for row in _rows(conn, filters, pk):
            cost = _cost(row)
            total.add(row, cost)
            models.add(row["model_raw"])
            sources.setdefault(row["query_source"], Aggregate()).add(row, cost)
            first = (
                min(first, row["occurred_at_us"]) if first is not None else row["occurred_at_us"]
            )
            last = max(last, row["occurred_at_us"]) if last is not None else row["occurred_at_us"]
            identity = {
                k: row[k] for k in ("session_pk", "session_id", "project_id", "project_name")
            }
        items.append(
            {
                **identity,
                "first_selected_request_us": first,
                "last_selected_request_us": last,
                "models": sorted(models),
                "sources": {k: v.result() for k, v in sources.items()},
                **total.result(),
            }
        )
    return {"items": items, "next_cursor": keys[limit - 1] if len(keys) > limit else None}


def session_detail(
    conn, filters: ReportFilter, session_pk: int, *, limit: int = 50, after: int = 0
) -> dict:
    _page(limit, after)
    total, items = Aggregate(), []
    more = False
    for row in _rows(conn, filters, session_pk):
        cost = _cost(row)
        total.add(row, cost)
        if row["request_pk"] <= after:
            continue
        if len(items) == limit:
            more = True
            continue
        item = dict(row)
        item.pop("normalized_candidate_json")
        snapshot = item.pop("snapshot_json")
        item["receipt"] = json.loads(snapshot) if snapshot else None
        item["cost"] = cost
        item["source_evidence"] = [
            dict(r)
            for r in conn.execute(
                """SELECT o.observation_id,f.canonical_path,o.source_line,o.byte_offset,
            o.generation,
            o.parser_version,o.superseded,o.source_available,o.is_current_winner
            FROM request_observations o JOIN source_files f ON f.file_id=o.source_file_id
            WHERE o.request_pk=? ORDER BY o.observation_id""",
                (row["request_pk"],),
            )
        ]
        items.append(item)
    where, params = filters.where(session_pk)
    tools = [
        dict(r)
        for r in conn.execute(
            """SELECT t.tool_name,count(*) AS calls,coalesce(sum(t.success=1),0) AS succeeded,
        coalesce(sum(t.success=0),0) AS failed,sum(t.success IS NULL) AS unknown_success
        FROM tool_calls t JOIN requests r USING(request_pk)
        JOIN sessions s ON s.session_pk=r.session_pk
        WHERE """
            + where
            + " GROUP BY t.tool_name ORDER BY t.tool_name",
            params,
        )
    ]
    return {
        "session_pk": session_pk,
        "total": total.result(),
        "requests": items,
        "next_cursor": items[-1]["request_pk"] if more else None,
        "tool_summary": tools,
    }


def data_health(conn) -> dict:
    def latest(status):
        row = conn.execute(
            "SELECT * FROM ingestion_runs WHERE status=? ORDER BY started_at_us DESC LIMIT 1",
            (status,),
        ).fetchone()
        return dict(row) if row else None

    counts = dict(
        conn.execute(
            "SELECT status,count(*) FROM cost_receipts c JOIN requests r USING(request_pk) "
            "WHERE c.is_current=1 AND c.request_revision=r.revision AND r.superseded_by IS NULL "
            "GROUP BY status"
        ).fetchall()
    )
    models, reasons = {}, Counter()
    for row in _rows(conn, ReportFilter("UTC")):
        cost = _cost(row)
        if cost["status"] != "COMPLETE":
            models.setdefault(row["model_raw"], Aggregate()).add(row, cost)
        reasons.update(cost["warnings"])
        reasons.update(cost["missing"])
        reasons.update(cost["estimates"])
    return {
        "last_complete_scan": latest("complete"),
        "last_failed_scan": latest("failed"),
        "current_receipts": counts,
        "receipt_history_count": conn.execute("SELECT count(*) FROM cost_receipts").fetchone()[0],
        "low_confidence_requests": conn.execute(
            "SELECT count(*) FROM requests WHERE "
            "superseded_by IS NULL AND identity_confidence='low'"
        ).fetchone()[0],
        "sources": [
            dict(r)
            for r in conn.execute(
                "SELECT canonical_path,parser_version,generation,size,byte_offset,last_scan_at_us "
                "FROM source_files ORDER BY canonical_path"
            )
        ],
        "catalogs": [dict(r) for r in conn.execute("SELECT * FROM price_catalog_versions")],
        "formula_versions": [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT formula_version FROM cost_receipts ORDER BY formula_version"
            )
        ],
        "historical_diagnostics_count": conn.execute(
            "SELECT count(*) FROM ingestion_diagnostics"
        ).fetchone()[0],
        "recent_diagnostics": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM ingestion_diagnostics ORDER BY diagnostic_id DESC LIMIT 20"
            )
        ],
        "warnings": dict(sorted(reasons.items())),
        "incomplete_models": [{"model": k, **v.result()} for k, v in sorted(models.items())],
    }
