"""Append-only receipts and explicit repricing; shared writer lock with ingestion."""

import json
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

from claude_metrics.config import Settings
from claude_metrics.ingestion.locking import writer_lock
from claude_metrics.operations import operation_log
from claude_metrics.pricing.catalog import Catalog, load_catalog
from claude_metrics.pricing.policy import PricingPolicy, canonical, default_policy
from claude_metrics.pricing.store import archived_catalog
from claude_metrics.runtime import Runtime, default_runtime
from claude_metrics.storage import connect, migrate


def register_catalog(conn, catalog: Catalog) -> None:
    meta = catalog.metadata
    existing = conn.execute(
        "SELECT sha256 FROM price_catalog_versions WHERE version=?", (meta["version"],)
    ).fetchone()
    if existing and existing[0] != meta["sha256"]:
        raise ValueError("catalog version already exists with a different checksum")
    conn.execute(
        """INSERT INTO price_catalog_versions(version,source_url,source_commit,sha256,retrieved_at)
        VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING""",
        tuple(
            meta[k] for k in ("version", "source_url", "source_commit", "sha256", "retrieved_at")
        ),
    )


def apply_pricing(
    settings: Settings,
    *,
    policy: PricingPolicy | None = None,
    reprice: bool = False,
    catalog: Catalog | None = None,
    start_us: int = 0,
    end_us: int = 2**63 - 1,
    runtime: Runtime | None = None,
) -> dict:
    with writer_lock(settings.data_dir):
        return _apply_pricing_locked(
            settings,
            policy=policy,
            reprice=reprice,
            catalog=catalog,
            start_us=start_us,
            end_us=end_us,
            runtime=runtime,
        )


def _apply_pricing_locked(
    settings: Settings,
    *,
    policy: PricingPolicy | None = None,
    reprice: bool = False,
    catalog: Catalog | None = None,
    start_us: int = 0,
    end_us: int = 2**63 - 1,
    runtime: Runtime | None = None,
    preserve_revision_policy: bool = False,
    revision_policies: dict[int, PricingPolicy] | None = None,
) -> dict:
    """Caller must hold writer_lock, including when composing scan and pricing."""
    started = perf_counter()
    runtime = default_runtime() if runtime is None else runtime
    engine = runtime.pricing
    catalog = runtime.catalog_loader(settings.data_dir) if catalog is None else catalog
    policy = default_policy() if policy is None else policy
    if end_us <= start_us:
        raise ValueError("pricing interval is empty or reversed")
    with connect(settings.database) as conn:
        migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            register_catalog(conn, catalog)
            added = reused = preserved = policy_required = 0
            for row in conn.execute(
                """SELECT r.*,o.observation_id,o.source_file_id,o.generation,o.source_line,
                o.byte_offset,o.parser_version FROM requests r JOIN request_observations o
                ON r.request_pk=o.request_pk AND o.is_current_winner=1
                JOIN sessions s ON s.session_pk=r.session_pk
                WHERE s.agent_type=? AND r.superseded_by IS NULL
                AND r.occurred_at_us>=? AND r.occurred_at_us<?
                ORDER BY r.request_pk""",
                (engine.AGENT_TYPE, start_us, end_us),
            ).fetchall():
                current = conn.execute(
                    "SELECT * FROM cost_receipts WHERE request_pk=? AND is_current=1",
                    (row["request_pk"],),
                ).fetchone()
                if current and current["request_revision"] == row["revision"] and not reprice:
                    preserved += 1
                    continue
                request_policy = policy
                if preserve_revision_policy and not reprice:
                    previous = conn.execute(
                        "SELECT snapshot_json FROM cost_receipts WHERE request_pk=? "
                        "ORDER BY receipt_id DESC LIMIT 1",
                        (row["request_pk"],),
                    ).fetchone()
                    policies = conn.execute(
                        "SELECT count(DISTINCT policy_key) FROM cost_receipts WHERE request_pk=?",
                        (row["request_pk"],),
                    ).fetchone()[0]
                    if row["request_pk"] in (revision_policies or {}):
                        request_policy = revision_policies[row["request_pk"]]
                    elif policies > 1:
                        # A separate CLI scan may have invalidated the selected receipt.
                        # Receipt creation order does not prove the last selected policy.
                        policy_required += 1
                        continue
                    elif previous:
                        request_policy = PricingPolicy(**json.loads(previous[0])["policy"])
                units = dict(
                    conn.execute(
                        "SELECT unit_type,quantity FROM billable_units WHERE observation_id=?",
                        (row["observation_id"],),
                    ).fetchall()
                )
                inputs = engine.pricing_input(dict(row), units)
                payload = engine.calculate(inputs, catalog, request_policy)
                snapshot = {
                    "formula_version": engine.FORMULA_VERSION,
                    "catalog_version": catalog.metadata["version"],
                    "catalog_sha256": catalog.metadata["sha256"],
                    "policy": asdict(request_policy),
                    "input": inputs,
                    "result": payload,
                    "evidence": {
                        k: row[k]
                        for k in (
                            "request_pk",
                            "revision",
                            "observation_id",
                            "source_file_id",
                            "generation",
                            "source_line",
                            "byte_offset",
                            "parser_version",
                        )
                    },
                }
                existing = conn.execute(
                    """SELECT receipt_id,snapshot_json FROM cost_receipts WHERE request_pk=?
                    AND request_revision=? AND catalog_version=? AND formula_version=?
                    AND policy_key=?""",
                    (
                        row["request_pk"],
                        row["revision"],
                        catalog.metadata["version"],
                        engine.FORMULA_VERSION,
                        request_policy.key,
                    ),
                ).fetchone()
                conn.execute(
                    "UPDATE cost_receipts SET is_current=0 WHERE request_pk=?", (row["request_pk"],)
                )
                serialized = canonical(snapshot)
                if existing:
                    if existing["snapshot_json"] != serialized:
                        raise ValueError(
                            "receipt inputs changed without a new request/formula version"
                        )
                    conn.execute(
                        "UPDATE cost_receipts SET is_current=1 WHERE receipt_id=?",
                        (existing["receipt_id"],),
                    )
                    reused += 1
                else:
                    conn.execute(
                        """INSERT INTO cost_receipts(request_pk,request_revision,observation_id,
                        catalog_version,formula_version,policy_key,price_key,resolution_method,
                        rates_json,rules_json,calculated_cost_nanos,reported_cost_nanos,
                        effective_cost_nanos,effective_source,status,estimates_json,snapshot_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["request_pk"],
                            row["revision"],
                            row["observation_id"],
                            catalog.metadata["version"],
                            engine.FORMULA_VERSION,
                            request_policy.key,
                            payload["price_key"],
                            payload["resolution_method"],
                            canonical(payload["rates"]),
                            canonical(payload["rules"]),
                            payload["calculated_cost_nanos"],
                            payload["reported_cost_nanos"],
                            payload["effective_cost_nanos"],
                            payload["effective_source"],
                            payload["status"],
                            canonical(payload["estimates"]),
                            serialized,
                        ),
                    )
                    added += 1
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    operation_log(
        settings.data_dir,
        "pricing_complete",
        receipts_added=added,
        current_receipts_preserved=preserved,
        policy_required_requests=policy_required,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )
    return {
        "ok": True,
        "receipts_added": added,
        "receipts_reused": reused,
        "current_receipts_preserved": preserved,
        "policy_required_requests": policy_required,
        "catalog_version": catalog.metadata["version"],
        "formula_version": engine.FORMULA_VERSION,
        "policy": asdict(policy),
    }


def verify_receipts(
    conn,
    catalog: Catalog | None = None,
    *,
    data_dir: Path | None = None,
    runtime: Runtime | None = None,
) -> dict:
    """Read-only audit: frozen-rate recomputation plus full replay against the matching catalog."""
    runtime = default_runtime() if runtime is None else runtime
    engine = runtime.pricing
    catalog = load_catalog() if catalog is None else catalog
    catalogs = {catalog.metadata["version"]: catalog}
    checked = 0
    failures, other_catalogs = [], []
    for row in conn.execute("SELECT * FROM cost_receipts ORDER BY receipt_id"):
        checked += 1
        try:
            snapshot = json.loads(row["snapshot_json"])
            if (
                snapshot["formula_version"] not in engine.SUPPORTED_FORMULAS
                or snapshot["formula_version"] != row["formula_version"]
                or snapshot["catalog_version"] != row["catalog_version"]
                or PricingPolicy(**snapshot["policy"]).key != row["policy_key"]
                or snapshot["evidence"]["request_pk"] != row["request_pk"]
                or snapshot["evidence"]["revision"] != row["request_revision"]
                or snapshot["evidence"]["observation_id"] != row["observation_id"]
            ):
                raise ValueError("receipt_metadata_mismatch")
            payload = snapshot["result"]
            version = snapshot["catalog_version"]
            if version not in catalogs and data_dir is not None:
                catalogs[version] = archived_catalog(data_dir, version)
            matching = catalogs.get(version)
            if engine.recompute_nanos(payload) != row["calculated_cost_nanos"]:
                raise ValueError("calculation_mismatch")
            for key in (
                "status",
                "effective_cost_nanos",
                "reported_cost_nanos",
                "effective_source",
                "price_key",
                "resolution_method",
            ):
                if row[key] != payload[key]:
                    raise ValueError("projection_mismatch")
            for column, key in (
                ("rates_json", "rates"),
                ("rules_json", "rules"),
                ("estimates_json", "estimates"),
            ):
                if json.loads(row[column]) != payload[key]:
                    raise ValueError("projection_mismatch")
            if matching is not None and snapshot["catalog_sha256"] == matching.metadata["sha256"]:
                replayed = engine.calculate(
                    snapshot["input"],
                    matching,
                    PricingPolicy(**snapshot["policy"]),
                    formula_version=snapshot["formula_version"],
                )
                if canonical(replayed) != canonical(payload):
                    raise ValueError("catalog_replay_mismatch")
            elif matching is not None:
                raise ValueError("catalog_checksum_mismatch")
            else:
                other_catalogs.append(row["receipt_id"])
        except (ValueError, KeyError, TypeError, ArithmeticError):
            failures.append(row["receipt_id"])
    return {
        "ok": not failures,
        "checked": checked,
        "failed_receipt_ids": failures,
        "frozen_rates_only_receipt_ids": other_catalogs,
    }
