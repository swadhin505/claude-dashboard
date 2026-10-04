"""CLI > CLAUDE_METRICS_* > selected TOML > built-in defaults.

An explicit --config replaces auto-discovery of settings.toml in the working
directory. Never search parents or the installed package for personal settings.
"""

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from platformdirs import user_data_path
from tzlocal import get_localzone_name

DEFAULT_CONFIG_FILENAME = "settings.toml"


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    source_dirs: tuple[Path, ...]
    timezone: str

    @property
    def database(self) -> Path:
        return self.data_dir / "usage.sqlite3"


def load_settings(
    *,
    config_file: Path | None = None,
    data_dir: Path | None = None,
    source_dirs: Sequence[Path] | None = None,
    timezone: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    document = {}
    base = Path.cwd()
    if config_file is None:
        candidate = base / DEFAULT_CONFIG_FILENAME
        try:
            candidate.stat()
        except FileNotFoundError:
            pass  # No local settings: retain the existing built-in defaults.
        else:
            config_file = candidate
    if config_file is not None:
        base = config_file.resolve().parent
        with config_file.open("rb") as stream:
            document = tomllib.load(stream)
        if set(document) - {"app"} or not isinstance(document.get("app", {}), dict):
            raise ValueError("configuration must contain only an [app] table")
    config = document.get("app", {})
    if set(config) - {"data_dir", "source_dirs", "timezone"}:
        raise ValueError("unknown configuration key in [app]")
    for name in ("data_dir", "timezone"):
        if name in config and (not isinstance(config[name], str) or not config[name].strip()):
            raise ValueError(f"{name} must be a non-empty string")
    if "source_dirs" in config and (
        not isinstance(config["source_dirs"], list)
        or not config["source_dirs"]
        or any(not isinstance(item, str) or not item.strip() for item in config["source_dirs"])
    ):
        raise ValueError("source_dirs must be a non-empty array of paths")

    def config_path(value: str) -> Path:
        path = Path(value).expanduser()
        return (base / path).resolve() if not path.is_absolute() else path.resolve()

    if data_dir is not None:
        selected_data = data_dir
    elif env.get("CLAUDE_METRICS_DATA_DIR"):
        selected_data = Path(env["CLAUDE_METRICS_DATA_DIR"])
    elif "data_dir" in config:
        selected_data = config_path(config["data_dir"])
    else:
        selected_data = user_data_path("claude-dashboard", appauthor=False)

    if source_dirs:
        roots = list(source_dirs)
    elif env.get("CLAUDE_METRICS_SOURCE_DIRS"):
        roots = [
            Path(item)
            for item in env["CLAUDE_METRICS_SOURCE_DIRS"].split(os.pathsep)
            if item.strip()
        ]
    elif "source_dirs" in config:
        roots = [config_path(item) for item in config["source_dirs"]]
    elif env.get("CLAUDE_CONFIG_DIR"):
        roots = [Path(env["CLAUDE_CONFIG_DIR"]) / "projects"]
    else:
        xdg = Path(env.get("XDG_CONFIG_HOME", str(user_home / ".config")))
        roots = [user_home / ".claude" / "projects", xdg / "claude" / "projects"]
    if not roots:
        raise ValueError("source directory override must contain at least one path")
    zone = timezone or env.get("CLAUDE_METRICS_TIMEZONE") or config.get("timezone")
    if zone is None:
        try:
            zone = get_localzone_name()
        except (OSError, ValueError, KeyError, ZoneInfoNotFoundError) as exc:
            raise ValueError("cannot detect timezone; set --timezone to an IANA name") from exc
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"invalid IANA timezone: {zone}") from exc
    return Settings(
        selected_data.expanduser().resolve(),
        tuple(dict.fromkeys(root.expanduser().resolve() for root in roots)),
        zone,
    )


def source_status(settings: Settings) -> list[dict[str, str]]:
    """One read-only source-availability definition for diagnostics and dashboard."""
    roots = []
    for root in settings.source_dirs:
        try:
            status = "available" if root.is_dir() else "missing"
        except OSError:
            status = "inaccessible"
        roots.append({"path": str(root), "status": status})
    return roots
