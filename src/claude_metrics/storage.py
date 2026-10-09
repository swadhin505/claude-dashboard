"""SQLite connection ownership and transactional, checksum-verified migrations."""

import hashlib
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from claude_metrics.project_paths import canonical_project_root


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def bundled_migrations() -> tuple[Migration, ...]:
    root = files("claude_metrics").joinpath("migrations")
    return tuple(
        Migration(int(item.name.split("_", 1)[0]), item.name, item.read_text("utf-8"))
        for item in sorted(root.iterdir(), key=lambda item: item.name)
        if item.name.endswith(".sql")
    )


@contextmanager
def connect(path: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    if not readonly:
        path.parent.mkdir(parents=True, exist_ok=True)
    target = path.resolve().as_uri() + "?mode=ro" if readonly else str(path)
    conn = sqlite3.connect(target, uri=readonly, isolation_level=None, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if readonly:
            conn.execute("PRAGMA query_only = ON")
        else:
            if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise MigrationError("database must support WAL mode")
            conn.execute("PRAGMA synchronous = FULL")
        yield conn
    finally:
        conn.close()


def _statements(sql: str) -> Iterator[str]:
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            yield buffer
            buffer = ""
    if buffer.strip():
        raise MigrationError("incomplete migration SQL")


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None) -> int:
    selected = bundled_migrations() if migrations is None else tuple(migrations)
    if [item.version for item in selected] != list(range(1, len(selected) + 1)):
        raise MigrationError("migration versions must be contiguous from 1")
    if conn.in_transaction:
        raise MigrationError("migrations need their own transaction")
    conn.create_function(
        "sha256_hex",
        1,
        lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest(),
        deterministic=True,
    )
    conn.create_function(
        "canonical_project_root", 1, canonical_project_root, deterministic=True
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ) STRICT""")
        applied = {row["version"]: row for row in conn.execute("SELECT * FROM schema_migrations")}
        if sorted(applied) != list(range(1, len(applied) + 1)) or len(applied) > len(selected):
            raise MigrationError("database has an unknown/newer migration history")
        for migration in selected:
            if migration.version in applied:
                old = applied[migration.version]
                if old["checksum"] != migration.checksum or old["name"] != migration.name:
                    raise MigrationError(f"applied migration {migration.version} was changed")
                continue
            for statement in _statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
                (migration.version, migration.name, migration.checksum),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return len(selected)


def inspect_database(conn: sqlite3.Connection) -> dict:
    expected = bundled_migrations()
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
    actual = [(row["version"], row["name"], row["checksum"]) for row in rows]
    wanted = [(item.version, item.name, item.checksum) for item in expected]
    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    foreign_key_errors = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return {
        "schema_version": len(actual),
        "schema_current": actual == wanted,
        "integrity": integrity,
        "foreign_key_errors": foreign_key_errors,
        "journal_mode": journal_mode,
        "ok": actual == wanted
        and integrity == "ok"
        and foreign_key_errors == 0
        and journal_mode == "wal",
    }
