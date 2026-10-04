"""Exercise integration seams with synthetic adapters/engines, not real extra providers."""

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from claude_metrics import runtime as wiring
from claude_metrics.adapters.base import JSONLSource, SourceFile
from claude_metrics.adapters.claude import ClaudeAdapter
from claude_metrics.cli import main
from claude_metrics.config import Settings, source_status
from claude_metrics.domain import Project, Session
from claude_metrics.ingestion.scanner import scan
from claude_metrics.pricing import calculator, policy
from claude_metrics.pricing.catalog import load_catalog
from claude_metrics.pricing.receipts import apply_pricing, verify_receipts
from claude_metrics.reports import ReportFilter, overview
from claude_metrics.runtime import Runtime
from claude_metrics.storage import connect
from claude_metrics.web.app import create_app

FIXTURES = Path(__file__).parents[1] / "fixtures/claude"
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def settings(tmp_path):
    source = tmp_path / "projects"
    source.mkdir()
    shutil.copyfile(FIXTURES / "normal.jsonl", source / "normal.jsonl")
    return Settings(tmp_path / "data", (source,), "Asia/Kolkata")


class RecordingEngine:
    FORMULA_VERSION = calculator.FORMULA_VERSION
    SUPPORTED_FORMULAS = calculator.SUPPORTED_FORMULAS

    def __init__(self, agent="claude"):
        self.AGENT_TYPE = agent
        self.calculations = self.replays = 0

    def pricing_input(self, request, units):
        return calculator.pricing_input(request, units)

    def calculate(self, *args, **kwargs):
        self.calculations += 1
        return calculator.calculate(*args, **kwargs)

    def recompute_nanos(self, payload):
        self.replays += 1
        return calculator.recompute_nanos(payload)


def test_one_composition_point_reaches_cli_web_and_refresh(settings, monkeypatch, capsys):
    contexts, catalog_reads = [], []

    class RecordingAdapter(ClaudeAdapter):
        def __init__(self, context=None):
            contexts.append(dict(context or {}))
            super().__init__(context)

    def catalog_loader(path):
        catalog_reads.append(path)
        return load_catalog()

    engine = RecordingEngine()
    monkeypatch.setattr(wiring, "ClaudeAdapter", RecordingAdapter)
    monkeypatch.setattr(wiring, "PARSER_VERSION", "fixture-jsonl/1")
    monkeypatch.setattr(wiring, "calculator", engine)
    monkeypatch.setattr(wiring, "active_catalog", catalog_loader)
    args = ["--data-dir", str(settings.data_dir), "--source-dir", str(settings.source_dirs[0])]
    assert main(["scan", *args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["parser_version"] == "fixture-jsonl/1"
    assert main(["pricing", "apply", *args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["receipts_added"] == 1
    assert engine.calculations == 1
    assert main(["doctor", *args, "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    with TestClient(create_app(settings, scan_on_start=False), base_url=ORIGIN) as web:
        health = web.get("/api/data-health").json()
        assert health["parser_version"] == "fixture-jsonl/1"
        assert health["roots"] == doctor["sources"] == source_status(settings)
        before = web.get("/api/overview").json()["total"]
        assert before["known_tokens"] == 450 and before["known_cost_nanos"] == 1_620_000
        token = re.search(r'name="scan-token" content="([^"]+)"', web.get("/").text)[1]
        refreshed = web.post("/api/scan", headers={"Origin": ORIGIN, "X-Scan-Token": token})
        assert refreshed.status_code == 200
        assert refreshed.json()["scan"]["parser_version"] == "fixture-jsonl/1"
        assert refreshed.json()["pricing"]["receipts_added"] == 0
        assert web.get("/api/overview").json()["total"] == before
    assert any(contexts)  # per-file turn context survived through the injected factory
    assert catalog_reads and all(path == settings.data_dir for path in catalog_reads)
    with connect(settings.database, readonly=True) as conn:
        assert verify_receipts(conn, runtime=wiring.default_runtime())["ok"]
    assert engine.replays == 1


def test_default_mode_change_reaches_cli_web_and_direct_pricing(settings, monkeypatch, capsys):
    monkeypatch.setattr(policy, "DEFAULT_COST_MODE", "strict")
    scan(settings)
    assert apply_pricing(settings)["policy"]["assume_provider"] is None
    assert main(["pricing", "apply", "--data-dir", str(settings.data_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["policy"]["assume_provider"] is None
    with TestClient(create_app(settings, scan_on_start=False), base_url=ORIGIN) as web:
        assert web.get("/api/data-health").json()["new_request_policy"]["assume_provider"] is None
        assert web.get("/api/overview").json()["total"]["cost_status"] == "UNPRICED"


class OtherAgentAdapter(ClaudeAdapter):
    """Same synthetic input format, separate identity namespace; not a production adapter."""

    def discover(self, settings):
        for source in super().discover(settings):
            yield SourceFile(source.path, "fixture-agent")

    def parse(self, record):
        for event in super().parse(record):
            event = replace(event, agent_type="fixture-agent")
            if isinstance(event, Project):
                event = replace(event, project_id="fixture:" + event.project_id)
            elif isinstance(event, Session) and event.project_id:
                event = replace(event, project_id="fixture:" + event.project_id)
            yield event


def test_source_namespaces_pricing_scope_and_missing_file_isolation(settings):
    source = JSONLSource("fixture-agent", "fixture-jsonl/1", OtherAgentAdapter)
    runtime = Runtime(source, RecordingEngine("fixture-agent"), lambda _: load_catalog())
    scan(settings)
    assert scan(settings, source=source)["rows_added"] == 1
    assert apply_pricing(settings)["receipts_added"] == 1  # never price another agent implicitly
    with connect(settings.database, readonly=True) as conn:
        total = overview(conn, ReportFilter("UTC"))["total"]
        assert total["requests"] == 2 and total["unpriced_requests"] == 1
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2
    assert apply_pricing(settings, runtime=runtime)["receipts_added"] == 1
    with connect(settings.database, readonly=True) as conn:
        total = overview(conn, ReportFilter("UTC"))["total"]
        assert total["known_cost_nanos"] == 3_240_000 and total["unpriced_requests"] == 0
    (settings.source_dirs[0] / "normal.jsonl").unlink()
    scan(settings, source=source)
    with connect(settings.database, readonly=True) as conn:
        states = dict(
            conn.execute(
                "SELECT f.agent_type,o.source_available FROM request_observations o "
                "JOIN source_files f ON f.file_id=o.source_file_id"
            )
        )
        assert states == {"claude": 1, "fixture-agent": 0}


def test_mislabelled_adapter_records_are_rolled_back(settings):
    class WrongEvents(ClaudeAdapter):
        def parse(self, record):
            for event in super().parse(record):
                yield replace(event, agent_type="wrong")

    result = scan(settings, source=JSONLSource("claude", "fixture-jsonl/1", WrongEvents))
    assert result["malformed_rows"] > 0
    assert result["ledger"]["requests"] == 0
    with connect(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_mislabelled_discovery_fails_before_ingesting_file(settings):
    source = JSONLSource("claude", "fixture-jsonl/1", OtherAgentAdapter)
    with pytest.raises(ValueError, match="discovered_source_agent_mismatch"):
        scan(settings, source=source)
    with connect(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM source_files").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "failed"


def test_injected_pricing_failure_rolls_back_the_whole_batch(settings):
    shutil.copyfile(FIXTURES / "mixed-models.jsonl", settings.source_dirs[0] / "normal.jsonl")
    scan(settings)

    class FailingEngine(RecordingEngine):
        def calculate(self, *args, **kwargs):
            if self.calculations == 1:
                raise ValueError("fixture failure")
            return super().calculate(*args, **kwargs)

    runtime = replace(wiring.default_runtime(), pricing=FailingEngine())
    with pytest.raises(ValueError, match="fixture failure"):
        apply_pricing(settings, runtime=runtime)
    with connect(settings.database, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM cost_receipts").fetchone()[0] == 0
