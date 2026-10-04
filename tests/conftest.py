import pytest


@pytest.fixture(autouse=True)
def isolate_user_state(tmp_path, monkeypatch):
    """Every CLI test stays in temp storage even when a flag is accidentally omitted."""
    # Never auto-load the developer's editable settings.toml during tests.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_METRICS_DATA_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("CLAUDE_METRICS_SOURCE_DIRS", str(tmp_path / "projects"))
    monkeypatch.setenv("CLAUDE_METRICS_TIMEZONE", "Asia/Kolkata")
