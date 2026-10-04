"""Consistent backups and an allowlisted content-free support report."""

import hashlib
import json
import platform
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from claude_metrics import __version__
from claude_metrics.config import Settings
from claude_metrics.ingestion.locking import writer_lock
from claude_metrics.operations import durable_write, operation_log, publish_directory
from claude_metrics.pricing.store import archived_catalog, copy_catalog
from claude_metrics.runtime import default_runtime
from claude_metrics.storage import connect, inspect_database


def _target(settings: Settings, destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError("destination exists; choose a new backup/support directory")
    if any(destination.is_relative_to(root.resolve()) for root in settings.source_dirs):
        raise ValueError("exports must not be placed inside Claude source directories")
    if destination == settings.database.resolve():
        raise ValueError("export must not replace the ledger")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def backup(settings: Settings, destination: Path) -> dict:
    """SQLite backup API includes committed WAL data; copying the live .db alone does not."""
    destination = _target(settings, destination)
    with (
        writer_lock(settings.data_dir),
        tempfile.TemporaryDirectory(prefix=".backup-stage-", dir=destination.parent) as temporary,
    ):
        staged = Path(temporary) / "backup"
        staged.mkdir()
        with connect(settings.database, readonly=True) as source:
            if not inspect_database(source)["ok"]:
                raise ValueError("source ledger failed integrity/schema checks")
            copied = sqlite3.connect(staged / "usage.sqlite3", isolation_level=None)
            try:
                source.backup(copied, pages=256)
                copied.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                copied.execute("PRAGMA journal_mode=DELETE")  # portable single-file artifact
                if copied.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("backup integrity check failed")
            finally:
                copied.close()
        with connect(staged / "usage.sqlite3", readonly=True) as restored:
            versions = {
                r[0] for r in restored.execute("SELECT version FROM price_catalog_versions")
            }
        active = default_runtime().catalog_loader(settings.data_dir)
        versions.add(active.metadata["version"])
        catalog_dir = staged / "pricing"
        catalog_dir.mkdir()
        for version in sorted(versions):
            copy_catalog(archived_catalog(settings.data_dir, version), catalog_dir / version)
        durable_write(
            catalog_dir / "active.json",
            json.dumps(
                {
                    "version": active.metadata["version"],
                    "sha256": active.metadata["sha256"],
                },
                sort_keys=True,
            ).encode(),
        )
        hashes = {
            str(path.relative_to(staged)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in staged.rglob("*")
            if path.is_file()
        }
        durable_write(
            staged / "manifest.json",
            json.dumps(
                {
                    "format": "claude-metrics-backup/1",
                    "created_at": datetime.now(UTC).isoformat(),
                    "app_version": __version__,
                    "files_sha256": hashes,
                    "privacy": "Private ledger: contains paths, project/session IDs and usage.",
                },
                indent=2,
            ).encode(),
        )
        publish_directory(staged, destination)
        operation_log(settings.data_dir, "backup_complete")
    return {"ok": True, "directory": str(destination), "files": len(hashes)}


def support_bundle(settings: Settings, destination: Path) -> dict:
    """No source paths, IDs, model names, usage totals, receipts, raw content or logs."""
    destination = _target(settings, destination)
    runtime = default_runtime()
    catalog = runtime.catalog_loader(settings.data_dir)
    report = {
        "format": "claude-metrics-support/1",
        "app_version": __version__,
        "python": platform.python_version(),
        "os": platform.system(),
        "sqlite": sqlite3.sqlite_version,
        "parser": runtime.source.parser_version,
        "formula": runtime.pricing.FORMULA_VERSION,
        "catalog_sha256": catalog.metadata["sha256"],
        "database_present": settings.database.is_file(),
    }
    if report["database_present"]:
        with connect(settings.database, readonly=True) as conn:
            conn.execute("BEGIN")
            health = inspect_database(conn)
            report["database"] = {
                k: health[k]
                for k in (
                    "ok",
                    "schema_version",
                    "schema_current",
                    "foreign_key_errors",
                    "journal_mode",
                )
            }
            report["counts"] = {
                name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                for name in ("requests", "sessions", "source_files", "cost_receipts")
            }
            report["scan_states"] = {
                state: conn.execute(
                    "SELECT count(*) FROM ingestion_runs WHERE status=?", (state,)
                ).fetchone()[0]
                for state in ("running", "complete", "failed")
            }
    with tempfile.TemporaryDirectory(prefix=".support-stage-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "support"
        staged.mkdir()
        durable_write(staged / "support.json", json.dumps(report, indent=2).encode())
        publish_directory(staged, destination)
    return {"ok": True, "directory": str(destination), "contains_private_ledger": False}
