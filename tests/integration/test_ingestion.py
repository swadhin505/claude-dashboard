import copy
import json
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from claude_metrics.adapters.claude import ClaudeAdapter
from claude_metrics.config import Settings
from claude_metrics.ingestion.ledger import TOKEN_COLUMNS
from claude_metrics.ingestion.locking import writer_lock
from claude_metrics.ingestion.scanner import scan
from claude_metrics.storage import connect, inspect_database
from claude_metrics.units import date_bounds

FIXTURES = Path(__file__).parents[1] / "fixtures/claude"
CASES = json.loads((Path(__file__).parents[1] / "golden/claude-cases.json").read_text())["cases"]


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return Settings(data_dir=tmp_path / "ledger", source_dirs=(root,), timezone="Asia/Kolkata")


def request(output=50, **changes):
    row = json.loads((FIXTURES / "normal.jsonl").read_text().splitlines()[1])
    row["message"]["usage"]["output_tokens"] = output
    row.update(changes)
    return row


def write(settings, rows, name="session.jsonl", mode="w"):
    path = settings.source_dirs[0] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return path


def records(settings, table="requests"):
    with connect(settings.database, readonly=True) as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]


def logical(settings):
    return sorted(
        (
            r["request_id"] or "",
            r["message_id"] or "",
            r["occurred_at_us"],
            r["model_raw"],
            r["query_source"],
            *(r[k] for k in TOKEN_COLUMNS),
        )
        for r in records(settings)
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_golden_cases_use_real_scanner(setup, case):
    for name in case["files"]:
        target = setup.source_dirs[0] / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, target)
    result = scan(setup)
    expected = case["expected"]
    assert result["ok"] and not result["diagnostics"]
    assert result["ledger"]["requests"] == expected["requests"]
    assert result["ledger"]["tokens"] == expected["tokens"]
    assert result["ledger"]["all_tokens"] == expected["all_tokens"]
    if "main_tokens" in expected:
        assert result["ledger"]["by_source"]["main"]["known_tokens"] == expected["main_tokens"]
        assert (
            result["ledger"]["by_source"]["subagent"]["known_tokens"] == expected["subagent_tokens"]
        )
    with connect(setup.database, readonly=True) as conn:
        assert inspect_database(conn)["ok"]
        assert (
            conn.execute(
                "SELECT count(*) FROM request_observations WHERE is_current_winner=1"
            ).fetchone()[0]
            == expected["requests"]
        )
        winners = {}
        for row in conn.execute(
            "SELECT f.canonical_path,o.source_line FROM request_observations o "
            "JOIN source_files f ON f.file_id=o.source_file_id WHERE o.is_current_winner=1"
        ):
            name = Path(row[0]).relative_to(setup.source_dirs[0]).as_posix()
            winners.setdefault(name, []).append(row[1])
        assert {k: sorted(v) for k, v in winners.items()} == case["winner_lines"]
        if "billable_units" in expected:
            units = {
                r[0]: r[1]
                for r in conn.execute(
                    "SELECT unit_type,sum(quantity) FROM billable_units b "
                    "JOIN request_observations o USING(observation_id) "
                    "WHERE is_current_winner=1 GROUP BY unit_type"
                )
            }
            assert units == expected["billable_units"]
        if "daily_tokens" in expected:
            for day, total in expected["daily_tokens"].items():
                start, end = date_bounds(
                    date.fromisoformat(day), date.fromisoformat(day), setup.timezone
                )
                rows = conn.execute(
                    "SELECT * FROM requests WHERE occurred_at_us>=? AND occurred_at_us<?",
                    (start, end),
                ).fetchall()
                assert sum(sum(r[k] for k in TOKEN_COLUMNS) for r in rows) == total
    before, observations = logical(setup), records(setup, "request_observations")
    again = scan(setup)
    assert again["parsed_records"] == 0
    assert again["rows_added"] == again["rows_revised"] == 0
    assert logical(setup) == before
    assert records(setup, "request_observations") == observations
    full = scan(setup, full=True)
    assert full["ledger"] == result["ledger"]
    assert logical(setup) == before
    assert len(records(setup, "request_observations")) == len(observations)


def test_incremental_revision_and_first_timestamp_match_clean_full(setup, tmp_path):
    first = request(2)
    write(setup, [first])
    scan(setup)
    later = request(50, timestamp="2026-09-30T18:30:01.000Z")
    write(setup, [later], mode="a")
    result = scan(setup)
    assert result["rows_revised"] == 1
    assert records(setup)[0]["revision"] == 2
    full_settings = Settings(
        data_dir=tmp_path / "fresh", source_dirs=setup.source_dirs, timezone=setup.timezone
    )
    scan(full_settings, full=True)
    assert logical(full_settings) == logical(setup)


def test_final_record_can_correct_high_partial_usage(setup):
    write(setup, [request(100)])
    scan(setup)
    final = request(20)
    final["message"]["stop_reason"] = "end_turn"
    write(setup, [final], mode="a")
    scan(setup)
    assert records(setup)[0]["output_tokens"] == 20


def test_unfinished_line_uses_byte_checkpoint_then_completes(setup):
    first = request(2)
    first["ignored"] = "emoji🙂 Unicode文"
    path = write(setup, [first])
    position = path.stat().st_size
    raw = (json.dumps(request(50)) + "\n").encode()
    with path.open("ab") as handle:
        handle.write(raw[:73])
    result = scan(setup)
    assert result["deferred_files"] == 1
    assert records(setup, "source_files")[0]["byte_offset"] == position
    with path.open("ab") as handle:
        handle.write(raw[73:])
    result = scan(setup)
    assert result["deferred_files"] == 0
    assert result["ledger"]["requests"] == 1
    assert records(setup)[0]["output_tokens"] == 50
    assert records(setup, "source_files")[0]["line_number"] == 2


def test_truncate_replace_delete_retain_durable_requests(setup):
    path = write(setup, [request()])
    scan(setup)
    path.write_bytes(b"")
    assert scan(setup)["ledger"]["requests"] == 1
    assert not records(setup, "request_observations")[0]["source_available"]
    replacement = write(setup, [request()], name="replacement.tmp")
    replacement.replace(path)
    assert scan(setup)["ledger"]["requests"] == 1
    assert len(records(setup, "request_observations")) == 2
    path.unlink()
    assert scan(setup)["ledger"]["all_tokens"] == 450
    assert all(not r["source_available"] for r in records(setup, "request_observations"))


def test_parser_upgrade_corrects_unchanged_file_and_retains_audit(setup, monkeypatch):
    write(setup, [request()])
    scan(setup)
    parse = ClaudeAdapter.parse

    def corrected(self, record):
        payload = copy.deepcopy(record.payload)
        payload["message"]["usage"]["output_tokens"] = 40
        from claude_metrics.adapters.base import SourceRecord

        yield from parse(self, SourceRecord(payload=payload, source=record.source))

    monkeypatch.setattr(ClaudeAdapter, "parse", corrected)
    result = scan(setup, parser_version="test-parser/2")
    assert result["parsed_records"] == 1
    assert records(setup)[0]["output_tokens"] == 40
    assert records(setup)[0]["revision"] == 2
    obs = records(setup, "request_observations")
    assert len(obs) == 2 and obs[0]["superseded"] and not obs[0]["is_current_winner"]
    assert obs[1]["is_current_winner"]


def test_identity_enrichment_and_retries_are_not_double_counted(setup):
    early = request(2)
    early.pop("requestId")
    write(setup, [early])
    scan(setup)
    write(setup, [request(50)], mode="a")
    assert scan(setup)["ledger"]["requests"] == 1
    assert records(setup)[0]["request_id"] == "req-a"
    write(setup, [request(10, requestId="different-api-retry")], mode="a")
    assert scan(setup)["ledger"]["requests"] == 2
    assert scan(setup, full=True)["ledger"]["requests"] == 2


def test_missing_all_ids_is_low_confidence_and_never_content_deduped(setup):
    row = request()
    row.pop("requestId")
    row["message"].pop("id")
    write(setup, [row, row])
    assert scan(setup)["ledger"]["requests"] == 2
    assert all(r["identity_confidence"] == "low" for r in records(setup))
    assert scan(setup, full=True)["ledger"]["requests"] == 2


@pytest.mark.parametrize("late", [False, True])
def test_record_specific_lineage_order_and_late_evidence(setup, late):
    replay = request(sessionId="child")
    if late:
        write(setup, [replay], name="a-child.jsonl")
        assert scan(setup)["ledger"]["requests"] == 1
    replay["forkedFrom"] = {"sessionId": "session-main", "messageUuid": "original"}
    write(setup, [replay], name="a-child.jsonl", mode="a" if late else "w")
    write(setup, [request()], name="z-parent.jsonl")
    result = scan(setup)
    assert result["ledger"]["requests"] == 1
    assert result["ledger"]["all_tokens"] == 450
    assert len(records(setup, "request_observations")) == (3 if late else 2)
    with connect(setup.database, readonly=True) as conn:
        assert (
            conn.execute(
                "SELECT s.session_id FROM requests r JOIN sessions s USING(session_pk)"
            ).fetchone()[0]
            == "session-main"
        )
        assert inspect_database(conn)["ok"]
    assert scan(setup, full=True)["ledger"]["requests"] == 1


def test_session_ancestry_alone_does_not_prove_replayed_request(setup):
    child = request(sessionId="child", forkedFrom={"sessionId": "session-main"})
    write(setup, [request(), child])
    assert scan(setup)["ledger"]["requests"] == 2


def test_tools_and_turns_survive_incremental_scan_without_content(setup):
    user = {
        "type": "user",
        "sessionId": "session-main",
        "uuid": "prompt-id",
        "message": {"content": "SECRET_PROMPT"},
    }
    assistant = request()
    assistant["message"]["content"] = [
        {"type": "text", "text": "SECRET_RESPONSE"},
        {"type": "tool_use", "id": "tool-id", "name": "Read", "input": "SECRET_ARGUMENT"},
    ]
    write(setup, [user, assistant])
    scan(setup)
    result = {
        "type": "user",
        "sessionId": "session-main",
        "uuid": "not-a-turn",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-id",
                    "is_error": False,
                    "content": "SECRET_RESULT",
                }
            ]
        },
    }
    next_request = request(requestId="req-next")
    next_request["message"]["id"] = "msg-next"
    write(setup, [result, next_request], mode="a")
    scan(setup)
    assert len(records(setup, "turns")) == 1
    assert len({r["turn_pk"] for r in records(setup)}) == 1
    assert records(setup)[0]["turn_pk"] is not None
    tool = records(setup, "tool_calls")[0]
    assert tool["success"] == 1 and tool["tool_name"] == "Read" and tool["request_pk"]
    with connect(setup.database, readonly=True) as conn:
        assert "SECRET_" not in "\n".join(conn.iterdump())


@pytest.mark.parametrize(
    "bad_usage",
    [
        {"input_tokens": -1},
        {"input_tokens": True},
        {"output_tokens": 1.5},
        {"output_tokens": 2**63},
        {"cache_creation_input_tokens": 101},
        {"reasoning_tokens": 51},
    ],
)
def test_invalid_usage_is_quarantined_without_partial_rows(setup, bad_usage):
    bad = request()
    bad["message"]["usage"].update(bad_usage)
    write(setup, [bad])
    write(setup, [request()], name="good.jsonl")
    result = scan(setup)
    assert result["malformed_rows"] == 1
    assert result["ledger"]["requests"] == 1
    assert len(records(setup, "request_observations")) == 1


def test_unknown_counters_and_provider_are_not_invented(setup):
    row = request()
    row["message"]["usage"] = {"output_tokens": 10}
    write(setup, [row])
    result = scan(setup)
    assert result["ledger"]["all_tokens"] is None
    assert result["ledger"]["known_tokens"] == 10
    assert result["ledger"]["unknown_counters"]["input_uncached_tokens"] == 1
    assert records(setup)[0]["provider"] == "unknown"


def test_bad_json_and_progress_are_visible_or_ignored_without_payload_leaks(setup):
    path = write(setup, [request(), {"type": "progress", "data": request()}])
    with path.open("ab") as handle:
        handle.write(b'{"SECRET_CONTENT": invalid}\n\xff\n{"a":1,"a":2}\n{"a":NaN}\n')
    result = scan(setup)
    assert result["malformed_rows"] == 4
    assert result["ledger"]["requests"] == 1
    assert "SECRET_CONTENT" not in json.dumps(result)


def test_transaction_failure_does_not_advance_checkpoint(setup, monkeypatch):
    write(setup, [request()])
    from claude_metrics.ingestion.ledger import Ledger

    apply = Ledger.apply

    def fail(self, events):
        apply(self, events)
        raise RuntimeError("simulated power loss")

    monkeypatch.setattr(Ledger, "apply", fail)
    with pytest.raises(RuntimeError):
        scan(setup)
    assert not records(setup) and not records(setup, "source_files")
    assert records(setup, "ingestion_runs")[0]["status"] == "failed"
    monkeypatch.setattr(Ledger, "apply", apply)
    assert scan(setup)["ledger"]["requests"] == 1


def test_cross_process_lock_returns_busy_and_releases(setup):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    command = [
        sys.executable,
        "-m",
        "claude_metrics",
        "scan",
        "--data-dir",
        str(setup.data_dir),
        "--source-dir",
        str(setup.source_dirs[0]),
        "--json",
    ]
    with writer_lock(setup.data_dir):
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)
        assert result.returncode == 1
        assert json.loads(result.stdout)["status"] == "busy"
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout


def test_overlapping_roots_discover_only_once(setup):
    path = write(setup, [request()], name="project/a.jsonl")
    config = Settings(
        data_dir=setup.data_dir,
        source_dirs=(*setup.source_dirs, path.parent),
        timezone=setup.timezone,
    )
    result = scan(config)
    assert result["files_seen"] == result["ledger"]["requests"] == 1


def test_schema_one_upgrades_preserving_existing_session(setup):
    from claude_metrics.storage import bundled_migrations, migrate

    with connect(setup.database) as conn:
        migrate(conn, bundled_migrations()[:1])
        conn.execute("INSERT INTO sessions(agent_type,session_id) VALUES ('claude','old-session')")
    write(setup, [request()])
    scan(setup)
    assert {r["session_id"] for r in records(setup, "sessions")} == {"old-session", "session-main"}


def test_late_replay_with_turn_and_tool_reassigns_owner_safely(setup):
    user = {"type": "user", "sessionId": "child", "uuid": "child-turn"}
    child = request(sessionId="child")
    child["message"]["content"] = [{"type": "tool_use", "id": "t", "name": "Read"}]
    write(setup, [user, child], name="a-child.jsonl")
    scan(setup)
    child["forkedFrom"] = {"sessionId": "session-main", "messageUuid": "original"}
    write(setup, [child], name="a-child.jsonl", mode="a")
    write(setup, [request()], name="z-parent.jsonl")
    assert scan(setup)["ledger"]["requests"] == 1
    assert records(setup)[0]["turn_pk"] is None
    assert records(setup, "tool_calls")[0]["session_pk"] == records(setup)[0]["session_pk"]


def test_earlier_nonwinning_observation_keeps_original_day(setup):
    write(setup, [request(50, timestamp="2026-09-30T18:30:01.000Z"), request(2)])
    scan(setup)
    before_midnight = date_bounds(date(2026, 9, 30), date(2026, 9, 30), setup.timezone)[1]
    assert records(setup)[0]["occurred_at_us"] < before_midnight


def test_same_size_rewrite_and_growing_prefix_rewrite_force_rescan(setup):
    path = write(setup, [request(10)])
    scan(setup)
    write(setup, [request(20)])
    result = scan(setup)
    assert result["parsed_records"] == 1 and records(setup)[0]["output_tokens"] == 20
    old_mtime = path.stat().st_mtime_ns
    write(setup, [request(30), request(40, requestId="retry")])
    os.utime(path, ns=(old_mtime, old_mtime))
    result = scan(setup)
    assert result["parsed_records"] == 2
    assert result["ledger"]["requests"] == 2


def test_inaccessible_file_does_not_block_other_files(setup, monkeypatch):
    bad = write(setup, [request()], name="bad.jsonl")
    write(setup, [request()], name="good.jsonl")
    original = Path.open

    def controlled_open(self, *args, **kwargs):
        if self == bad:
            raise PermissionError("test access denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", controlled_open)
    result = scan(setup)
    assert result["failed_files"] == 1 and not result["ok"]
    assert result["ledger"]["requests"] == 1


def test_large_complete_line_is_skipped_and_following_record_survives(setup, monkeypatch):
    import claude_metrics.ingestion.scanner as scanner

    path = write(setup, [request()])
    original = path.read_bytes()
    path.write_bytes(b'"' + b"x" * 4096 + b'"\n' + original)
    monkeypatch.setattr(scanner, "MAX_LINE_BYTES", 1024)
    result = scan(setup)
    assert result["malformed_rows"] == 1 and result["ledger"]["requests"] == 1
    assert result["diagnostics"][0]["code"] == "line_too_large"


def test_os_releases_lock_after_process_exit(setup):
    # os._exit bypasses finally; the kernel must release the byte-range/flock lock.
    script = (
        "import os,sys; from pathlib import Path; "
        "from claude_metrics.ingestion.locking import writer_lock; "
        "lock=writer_lock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", script, str(setup.data_dir)], check=True, timeout=15)
    assert scan(setup)["ok"]


def test_stale_run_is_recovered_and_busy_reports_active_id(setup):
    scan(setup)
    with connect(setup.database) as conn:
        conn.execute(
            "INSERT INTO ingestion_runs(run_id,started_at_us,parser_version,status) "
            "VALUES ('interrupted',1,'old','running')"
        )
    with writer_lock(setup.data_dir):
        assert scan(setup)["active_run_id"] == "interrupted"
    scan(setup)
    assert (
        next(r for r in records(setup, "ingestion_runs") if r["run_id"] == "interrupted")["status"]
        == "failed"
    )


def test_representative_ledger_indexes_and_incremental_work(setup):
    from time import perf_counter

    rows = []
    for index in range(2500):
        row = request(2, sessionId=f"session-{index // 50}", requestId=f"req-{index}")
        row["message"]["id"] = f"msg-{index}"
        rows.append(row)
    write(setup, rows)
    started = perf_counter()
    first = scan(setup)
    elapsed = perf_counter() - started
    started = perf_counter()
    again = scan(setup)
    unchanged = perf_counter() - started
    assert first["ledger"]["requests"] == 2500
    assert again["parsed_records"] == 0 and again["ledger"] == first["ledger"]
    with connect(setup.database, readonly=True) as conn:
        plans = [
            (
                "SELECT * FROM requests WHERE occurred_at_us>=1 AND occurred_at_us<2",
                "idx_requests_time",
            ),
            (
                "SELECT * FROM requests WHERE session_pk=1 AND message_id='msg-1'",
                "idx_requests_message",
            ),
            (
                "SELECT * FROM requests WHERE session_pk=1 AND request_id='req-1'",
                "idx_requests_request",
            ),
        ]
        for query, index in plans:
            assert index in str([tuple(r) for r in conn.execute("EXPLAIN QUERY PLAN " + query)])
    print(f"synthetic 2500 requests: first={elapsed:.3f}s unchanged={unchanged:.3f}s")


def test_billable_unit_only_revision_invalidates_projection_version(setup):
    first = request()
    first["message"]["usage"]["server_tool_use"] = {"web_search_requests": 1}
    write(setup, [first])
    scan(setup)
    final = copy.deepcopy(first)
    final["message"]["usage"]["server_tool_use"]["web_search_requests"] = 2
    write(setup, [final], mode="a")
    assert scan(setup)["rows_revised"] == 1
    assert records(setup)[0]["revision"] == 2
    assert scan(setup, full=True)["rows_revised"] == 0


def test_mutation_recipe_against_real_scanner(setup):
    recipe = json.loads((FIXTURES / "file-mutations.json").read_text())
    path = setup.source_dirs[0] / "mutating.jsonl"
    path.write_bytes(recipe["initial"].encode())
    assert scan(setup)["ledger"]["requests"] == 1
    for step in recipe["steps"]:
        mode = "ab" if step["operation"] == "append" else "wb"
        target = path.with_suffix(".tmp") if step["operation"] == "replace" else path
        with target.open(mode) as handle:
            handle.write(step["text"].encode())
        if target != path:
            target.replace(path)
        expected = step.get("expected_unique_requests", step.get("expected_durable_requests"))
        assert scan(setup)["ledger"]["requests"] == expected


def test_mid_scan_mutation_rolls_back_file_and_later_retry_succeeds(setup, monkeypatch):
    path = write(setup, [request()])
    from claude_metrics.ingestion.ledger import Ledger

    apply = Ledger.apply

    def truncate_during_scan(self, events):
        apply(self, events)
        path.write_bytes(b"")

    monkeypatch.setattr(Ledger, "apply", truncate_during_scan)
    result = scan(setup)
    assert result["failed_files"] == 1
    assert not records(setup) and not records(setup, "source_files")
    monkeypatch.setattr(Ledger, "apply", apply)
    write(setup, [request()])
    assert scan(setup)["ledger"]["requests"] == 1
