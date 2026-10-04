import json
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from claude_metrics.cli import main


def test_init_then_doctor_and_repeat_init(tmp_path, capsys):
    assert main(["init", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["schema_version"] == 3
    assert Path(created["database"]).is_relative_to(tmp_path)
    assert main(["init", "--json"]) == 0
    capsys.readouterr()
    assert main(["doctor", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["database"]["ok"]
    assert report["catalog"]["verified"]
    assert report["warnings"]  # missing logs are a setup warning, not a fatal error
    assert report["capabilities"] == {
        "ingestion": True,
        "request_pricing": True,
        "dashboard": True,
    }


def test_doctor_does_not_create_database(tmp_path, capsys):
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["database"]["status"] == "not_initialized"
    assert not (tmp_path / "app").exists()


def test_invalid_config_returns_actionable_json(capsys):
    assert main(["doctor", "--timezone", "Fixture/Invalid", "--json"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert not error["ok"] and "IANA" in error["error"]


def test_module_and_console_entrypoints_work_away_from_checkout(tmp_path):
    for invocation in (
        [sys.executable, "-m", "claude_metrics"],
        [sys.executable, "-m", "claude_metrics.cli"],
        [str(Path(sys.executable).with_name("claude-metrics.exe"))]
        if sys.platform == "win32"
        else [str(Path(sys.executable).with_name("claude-metrics"))],
    ):
        result = subprocess.run(
            [*invocation, "pricing", "info", "--json"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["verified"]


def test_editing_one_config_reaches_scan_pricing_reports_and_serve(tmp_path, monkeypatch, capsys):
    for key in ("DATA_DIR", "SOURCE_DIRS", "TIMEZONE"):
        monkeypatch.delenv(f"CLAUDE_METRICS_{key}")
    fixture = Path(__file__).parents[1] / "fixtures/claude/normal.jsonl"
    served = []

    def serve(app, **options):
        assert options["host"] == "127.0.0.1"
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            served.append(
                (client.get("/api/data-health").json(), client.get("/api/overview").json())
            )

    monkeypatch.setattr("uvicorn.run", serve)
    for name, timezone in (("first", "UTC"), ("second", "Asia/Kolkata")):
        source = tmp_path / name / "projects"
        source.mkdir(parents=True)
        shutil.copyfile(fixture, source / "normal.jsonl")
        (tmp_path / "settings.toml").write_text(
            f'[app]\ndata_dir="{name}/db"\nsource_dirs=["{name}/projects"]\n'
            f'timezone="{timezone}"\n',
            encoding="utf-8",
        )
        assert main(["doctor", "--json"]) == 0
        doctor = json.loads(capsys.readouterr().out)
        assert Path(doctor["database_path"]) == tmp_path / name / "db/usage.sqlite3"
        assert doctor["timezone"] == timezone
        assert doctor["sources"] == [{"path": str(source), "status": "available"}]
        assert not (tmp_path / name / "db").exists()
        assert main(["scan", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ledger"]["requests"] == 1
        assert main(["pricing", "apply", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["receipts_added"] == 1
        assert main(["report", "overview", "--json"]) == 0
        total = json.loads(capsys.readouterr().out)["total"]
        assert total["known_tokens"] == 450
        assert total["known_cost_nanos"] == 1_620_000
        assert main(["pricing", "verify", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
        assert main(["serve"]) == 0
        capsys.readouterr()
        health, overview = served[-1]
        assert health["roots"] == doctor["sources"]
        assert health["last_refresh"]["ok"]
        assert overview["total"] == total
    assert len(served) == 2


def test_invalid_local_settings_fail_before_initialization(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_METRICS_DATA_DIR")
    (tmp_path / "settings.toml").write_text("[app]\nunknown=1", encoding="utf-8")
    assert main(["init", "--json"]) == 1
    assert "configuration" in json.loads(capsys.readouterr().out)["error"]
    assert not (tmp_path / ".local").exists()
