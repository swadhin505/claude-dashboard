"""Compose the existing writers without a lock gap or implicit repricing."""

import json
import sqlite3
from dataclasses import asdict

from claude_metrics.config import Settings
from claude_metrics.ingestion.locking import ScanBusy, writer_lock
from claude_metrics.ingestion.scanner import _scan_locked
from claude_metrics.pricing.catalog import Catalog
from claude_metrics.pricing.policy import PricingPolicy
from claude_metrics.pricing.receipts import _apply_pricing_locked, register_catalog
from claude_metrics.runtime import Runtime, default_runtime
from claude_metrics.storage import connect, migrate


def initialize(
    settings: Settings,
    catalog: Catalog | None = None,
    *,
    runtime: Runtime | None = None,
) -> dict:
    """Shared CLI/web initialization; validate the selected catalog before ledger changes."""
    runtime = default_runtime() if runtime is None else runtime
    with writer_lock(settings.data_dir):
        catalog = runtime.catalog_loader(settings.data_dir) if catalog is None else catalog
        with connect(settings.database) as conn:
            version = migrate(conn)
            register_catalog(conn, catalog)
    return {"ok": True, "database": str(settings.database), "schema_version": version}


def refresh(
    settings: Settings,
    catalog: Catalog,
    policy: PricingPolicy,
    *,
    runtime: Runtime | None = None,
) -> dict:
    runtime = default_runtime() if runtime is None else runtime
    stage = "scan"
    try:
        with writer_lock(settings.data_dir):
            with connect(settings.database) as conn:
                migrate(conn)
                selected = {
                    r["request_pk"]: PricingPolicy(**json.loads(r["snapshot_json"])["policy"])
                    for r in conn.execute(
                        "SELECT request_pk,snapshot_json FROM cost_receipts WHERE is_current=1"
                    )
                }
            scanned = _scan_locked(settings, full=False, source=runtime.source)
            stage = "pricing"
            priced = _apply_pricing_locked(
                settings,
                catalog=catalog,
                policy=policy,
                preserve_revision_policy=True,
                revision_policies=selected,
                runtime=runtime,
            )
            return {
                "ok": scanned["ok"] and not priced["policy_required_requests"],
                "status": "complete",
                "scan": scanned,
                "pricing": priced,
                "policy": asdict(policy),
            }
    except ScanBusy:
        return {
            "ok": False,
            "status": "busy",
            "message": "Another writer is active. Retry shortly.",
        }
    except (OSError, ValueError, sqlite3.Error):
        # Error messages can contain local content. Do not serialize the exception.
        return {
            "ok": False,
            "status": "failed",
            "message": f"{stage.capitalize()} failed. Retained data is safe; check Data health "
            "and run claude-metrics doctor. New revisions may still need pricing.",
        }
