import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from claude_metrics.cli import main
from claude_metrics.config import Settings
from claude_metrics.ingestion.locking import ScanBusy, writer_lock
from claude_metrics.ingestion.scanner import scan
from claude_metrics.maintenance import backup, support_bundle
from claude_metrics.operations import operation_log
from claude_metrics.pricing import calculator, receipts, store
from claude_metrics.pricing.calculator import PricingPolicy, calculate
from claude_metrics.pricing.catalog import CatalogError, load_catalog
from claude_metrics.reports import ReportFilter, overview
from claude_metrics.storage import Migration, bundled_migrations, connect, inspect_database, migrate

FIXTURES = Path(__file__).parents[1] / "fixtures/claude"
ESTIMATE = PricingPolicy(assume_provider="anthropic")


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "private-project-秘密 # &"
    root.mkdir()
    shutil.copyfile(FIXTURES / "mixed-models.jsonl", root / "history.jsonl")
    return Settings(tmp_path / "data", (root,), "Asia/Kolkata")


def prepare(settings):
    scan(settings)
    receipts.apply_pricing(settings, policy=ESTIMATE)


def totals(settings):
    with connect(settings.database, readonly=True) as conn:
        return overview(conn, ReportFilter(settings.timezone))["total"]


def snapshots(settings):
    with connect(settings.database, readonly=True) as conn:
        return [
            tuple(row) for row in conn.execute("SELECT * FROM cost_receipts ORDER BY receipt_id")
        ]


def test_geography_fix_versioned_old_receipts_replay(settings, monkeypatch):
    path = settings.source_dirs[0] / "history.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row["message"]["usage"]["inference_geo"] = "not_available"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    scan(settings)
    with monkeypatch.context() as old:
        old.setattr(calculator, "FORMULA_VERSION", "claude-cost/1")
        old.setattr(
            calculator, "calculate", lambda *args: calculate(*args, formula_version="claude-cost/1")
        )
        receipts.apply_pricing(settings, policy=ESTIMATE)
    before = snapshots(settings)
    assert totals(settings)["cost_status"] == "UNPRICED"
    receipts.apply_pricing(settings, policy=ESTIMATE)
    assert snapshots(settings) == before  # no implicit reprice on formula upgrade
    receipts.apply_pricing(settings, policy=ESTIMATE, reprice=True)
    assert totals(settings)["known_cost_nanos"] == 1_690_000
    with connect(settings.database, readonly=True) as conn:
        assert receipts.verify_receipts(conn)["ok"]
        assert conn.execute("SELECT count(*) FROM cost_receipts").fetchone()[0] == 4
        assert {
            r[0] for r in conn.execute("SELECT DISTINCT formula_version FROM cost_receipts")
        } == {"claude-cost/1", "claude-cost/2"}


def test_backup_live_wal_and_restore_preserve_receipts(settings, tmp_path):
    prepare(settings)
    before = snapshots(settings)
    with connect(settings.database) as live:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute(
            "INSERT INTO sessions(agent_type,session_id) VALUES ('claude','SECRET_SESSION')"
        )
        assert Path(str(settings.database) + "-wal").stat().st_size > 0
        destination = tmp_path / "backup"
        result = backup(settings, destination)
    assert result["ok"]
    manifest = json.loads((destination / "manifest.json").read_text())
    for name, checksum in manifest["files_sha256"].items():
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == checksum
    assert not list(destination.glob("*.sqlite3-*"))
    restored = Settings(destination, settings.source_dirs, settings.timezone)
    with connect(restored.database) as db:
        assert inspect_database(db)["ok"]  # connect restores WAL mode
        assert db.execute("SELECT 1 FROM sessions WHERE session_id='SECRET_SESSION'").fetchone()
        assert receipts.verify_receipts(db, data_dir=destination)["ok"]
    assert snapshots(restored) == before
    assert scan(restored)["rows_added"] == 0
    assert totals(restored) == totals(settings)
    with pytest.raises(FileExistsError):
        backup(settings, destination)


def test_backup_blocks_competing_writer_and_source_destination(settings, tmp_path):
    prepare(settings)
    with writer_lock(settings.data_dir), pytest.raises(ScanBusy):
        backup(settings, tmp_path / "busy-backup")
    assert not (tmp_path / "busy-backup").exists()
    with pytest.raises(ValueError, match="source directories"):
        backup(settings, settings.source_dirs[0] / "bad-backup")


def test_failed_backup_never_publishes_partial_directory(settings, tmp_path, monkeypatch):
    prepare(settings)
    import claude_metrics.maintenance as maintenance

    def fail(*args):
        raise OSError("simulated disk full")

    monkeypatch.setattr(maintenance, "copy_catalog", fail)
    with pytest.raises(OSError):
        backup(settings, tmp_path / "failed-backup")
    assert not (tmp_path / "failed-backup").exists()
    assert not list(tmp_path.glob(".backup-stage-*"))
    assert totals(settings)["requests"] == 2


def test_support_bundle_excludes_identifiers_usage_receipts_and_paths(settings, tmp_path):
    prepare(settings)
    with connect(settings.database) as conn:
        conn.execute("UPDATE sessions SET session_id='SECRET_SESSION',cwd='SECRET_PATH'")
        conn.execute("UPDATE requests SET model_raw='SECRET_MODEL'")
    result = support_bundle(settings, tmp_path / "support")
    payload = (tmp_path / "support/support.json").read_text()
    assert result["contains_private_ledger"] is False
    for forbidden in (
        "SECRET",
        "private-project",
        "1690000",
        "input_tokens",
        "snapshot_json",
        str(tmp_path),
    ):
        assert forbidden not in payload
    assert len(list((tmp_path / "support").iterdir())) == 1
    assert json.loads(payload)["database"]["ok"]


def test_logs_are_allowlisted_bounded_and_do_not_fail_accounting(settings, monkeypatch):
    prepare(settings)
    operation_log(
        settings.data_dir,
        "scan_complete",
        source_path="SECRET",
        error="SECRET",
        files_seen=7,
        parsed_records="SECRET",
        elapsed_ms=-1,
    )
    path = settings.data_dir / "logs/operations.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[-1]["files_seen"] == 7
    assert "SECRET" not in path.read_text() and "elapsed_ms" not in records[-1]
    path.write_bytes(b" " * 1_048_576)
    operation_log(settings.data_dir, "scan_complete")
    assert path.stat().st_size < 500
    assert (path.parent / "operations.1.jsonl").stat().st_size == 1_048_576
    with pytest.raises(ValueError):
        operation_log(settings.data_dir, "SECRET")
    with monkeypatch.context() as denied:
        denied.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
        operation_log(settings.data_dir, "scan_complete")  # optional logs can't break a commit


def catalog_bytes():
    return (load_catalog().directory / "litellm-model-prices.json").read_bytes()


def install(settings, monkeypatch, commit="a" * 40, raw=None):
    raw = catalog_bytes() if raw is None else raw
    monkeypatch.setattr(store, "_download", lambda _: raw)
    return store.update_catalog(settings.data_dir, commit, hashlib.sha256(raw).hexdigest())


def test_catalog_update_retains_old_versions_and_never_reprices(settings, monkeypatch, tmp_path):
    prepare(settings)
    before = snapshots(settings)
    original = store.active_catalog(settings.data_dir)
    result = install(settings, monkeypatch)
    assert result["repriced"] is False
    assert snapshots(settings) == before
    assert store.active_catalog(settings.data_dir).metadata["version"] == "litellm-" + "a" * 40
    assert (
        store.archived_catalog(settings.data_dir, original.metadata["version"]).metadata
        == original.metadata
    )
    assert (settings.data_dir / "pricing" / original.metadata["version"]).is_dir()
    receipts.apply_pricing(settings, policy=ESTIMATE)
    assert snapshots(settings) == before
    receipts.apply_pricing(settings, policy=ESTIMATE, reprice=True)
    install(settings, monkeypatch, "b" * 40)
    with connect(settings.database, readonly=True) as conn:
        assert receipts.verify_receipts(conn, data_dir=settings.data_dir)["ok"]
    result = backup(settings, tmp_path / "versioned-backup")
    assert result["ok"]
    for version in (original.metadata["version"], "litellm-" + "a" * 40, "litellm-" + "b" * 40):
        assert (tmp_path / "versioned-backup/pricing" / version).is_dir()


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b'{"duplicate":1,"duplicate":2}',
        b'{"x":NaN}',
        b"[]",
        b"\xff",
        b"[" * 2000 + b"0" + b"]" * 2000,
    ],
)
def test_invalid_catalog_never_changes_selection(settings, monkeypatch, payload):
    original = store.active_catalog(settings.data_dir).metadata
    with pytest.raises(CatalogError):
        install(settings, monkeypatch, raw=payload)
    assert store.active_catalog(settings.data_dir).metadata == original
    assert not (settings.data_dir / "pricing/active.json").exists()
    assert not list((settings.data_dir / "pricing").glob(".catalog-stage-*"))


def test_catalog_checksum_collision_and_pointer_failure_are_safe(settings, monkeypatch):
    install(settings, monkeypatch)
    original = (settings.data_dir / "pricing/active.json").read_bytes()
    raw = catalog_bytes() + b"\n"
    with pytest.raises(CatalogError, match="collision"):
        install(settings, monkeypatch, raw=raw)
    assert (settings.data_dir / "pricing/active.json").read_bytes() == original
    monkeypatch.setattr(store, "_download", lambda _: b"wrong")
    with pytest.raises(CatalogError, match="SHA-256"):
        store.update_catalog(settings.data_dir, "b" * 40, "0" * 64)

    def fail(*args):
        raise OSError("atomic pointer publication failed")

    monkeypatch.setattr(store.os, "replace", fail)
    with pytest.raises(OSError):
        install(settings, monkeypatch, "b" * 40)
    assert (settings.data_dir / "pricing/active.json").read_bytes() == original
    assert store.active_catalog(settings.data_dir).metadata["version"] == "litellm-" + "a" * 40
    assert store.archived_catalog(settings.data_dir, "litellm-" + "b" * 40)  # safe orphan, retained


def test_catalog_pointer_tampering_is_not_silently_ignored(settings):
    directory = settings.data_dir / "pricing"
    directory.mkdir(parents=True)
    (directory / "active.json").write_text('{"version":"../../escape","sha256":"fake"}')
    with pytest.raises(CatalogError):
        store.active_catalog(settings.data_dir)


@pytest.mark.parametrize(
    "commit,checksum", [("main", "0" * 64), ("../escape", "0" * 64), ("a" * 40, "bad")]
)
def test_update_requires_reviewed_immutable_identifiers(settings, monkeypatch, commit, checksum):
    monkeypatch.setattr(store, "_download", lambda _: pytest.fail("must validate before network"))
    with pytest.raises(CatalogError):
        store.update_catalog(settings.data_dir, commit, checksum)


def test_downloader_limits_redirects_and_truncated_responses(monkeypatch):
    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": "5"}

    class Opener:
        def open(self, request, timeout):
            assert (
                request.full_url
                == "https://raw.githubusercontent.com/BerriAI/litellm/"
                + "a" * 40
                + "/model_prices_and_context_window.json"
            )
            assert timeout == 15
            return Response(b"abc")

    monkeypatch.setattr(store.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(CatalogError, match="incomplete"):
        store._download("a" * 40)
    Response.headers = {"Content-Length": str(store.MAX_DOWNLOAD + 1)}
    with pytest.raises(CatalogError, match="size limit"):
        store._download("a" * 40)
    with pytest.raises(CatalogError, match="redirects"):
        store._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.example")


def test_maintenance_cli_exports_and_catalog_info(settings, tmp_path, capsys):
    prepare(settings)
    for command in ("backup", "support-bundle"):
        assert (
            main(
                [
                    command,
                    "--data-dir",
                    str(settings.data_dir),
                    "--output",
                    str(tmp_path / command),
                    "--json",
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["ok"]
    assert main(["pricing", "info", "--data-dir", str(settings.data_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["verified"]


def test_cli_init_obeys_same_writer_lock(settings, capsys):
    prepare(settings)
    before = snapshots(settings)
    with writer_lock(settings.data_dir):
        assert main(["init", "--data-dir", str(settings.data_dir), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert snapshots(settings) == before


def run_crash(settings, body):
    script = (
        """
import os,sys
from pathlib import Path
from claude_metrics.config import Settings
from claude_metrics.ingestion import scanner
from claude_metrics.pricing import receipts
from claude_metrics.pricing.calculator import PricingPolicy
settings = Settings(Path(sys.argv[1]), (Path(sys.argv[2]),), 'Asia/Kolkata')
"""
        + body
    )
    process = subprocess.run(
        [sys.executable, "-B", "-c", script, str(settings.data_dir), str(settings.source_dirs[0])],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert process.returncode == 73, process.stderr


def test_process_death_recovers_lock_run_and_transaction(settings):
    run_crash(
        settings,
        """
original = scanner.scan_file
def die(conn, *args):
    result = original(conn, *args)
    conn.execute('BEGIN IMMEDIATE')
    conn.execute('UPDATE source_files SET byte_offset=99999999')
    conn.execute("INSERT INTO sessions(agent_type,session_id) VALUES ('claude','UNCOMMITTED')")
    os._exit(73)
scanner.scan_file = die
scanner.scan(settings)
""",
    )
    with connect(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "running"
        assert not conn.execute("SELECT 1 FROM sessions WHERE session_id='UNCOMMITTED'").fetchone()
        assert conn.execute("SELECT byte_offset FROM source_files").fetchone()[0] < 99999999
    recovered = scan(settings)
    assert recovered["rows_added"] == 0
    with connect(settings.database, readonly=True) as conn:
        assert inspect_database(conn)["ok"]
        assert [
            r[0] for r in conn.execute("SELECT status FROM ingestion_runs ORDER BY started_at_us")
        ] == ["failed", "complete"]
    receipts.apply_pricing(settings, policy=ESTIMATE)
    assert totals(settings)["known_cost_nanos"] == 1_690_000


def test_process_death_rolls_back_partial_pricing(settings):
    scan(settings)
    run_crash(
        settings,
        """
from claude_metrics.pricing import calculator
original = calculator.calculate
calls = 0
def die(*args):
    global calls
    calls += 1
    if calls == 2:
        os._exit(73)
    return original(*args)
calculator.calculate = die
receipts.apply_pricing(settings, policy=PricingPolicy(assume_provider='anthropic'))
""",
    )
    assert snapshots(settings) == []
    receipts.apply_pricing(settings, policy=ESTIMATE)
    assert totals(settings)["known_cost_nanos"] == 1_690_000


def test_upgrade_failure_rolls_back_data_and_preserves_receipts(settings):
    prepare(settings)
    before = snapshots(settings)
    with connect(settings.database) as conn:
        failure = Migration(
            4, "004_failure.sql", "UPDATE requests SET output_tokens=999;\nINVALID;\n"
        )
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, (*bundled_migrations(), failure))
        assert inspect_database(conn)["ok"]
        assert receipts.verify_receipts(conn)["ok"]
    assert snapshots(settings) == before
    assert totals(settings)["known_tokens"] == 480
