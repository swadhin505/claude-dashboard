import copy
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from claude_metrics.cli import main
from claude_metrics.config import Settings
from claude_metrics.ingestion.locking import ScanBusy, writer_lock
from claude_metrics.ingestion.scanner import scan
from claude_metrics.pricing.calculator import PricingPolicy
from claude_metrics.pricing.catalog import Catalog, load_catalog
from claude_metrics.pricing.receipts import apply_pricing, verify_receipts
from claude_metrics.reports import ReportFilter, data_health, overview, session_detail, sessions
from claude_metrics.storage import connect, inspect_database

FIXTURES = Path(__file__).parents[1] / "fixtures/claude"
ESTIMATE = PricingPolicy(assume_provider="anthropic")


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return Settings(data_dir=tmp_path / "app", source_dirs=(root,), timezone="Asia/Kolkata")


def row(output=50, **changes):
    item = json.loads((FIXTURES / "normal.jsonl").read_text().splitlines()[1])
    item["message"]["usage"]["output_tokens"] = output
    item.update(changes)
    return item


def write(settings, rows, name="session.jsonl", mode="w"):
    path = settings.source_dirs[0] / name
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        for item in rows:
            handle.write(json.dumps(item) + "\n")


def receipts(settings):
    with connect(settings.database, readonly=True) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM cost_receipts ORDER BY receipt_id")]


def test_receipt_idempotency_replay_and_immutability(setup):
    write(setup, [row()])
    scan(setup)
    assert apply_pricing(setup, policy=ESTIMATE)["receipts_added"] == 1
    before = receipts(setup)
    assert apply_pricing(setup, policy=ESTIMATE)["current_receipts_preserved"] == 1
    scan(setup, full=True)
    apply_pricing(setup, policy=ESTIMATE)
    assert receipts(setup) == before
    with connect(setup.database) as conn:
        assert verify_receipts(conn)["ok"]
        assert inspect_database(conn)["ok"]
        for sql in ("UPDATE cost_receipts SET effective_cost_nanos=0", "DELETE FROM cost_receipts"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_revision_supersedes_without_rewriting_old_receipt(setup):
    write(setup, [row(2)])
    scan(setup)
    apply_pricing(setup, policy=ESTIMATE)
    old = receipts(setup)[0]
    write(setup, [row()], mode="a")
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        assert overview(conn, ReportFilter("UTC"))["total"]["warnings"] == {"pricing_pending": 1}
    apply_pricing(setup, policy=ESTIMATE)
    all_receipts = receipts(setup)
    assert len(all_receipts) == 2
    assert all_receipts[0] == {**old, "is_current": 0}
    assert all_receipts[1]["effective_cost_nanos"] == 1_620_000
    assert all_receipts[1]["request_revision"] > old["request_revision"]
    with connect(setup.database, readonly=True) as conn:
        assert verify_receipts(conn)["checked"] == 2
        assert verify_receipts(conn)["ok"]


def test_explicit_reprice_preserves_unknown_provider_and_all_policies(setup):
    write(setup, [row()])
    scan(setup)
    apply_pricing(setup, policy=PricingPolicy())
    assert receipts(setup)[0]["effective_cost_nanos"] is None
    assert apply_pricing(setup, policy=ESTIMATE)["current_receipts_preserved"] == 1
    apply_pricing(setup, policy=ESTIMATE, reprice=True)
    assert len(receipts(setup)) == 2
    apply_pricing(setup, policy=PricingPolicy(), reprice=True)
    assert len(receipts(setup)) == 2 and receipts(setup)[0]["is_current"]
    with connect(setup.database, readonly=True) as conn:
        assert conn.execute("SELECT provider FROM requests").fetchone()[0] == "unknown"
        assert verify_receipts(conn)["ok"]


def test_auto_default_prices_legacy_cache_without_rewriting_evidence(setup):
    item = row()
    del item["message"]["usage"]["cache_creation"]
    item["message"]["usage"]["inference_geo"] = "not_available"
    write(setup, [item])
    scan(setup)
    apply_pricing(setup)
    receipt = json.loads(receipts(setup)[0]["snapshot_json"])
    assert receipt["input"]["provider"] == "unknown"
    assert receipt["input"]["inference_geo"] == "not_available"
    assert receipt["input"]["tokens"]["cache_write_unknown_tokens"] == 100
    assert receipt["policy"]["assume_provider"] == "anthropic"
    assert receipt["policy"]["assume_cache_5m"] is True
    assert receipt["result"]["status"] == "COMPLETE"
    # 100*3 + 200*0.3 + 100*3.75 + 50*15 USD per million tokens.
    assert receipt["result"]["effective_cost_nanos"] == 1_485_000
    assert receipt["result"]["estimates"]
    assert apply_pricing(setup)["current_receipts_preserved"] == 1
    with connect(setup.database, readonly=True) as conn:
        assert verify_receipts(conn)["ok"]


@pytest.mark.parametrize(
    "mode,provider,cache", [("auto", "anthropic", True), ("strict", None, False)]
)
def test_cli_cost_modes_and_explicit_repricing(setup, capsys, mode, provider, cache):
    write(setup, [row()])
    scan(setup)
    apply_pricing(setup, policy=PricingPolicy())
    original = receipts(setup)[0]["snapshot_json"]
    command = ["pricing", "apply", "--data-dir", str(setup.data_dir), "--json"]
    assert main([*command, "--cost-mode", mode]) == 0
    assert json.loads(capsys.readouterr().out)["current_receipts_preserved"] == 1
    assert main([*command, "--cost-mode", mode, "--reprice"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["policy"]["assume_provider"] == provider
    assert result["policy"]["assume_cache_5m"] is cache
    assert receipts(setup)[0]["snapshot_json"] == original
    with connect(setup.database, readonly=True) as conn:
        total = overview(conn, ReportFilter("UTC"))["total"]
        assert total["cost_status"] == ("COMPLETE" if mode == "auto" else "UNPRICED")
        assert verify_receipts(conn)["ok"]


def test_late_replay_after_pricing_retains_frozen_history_without_double_charge(setup):
    child = row(sessionId="child")
    write(setup, [child], name="a-child.jsonl")
    scan(setup)
    apply_pricing(setup, policy=ESTIMATE)
    old = receipts(setup)[0]
    child["forkedFrom"] = {"sessionId": "session-main", "messageUuid": "source-uuid"}
    write(setup, [child], name="a-child.jsonl", mode="a")
    write(setup, [row()], name="z-parent.jsonl")
    result = scan(setup)
    assert result["ledger"]["requests"] == 1
    apply_pricing(setup, policy=ESTIMATE)
    assert receipts(setup)[0] == {**old, "is_current": 0}
    with connect(setup.database, readonly=True) as conn:
        report = overview(conn, ReportFilter("UTC"))
        assert report["total"]["requests"] == 1
        assert report["total"]["total_cost_nanos"] == 1_620_000
        assert verify_receipts(conn)["ok"] and inspect_database(conn)["ok"]


def test_mixed_model_reports_and_all_groupings_conserve_values(setup):
    fixture = [
        json.loads(line) for line in (FIXTURES / "mixed-models.jsonl").read_text().splitlines()
    ]
    write(setup, fixture)
    scan(setup)
    apply_pricing(setup, policy=ESTIMATE)
    with connect(setup.database, readonly=True) as conn:
        filters = ReportFilter("Asia/Kolkata")
        report = overview(conn, filters)
        assert report["total"]["total_cost_nanos"] == 1_620_000 + 70_000
        for group in ("models", "projects", "daily", "sources"):
            assert sum(r["known_cost_nanos"] for r in report[group]) == 1_690_000
            assert sum(r["known_tokens"] for r in report[group]) == 480
            assert sum(r["requests"] for r in report[group]) == 2
        listed = sessions(conn, filters)
        assert len(listed["items"]) == 1
        assert listed["items"][0]["total_cost_nanos"] == 1_690_000
        detail = session_detail(conn, filters, listed["items"][0]["session_pk"])
        assert sum(r["cost"]["effective_cost_nanos"] for r in detail["requests"]) == 1_690_000


def test_partial_aggregate_reports_missing_money_tokens_and_units(setup):
    unknown = row(requestId="unknown-request")
    unknown["message"]["id"] = "unknown-message"
    unknown["message"]["model"] = "not-a-priced-model"
    unknown["message"]["usage"]["server_tool_use"] = {"unknown_unit": 2}
    write(setup, [row(), unknown])
    scan(setup)
    apply_pricing(setup, policy=ESTIMATE)
    with connect(setup.database, readonly=True) as conn:
        total = overview(conn, ReportFilter("UTC"))["total"]
        assert total["cost_status"] == "PARTIAL"
        assert total["total_cost_nanos"] is None and total["known_cost_nanos"] == 1_620_000
        assert total["unpriced_tokens"] == total["priced_tokens"] == 450
        assert total["complete_requests"] == total["unpriced_requests"] == 1
        assert total["unpriced_units"] == {"unknown_unit": 2}
        health = data_health(conn)
        assert health["incomplete_models"][0]["model"] == "not-a-priced-model"
        assert health["warnings"]["unknown_model"] == 1


def test_midnight_filter_does_not_leak_session_lifetime(setup):
    rows = [json.loads(line) for line in (FIXTURES / "midnight.jsonl").read_text().splitlines()]
    write(setup, rows)
    scan(setup)
    apply_pricing(setup, policy=ESTIMATE)
    with connect(setup.database, readonly=True) as conn:
        filters = ReportFilter("Asia/Kolkata", date(2026, 10, 1), date(2026, 10, 1))
        total = overview(conn, filters)["total"]
        listed = sessions(conn, filters)["items"]
        assert total["requests"] == 1 and total["known_tokens"] == 4
        assert listed[0]["requests"] == 1 and listed[0]["known_tokens"] == 4
        details = session_detail(conn, filters, listed[0]["session_pk"])
        assert len(details["requests"]) == 1


@pytest.mark.parametrize(
    "day,timestamps",
    [
        (
            date(2026, 3, 8),
            [
                "2026-03-08T04:59:59Z",
                "2026-03-08T05:00:00Z",
                "2026-03-09T03:59:59Z",
                "2026-03-09T04:00:00Z",
            ],
        ),
        (
            date(2026, 11, 1),
            [
                "2026-11-01T03:59:59Z",
                "2026-11-01T04:00:00Z",
                "2026-11-02T04:59:59Z",
                "2026-11-02T05:00:00Z",
            ],
        ),
    ],
)
def test_dst_day_queries_have_23_or_25_hours(setup, day, timestamps):
    rows = []
    for i, timestamp in enumerate(timestamps):
        item = row(requestId=f"req-{i}", timestamp=timestamp)
        item["message"]["id"] = f"msg-{i}"
        rows.append(item)
    write(setup, rows)
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        report = overview(conn, ReportFilter("America/New_York", day, day))
        assert report["total"]["requests"] == 2
        assert report["daily"][0]["key"] == str(day)


def test_pagination_and_model_project_filters(setup):
    rows = [row(sessionId=f"s-{i}", requestId=f"r-{i}") for i in range(3)]
    for i, item in enumerate(rows):
        item["message"]["id"] = f"m-{i}"
    write(setup, rows)
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        filters = ReportFilter("UTC")
        first = sessions(conn, filters, limit=2)
        second = sessions(conn, filters, limit=2, after=first["next_cursor"])
        assert len(first["items"]) == 2 and len(second["items"]) == 1
        assert second["next_cursor"] is None
        project = first["items"][0]["project_id"]
        assert overview(conn, ReportFilter("UTC", project=project))["total"]["requests"] == 3
        assert overview(conn, ReportFilter("UTC", model="unknown"))["total"]["requests"] == 0
        with pytest.raises(ValueError):
            sessions(conn, filters, limit=1000)


def test_pricing_uses_scan_lock_and_rolls_back_on_failure(setup):
    write(setup, [row()])
    scan(setup)
    with writer_lock(setup.data_dir), pytest.raises(ScanBusy):
        apply_pricing(setup)
    catalog = load_catalog()
    bad_meta = copy.deepcopy(catalog.metadata)
    apply_pricing(setup, catalog=catalog)
    before = receipts(setup)
    bad_meta["sha256"] = "f" * 64
    with pytest.raises(ValueError, match="different checksum"):
        apply_pricing(setup, catalog=Catalog(bad_meta, catalog.models), reprice=True)
    assert receipts(setup) == before


def test_cli_pricing_reports_and_verify(setup, capsys):
    write(setup, [row()])
    scan(setup)
    assert main(["pricing", "apply", "--json"]) == 0  # auto without extra flags
    assert json.loads(capsys.readouterr().out)["receipts_added"] == 1
    assert main(["report", "overview", "--from", "2026-09-30", "--to", "2026-09-30", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"]["total_cost_nanos"] == 1_620_000
    assert main(["pricing", "verify", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["checked"] == 1
    assert main(["report", "overview", "--from", "2026-09-30", "--json"]) == 1
    assert "both" in json.loads(capsys.readouterr().out)["error"]
    assert main(["report", "health", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["receipt_history_count"] == 1
    assert main(["report", "overview", "--from", "9999-12-31", "--to", "9999-12-31", "--json"]) == 1
    assert "next-day" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize(
    "field,bad",
    [("speed", True), ("inference_geo", []), ("service_tier", 123), ("server_tool_use", [])],
)
def test_malformed_pricing_fields_do_not_become_standard_rates(setup, field, bad):
    item = row()
    item["message"]["usage"][field] = bad
    write(setup, [item])
    result = scan(setup)
    assert result["malformed_rows"] == 1 and result["ledger"]["requests"] == 0


def test_failed_price_batch_rolls_back_prior_insertions(setup, monkeypatch):
    import claude_metrics.pricing.calculator as module

    first, second = row(), row(requestId="different")
    second["message"]["id"] = "different"
    write(setup, [first, second])
    scan(setup)
    original, calls = module.calculate, 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("simulated calculation failure")
        return original(*args)

    monkeypatch.setattr(module, "calculate", fail_second)
    with pytest.raises(ValueError, match="simulated"):
        apply_pricing(setup, policy=ESTIMATE)
    assert receipts(setup) == []
    with connect(setup.database, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM price_catalog_versions").fetchone()[0] == 0


def test_dated_pricing_only_affects_selected_requests(setup):
    rows = [json.loads(line) for line in (FIXTURES / "midnight.jsonl").read_text().splitlines()]
    write(setup, rows)
    scan(setup)
    from claude_metrics.units import date_bounds

    start, end = date_bounds(date(2026, 10, 1), date(2026, 10, 1), setup.timezone)
    assert apply_pricing(setup, policy=ESTIMATE, start_us=start, end_us=end)["receipts_added"] == 1
    with connect(setup.database, readonly=True) as conn:
        assert overview(conn, ReportFilter("UTC"))["total"]["unpriced_requests"] == 1


def test_empty_reports_and_session_request_pagination(setup):
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        assert overview(conn, ReportFilter("UTC"))["total"]["requests"] == 0
        assert sessions(conn, ReportFilter("UTC"))["items"] == []
        assert session_detail(conn, ReportFilter("UTC"), 1)["requests"] == []
        assert verify_receipts(conn)["checked"] == 0
    rows = []
    for i in range(3):
        item = row(requestId=f"r-{i}")
        item["message"]["id"] = f"m-{i}"
        rows.append(item)
    write(setup, rows)
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        first = session_detail(conn, ReportFilter("UTC"), 1, limit=2)
        last = session_detail(conn, ReportFilter("UTC"), 1, limit=2, after=first["next_cursor"])
        assert len(first["requests"]) == 2 and len(last["requests"]) == 1
        assert first["total"]["requests"] == last["total"]["requests"] == 3
        assert last["next_cursor"] is None


def test_missing_receipt_health_and_read_only_reports_do_not_write(setup):
    write(setup, [row()])
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        before = "\n".join(conn.iterdump())
        assert data_health(conn)["warnings"]["pricing_pending"] == 1
        overview(conn, ReportFilter("UTC"))
        sessions(conn, ReportFilter("UTC"))
        session_detail(conn, ReportFilter("UTC"), 1)
        assert "\n".join(conn.iterdump()) == before


def test_price_and_report_representative_history(setup):
    from time import perf_counter

    rows = []
    for i in range(2500):
        item = row(requestId=f"r-{i}", sessionId=f"s-{i // 50}")
        item["message"]["id"] = f"m-{i}"
        rows.append(item)
    write(setup, rows)
    scan(setup)
    start = perf_counter()
    assert apply_pricing(setup, policy=ESTIMATE)["receipts_added"] == 2500
    priced = perf_counter() - start
    with connect(setup.database, readonly=True) as conn:
        start = perf_counter()
        report = overview(conn, ReportFilter("UTC"))
        elapsed = perf_counter() - start
        assert report["total"]["requests"] == 2500
        assert report["total"]["known_cost_nanos"] == 2500 * 1_620_000
        assert (
            sum(i["known_cost_nanos"] for i in sessions(conn, ReportFilter("UTC"))["items"])
            == report["total"]["known_cost_nanos"]
        )
        print(f"2500 requests: pricing={priced:.3f}s overview={elapsed:.3f}s")


def test_tool_summary_keeps_unknown_outcomes_separate(setup):
    item = row()
    item["message"]["content"] = [{"type": "tool_use", "id": "tool-1", "name": "Read"}]
    write(setup, [item])
    scan(setup)
    with connect(setup.database, readonly=True) as conn:
        tools = session_detail(conn, ReportFilter("UTC"), 1)["tool_summary"]
        assert tools == [
            {"tool_name": "Read", "calls": 1, "succeeded": 0, "failed": 0, "unknown_success": 1}
        ]
