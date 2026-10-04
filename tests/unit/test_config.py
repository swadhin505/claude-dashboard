from pathlib import Path

import pytest

from claude_metrics.config import load_settings


def test_defaults_and_dedicated_database(tmp_path):
    settings = load_settings(environ={}, home=tmp_path, data_dir=tmp_path / "app", timezone="UTC")
    assert settings.source_dirs == (
        tmp_path / ".claude/projects",
        tmp_path / ".config/claude/projects",
    )
    assert settings.database == tmp_path / "app/usage.sqlite3"
    assert not settings.data_dir.exists()


def test_claude_config_override_is_config_root(tmp_path):
    settings = load_settings(
        environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "custom")}, timezone="UTC"
    )
    assert settings.source_dirs == (tmp_path / "custom/projects",)


def test_cli_over_env_over_toml_and_relative_config_paths(tmp_path):
    conf = tmp_path / "settings.toml"
    conf.write_text(
        '[app]\ndata_dir="db"\nsource_dirs=["logs"]\ntimezone="UTC"\n', encoding="utf-8"
    )
    from_file = load_settings(config_file=conf, environ={})
    assert from_file.database == tmp_path / "db/usage.sqlite3"
    assert from_file.source_dirs == (tmp_path / "logs",)
    from_env = load_settings(config_file=conf, environ={"CLAUDE_METRICS_TIMEZONE": "Asia/Kolkata"})
    assert from_env.timezone == "Asia/Kolkata"
    from_cli = load_settings(
        config_file=conf,
        environ={"CLAUDE_METRICS_TIMEZONE": "Asia/Kolkata"},
        timezone="America/New_York",
        source_dirs=[tmp_path / "cli"],
    )
    assert from_cli.timezone == "America/New_York"
    assert from_cli.source_dirs == (tmp_path / "cli",)


@pytest.mark.parametrize(
    "toml",
    [
        "[app]\nunknown=1",
        '[app]\nsource_dirs="wrong"',
        "[app]\nsource_dirs=[]",
        "[app]\ntimezone=3",
        "[wrong]\nx=1",
    ],
)
def test_bad_configuration_rejected(tmp_path, toml):
    conf = tmp_path / "settings.toml"
    conf.write_text(toml, encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(config_file=conf, environ={})


def test_invalid_timezone_does_not_silently_become_utc():
    with pytest.raises(ValueError, match="IANA timezone"):
        load_settings(timezone="Fixture/Invalid", environ={})


def test_failed_autodetection_requires_explicit_timezone(monkeypatch):
    def fail():
        raise ValueError("not detectable")

    monkeypatch.setattr("claude_metrics.config.get_localzone_name", fail)
    with pytest.raises(ValueError, match="--timezone"):
        load_settings(environ={})


def test_duplicate_source_roots_collapse(tmp_path):
    settings = load_settings(
        source_dirs=[tmp_path, tmp_path / Path(".")], timezone="UTC", environ={}
    )
    assert settings.source_dirs == (tmp_path,)


def test_local_settings_are_auto_loaded_without_creating_data(tmp_path):
    (tmp_path / "settings.toml").write_text(
        '[app]\ndata_dir="db"\nsource_dirs=["first", "second"]\ntimezone="UTC"\n',
        encoding="utf-8",
    )
    settings = load_settings(environ={})
    assert settings.data_dir == tmp_path / "db"
    assert settings.source_dirs == (tmp_path / "first", tmp_path / "second")
    assert settings.timezone == "UTC"
    assert not settings.data_dir.exists()


def test_explicit_config_replaces_local_file_and_anchors_relative_paths(tmp_path):
    (tmp_path / "settings.toml").write_text("invalid TOML!", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    conf = elsewhere / "custom.toml"
    conf.write_text('[app]\ndata_dir="db"\ntimezone="UTC"', encoding="utf-8")
    settings = load_settings(config_file=conf, environ={}, home=tmp_path)
    assert settings.data_dir == elsewhere / "db"
    assert settings.source_dirs[0] == tmp_path / ".claude/projects"


def test_explicit_missing_config_does_not_fall_back_to_local_file(tmp_path):
    (tmp_path / "settings.toml").write_text('[app]\ntimezone="UTC"', encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        load_settings(config_file=tmp_path / "missing.toml", environ={})


def test_discovery_does_not_search_parent_directories(tmp_path, monkeypatch):
    (tmp_path / "settings.toml").write_text("invalid TOML!", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    settings = load_settings(environ={}, timezone="UTC", home=tmp_path)
    assert settings.source_dirs[0] == tmp_path / ".claude/projects"


@pytest.mark.parametrize("document", ["invalid TOML!", "[app]\nunknown=1"])
def test_invalid_auto_config_fails_instead_of_silently_using_defaults(tmp_path, document):
    (tmp_path / "settings.toml").write_text(document, encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(environ={})


def test_auto_config_must_be_a_readable_file(tmp_path):
    (tmp_path / "settings.toml").mkdir()
    with pytest.raises(OSError):
        load_settings(environ={})


def test_all_env_and_cli_overrides_win_over_local_settings(tmp_path):
    import os

    (tmp_path / "settings.toml").write_text(
        '[app]\ndata_dir="file-db"\nsource_dirs=["file-logs"]\ntimezone="UTC"',
        encoding="utf-8",
    )
    env = {
        "CLAUDE_METRICS_DATA_DIR": "env-db",
        "CLAUDE_METRICS_SOURCE_DIRS": os.pathsep.join(["env-first", "env-second"]),
        "CLAUDE_METRICS_TIMEZONE": "Asia/Kolkata",
    }
    settings = load_settings(environ=env)
    assert settings.data_dir == tmp_path / "env-db"
    assert settings.source_dirs == (tmp_path / "env-first", tmp_path / "env-second")
    assert settings.timezone == "Asia/Kolkata"
    settings = load_settings(
        environ=env, data_dir=Path("cli-db"), source_dirs=[Path("cli-logs")], timezone="UTC"
    )
    assert settings.data_dir == tmp_path / "cli-db"
    assert settings.source_dirs == (tmp_path / "cli-logs",)
    assert settings.timezone == "UTC"


def test_toml_home_paths_expand(tmp_path):
    (tmp_path / "settings.toml").write_text(
        '[app]\nsource_dirs=["~/.claude/projects"]\ntimezone="UTC"', encoding="utf-8"
    )
    assert load_settings(environ={}).source_dirs == ((Path.home() / ".claude/projects").resolve(),)
