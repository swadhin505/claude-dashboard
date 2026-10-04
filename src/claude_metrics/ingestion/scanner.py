"""Binary JSONL ingestion: per-file transactions, durable history and resumable tails."""

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from claude_metrics.adapters.base import JSONLSource, SourceRecord
from claude_metrics.config import Settings
from claude_metrics.domain import SourceRef
from claude_metrics.ingestion.ledger import Ledger, summary
from claude_metrics.ingestion.locking import ScanBusy, writer_lock
from claude_metrics.operations import operation_log
from claude_metrics.runtime import default_runtime
from claude_metrics.storage import connect, migrate
from claude_metrics.units import utc_microseconds

MAX_LINE_BYTES = 16 * 1024 * 1024


def now() -> int:
    return utc_microseconds(datetime.now(UTC))


def digest(handle, start: int, length: int) -> str:
    handle.seek(start)
    return hashlib.sha256(handle.read(length)).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite_json")


def diagnostic(conn, run_id: str, path: str, line: int | None, code: str) -> None:
    conn.execute(
        "INSERT INTO ingestion_diagnostics(run_id,source_path,source_line,code) VALUES (?,?,?,?)",
        (run_id, path, line, code),
    )


def scan_file(
    conn,
    path: Path,
    run_id: str,
    full: bool,
    parser_version: str,
    source: JSONLSource | None = None,
) -> dict:
    source = default_runtime().source if source is None else source
    old = conn.execute(
        "SELECT * FROM source_files WHERE agent_type=? AND canonical_path=?",
        (source.agent_type, str(path)),
    ).fetchone()
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        file_identity = f"{stat.st_dev}:{stat.st_ino}"
        sample_size = min(4096, stat.st_size)
        snapshot_prefix = digest(handle, 0, sample_size)
        snapshot_tail = digest(handle, stat.st_size - sample_size, sample_size)
        changed = bool(
            old
            and (
                old["file_identity"] != file_identity
                or stat.st_size < old["size"]
                or digest(handle, 0, old["fingerprint_bytes"]) != old["prefix_fingerprint"]
                or digest(handle, max(0, old["byte_offset"] - 4096), min(4096, old["byte_offset"]))
                != old["tail_fingerprint"]
                or (stat.st_size == old["size"] and stat.st_mtime_ns != old["mtime_ns"])
            )
        )
        reparse = full or changed or not old or old["parser_version"] != parser_version
        unchanged = bool(not reparse and old["byte_offset"] == stat.st_size)
        generation = old["generation"] + int(changed) if old else 1
        offset = 0 if reparse else old["byte_offset"]
        line_number = 0 if reparse else old["line_number"]
        context = {} if reparse else json.loads(old["context_json"])
        adapter = source.factory(context)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO source_files(agent_type,canonical_path,parser_version)
                VALUES (?,?,?) ON CONFLICT DO NOTHING""",
                (source.agent_type, str(path), parser_version),
            )
            file_id = conn.execute(
                "SELECT file_id FROM source_files WHERE agent_type=? AND canonical_path=?",
                (source.agent_type, str(path)),
            ).fetchone()[0]
            ledger = Ledger(conn, file_id)
            if changed:
                conn.execute(
                    "UPDATE request_observations SET source_available=0 WHERE source_file_id=?",
                    (file_id,),
                )
            malformed = deferred = parsed = 0
            handle.seek(offset)
            while offset < stat.st_size:
                start = offset
                # Bound memory and this scan's snapshot even when Claude appends concurrently.
                raw = handle.readline(min(MAX_LINE_BYTES + 1, stat.st_size - start))
                if not raw:
                    break
                oversized = len(raw) > MAX_LINE_BYTES
                while oversized and not raw.endswith(b"\n") and handle.tell() < stat.st_size:
                    raw = handle.readline(min(MAX_LINE_BYTES, stat.st_size - handle.tell()))
                    if not raw:
                        break
                if not raw.endswith(b"\n"):
                    deferred = 1
                    break
                offset = handle.tell()
                line_number += 1
                if oversized:
                    diagnostic(conn, run_id, str(path), line_number, "line_too_large")
                    malformed += 1
                    continue
                if not raw.strip():
                    continue
                context_before = dict(adapter.context)
                counts_before = ledger.added, ledger.revised
                conn.execute("SAVEPOINT record")
                try:
                    payload = json.loads(
                        raw.decode("utf-8-sig"), object_pairs_hook=_object, parse_constant=_constant
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("record_not_object")
                    src = SourceRef(
                        path=str(path),
                        line=line_number,
                        byte_offset=start,
                        generation=generation,
                        parser_version=parser_version,
                    )
                    # Materialize first, so invalid usage cannot leave half a record in the ledger.
                    events = list(adapter.parse(SourceRecord(payload=payload, source=src)))
                    if any(event.agent_type != source.agent_type for event in events):
                        raise ValueError("adapter_agent_mismatch")
                    conn.execute(
                        """UPDATE request_observations SET superseded=1 WHERE source_file_id=?
                        AND generation=? AND byte_offset=? AND parser_version!=?""",
                        (file_id, generation, start, parser_version),
                    )
                    ledger.apply(events)
                    conn.execute(
                        """UPDATE request_observations SET source_available=1
                        WHERE source_file_id=? AND generation=? AND byte_offset=?
                        AND parser_version=?""",
                        (file_id, generation, start, parser_version),
                    )
                    conn.execute("RELEASE record")
                    parsed += 1
                except (ValueError, TypeError, OverflowError, RecursionError) as exc:
                    conn.execute("ROLLBACK TO record")
                    conn.execute("RELEASE record")
                    adapter.context = context_before
                    ledger.added, ledger.revised = counts_before
                    # Never retain exception messages, which may contain transcript text.
                    code = (
                        "invalid_json"
                        if isinstance(exc, (json.JSONDecodeError, UnicodeError))
                        else "invalid_record"
                    )
                    diagnostic(conn, run_id, str(path), line_number, code)
                    malformed += 1
            after = os.fstat(handle.fileno())
            current = path.stat()
            if after.st_size < stat.st_size or current.st_ino != stat.st_ino:
                raise OSError("source changed during scan; retry")
            if after.st_size == stat.st_size and after.st_mtime_ns != stat.st_mtime_ns:
                raise OSError("source changed during scan; retry")
            if (
                digest(handle, 0, sample_size) != snapshot_prefix
                or digest(handle, stat.st_size - sample_size, sample_size) != snapshot_tail
            ):
                raise OSError("source changed during scan; retry")
            prefix_bytes = min(4096, offset)
            conn.execute(
                """UPDATE source_files SET file_identity=?,generation=?,size=?,mtime_ns=?,
                byte_offset=?,line_number=?,prefix_fingerprint=?,fingerprint_bytes=?,
                tail_fingerprint=?,parser_version=?,context_json=?,last_scan_at_us=?
                WHERE file_id=?""",
                (
                    file_identity,
                    generation,
                    stat.st_size,
                    stat.st_mtime_ns,
                    offset,
                    line_number,
                    digest(handle, 0, prefix_bytes),
                    prefix_bytes,
                    digest(handle, max(0, offset - 4096), min(4096, offset)),
                    parser_version,
                    json.dumps(adapter.context),
                    now(),
                    file_id,
                ),
            )
            conn.execute("COMMIT")
            return {
                "files_unchanged": int(unchanged),
                "files_processed": int(not unchanged),
                "parsed_records": parsed,
                "malformed_rows": malformed,
                "deferred_files": deferred,
                "rows_added": ledger.added,
                "rows_revised": ledger.revised,
            }
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def scan(
    settings: Settings,
    *,
    full: bool = False,
    parser_version: str | None = None,
    source: JSONLSource | None = None,
) -> dict:
    try:
        with writer_lock(settings.data_dir):
            return _scan_locked(settings, full=full, parser_version=parser_version, source=source)
    except ScanBusy:
        active = None
        if settings.database.is_file():
            with connect(settings.database, readonly=True) as conn:
                try:
                    row = conn.execute(
                        "SELECT run_id FROM ingestion_runs WHERE status='running' "
                        "ORDER BY started_at_us DESC LIMIT 1"
                    ).fetchone()
                    active = row[0] if row else None
                except sqlite3.OperationalError:
                    pass  # first writer may not have created its schema yet
        return {"ok": False, "status": "busy", "active_run_id": active}


def _scan_locked(
    settings: Settings,
    *,
    full: bool,
    parser_version: str | None = None,
    source: JSONLSource | None = None,
) -> dict:
    source = default_runtime().source if source is None else source
    parser_version = source.parser_version if parser_version is None else parser_version
    started = perf_counter()
    with connect(settings.database) as conn:
        migrate(conn)
        # Holding the OS lock proves any still-running row belongs to an interrupted process.
        conn.execute(
            "UPDATE ingestion_runs SET status='failed',completed_at_us=? WHERE status='running'",
            (now(),),
        )
        run_id = uuid4().hex
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at_us,parser_version,status) "
            "VALUES (?,?,?,'running')",
            (run_id, now(), parser_version),
        )
        totals = dict.fromkeys(
            (
                "files_seen",
                "files_unchanged",
                "files_processed",
                "parsed_records",
                "malformed_rows",
                "deferred_files",
                "rows_added",
                "rows_revised",
                "failed_files",
            ),
            0,
        )
        seen = set()
        try:
            adapter = source.factory(None)
            for discovered in adapter.discover(settings):
                if discovered.agent_type != source.agent_type:
                    raise ValueError("discovered_source_agent_mismatch")
                seen.add(str(discovered.path))
                totals["files_seen"] += 1
                try:
                    counts = scan_file(conn, discovered.path, run_id, full, parser_version, source)
                    for key, value in counts.items():
                        totals[key] += value
                except OSError:
                    diagnostic(
                        conn, run_id, str(discovered.path), None, "file_unavailable_or_changed"
                    )
                    totals["failed_files"] += 1
            for path, code in adapter.discovery_warnings:
                diagnostic(conn, run_id, path, None, code)
            for row in conn.execute(
                "SELECT file_id,canonical_path FROM source_files WHERE agent_type=?",
                (source.agent_type,),
            ).fetchall():
                if row["canonical_path"] not in seen and not Path(row["canonical_path"]).exists():
                    conn.execute(
                        "UPDATE request_observations SET source_available=0 WHERE source_file_id=?",
                        (row["file_id"],),
                    )
            conn.execute(
                """UPDATE ingestion_runs SET status='complete',completed_at_us=?,files_seen=?,
                rows_added=?,rows_revised=?,malformed_rows=? WHERE run_id=?""",
                (
                    now(),
                    totals["files_seen"],
                    totals["rows_added"],
                    totals["rows_revised"],
                    totals["malformed_rows"],
                    run_id,
                ),
            )
        except BaseException:
            conn.execute(
                "UPDATE ingestion_runs SET status='failed',completed_at_us=? WHERE run_id=?",
                (now(), run_id),
            )
            operation_log(settings.data_dir, "scan_failed")
            raise
        diagnostics = [
            dict(row)
            for row in conn.execute(
                "SELECT source_path,source_line,code FROM ingestion_diagnostics WHERE run_id=?",
                (run_id,),
            )
        ]
        operation_log(
            settings.data_dir,
            "scan_complete",
            **totals,
            elapsed_ms=int((perf_counter() - started) * 1000),
        )
        return {
            "ok": totals["failed_files"] == 0,
            "status": "complete",
            "run_id": run_id,
            "parser_version": parser_version,
            **totals,
            "diagnostics": diagnostics,
            "ledger": summary(conn),
        }
