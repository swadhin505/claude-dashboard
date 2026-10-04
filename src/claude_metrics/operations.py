"""Small, content-free local operation logs and safe export publication."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

EVENTS = {"scan_complete", "scan_failed", "pricing_complete", "catalog_updated", "backup_complete"}
FIELDS = {
    "files_seen",
    "files_processed",
    "files_unchanged",
    "parsed_records",
    "malformed_rows",
    "rows_added",
    "rows_revised",
    "failed_files",
    "deferred_files",
    "receipts_added",
    "current_receipts_preserved",
    "policy_required_requests",
    "elapsed_ms",
}


def operation_log(data_dir: Path, event: str, **values) -> None:
    """Caller holds writer lock. Logging failure never rolls back successful accounting.

    Only fixed event names and non-negative integer counters can reach disk. No
    arbitrary message/exception/path/identifier can enter these rotating local logs.
    """
    if event not in EVENTS:
        raise ValueError("unknown operation event")
    record = {"at": datetime.now(UTC).isoformat(), "event": event}
    record.update({k: v for k, v in values.items() if k in FIELDS and type(v) is int and v >= 0})
    try:
        directory = data_dir / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        current = directory / "operations.jsonl"
        if current.exists() and current.stat().st_size >= 1_048_576:
            older = directory / "operations.1.jsonl"
            if older.exists():
                os.replace(older, directory / "operations.2.jsonl")
            os.replace(current, older)
        with current.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass  # diagnostics must not corrupt or misreport a committed scan


def durable_write(path: Path, data: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def publish_directory(staged: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError("destination already exists; choose a new directory")
    staged.rename(destination)  # same-volume publication; caller owns the staging directory
