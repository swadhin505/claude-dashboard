import sqlite3

import pytest

from claude_metrics.storage import (
    Migration,
    MigrationError,
    bundled_migrations,
    connect,
    inspect_database,
    migrate,
)


def test_migrations_are_idempotent_and_durable(tmp_path):
    path = tmp_path / "nested/ledger.sqlite3"
    with connect(path) as conn:
        assert migrate(conn) == 3
        conn.execute("INSERT INTO sessions(agent_type, session_id) VALUES ('claude', 's1')")
        before = tuple(conn.execute("SELECT * FROM schema_migrations").fetchone())
        assert migrate(conn) == 3
        assert tuple(conn.execute("SELECT * FROM schema_migrations").fetchone()) == before
        assert inspect_database(conn)["ok"]
    with connect(path, readonly=True) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM sessions")


def test_schema_contains_mvp_tables_but_no_live_table(tmp_path):
    with connect(tmp_path / "db.sqlite3") as conn:
        migrate(conn)
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert names == {
            "schema_migrations",
            "ingestion_runs",
            "source_files",
            "projects",
            "sessions",
            "turns",
            "requests",
            "request_observations",
            "tool_calls",
            "billable_units",
            "price_catalog_versions",
            "cost_receipts",
            "ingestion_diagnostics",
            "replay_aliases",
        }


def test_failed_migration_rolls_back_schema_and_history(tmp_path):
    with connect(tmp_path / "db.sqlite3") as conn:
        migrate(conn)
        bad = Migration(
            4, "004_bad.sql", "CREATE TABLE should_rollback(x INTEGER);\nINVALID SQL;\n"
        )
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, (*bundled_migrations(), bad))
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 3
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='should_rollback'"
        ).fetchall()
        assert inspect_database(conn)["ok"]


def test_changed_or_newer_migrations_are_rejected(tmp_path):
    with connect(tmp_path / "db.sqlite3") as conn:
        migrate(conn)
        first = bundled_migrations()[0]
        with pytest.raises(MigrationError, match="changed"):
            migrate(
                conn,
                [Migration(1, first.name, first.sql + "\n-- changed"), *bundled_migrations()[1:]],
            )
        with pytest.raises(MigrationError, match="newer"):
            migrate(conn, [])


def test_request_identity_is_scoped_and_counters_nullable(tmp_path):
    with connect(tmp_path / "db.sqlite3") as conn:
        migrate(conn)
        for session in ("a", "b"):
            conn.execute(
                "INSERT INTO sessions(agent_type, session_id) VALUES ('claude', ?)", (session,)
            )
        sql = """INSERT INTO requests
                 (session_pk, dedup_key, occurred_at_us, model_raw, output_tokens)
                 VALUES (?, 'same-message', 1, 'fixture-model', ?)"""
        conn.execute(sql, (1, None))
        conn.execute(sql, (2, 3))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (1, 4))
        for session_pk, count in ((999, 1), (1, -1), (1, 1.5)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO requests(session_pk, dedup_key, occurred_at_us,
                    model_raw, output_tokens) VALUES (?, 'different', 1, 'fixture-model', ?)""",
                    (session_pk, count),
                )
        assert (
            conn.execute("SELECT output_tokens FROM requests WHERE session_pk=1").fetchone()[0]
            is None
        )


def test_turn_cannot_be_assigned_to_another_session(tmp_path):
    with connect(tmp_path / "db.sqlite3") as conn:
        migrate(conn)
        conn.executemany(
            "INSERT INTO sessions(agent_type, session_id) VALUES ('claude', ?)", [("a",), ("b",)]
        )
        conn.execute("INSERT INTO turns(session_pk, turn_id) VALUES (1, 'turn-a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("""INSERT INTO requests(session_pk, turn_pk, dedup_key, occurred_at_us,
                         model_raw) VALUES (2, 1, 'key', 1, 'fixture-model')""")
