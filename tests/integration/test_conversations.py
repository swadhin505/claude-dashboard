"""End-to-end local browsing with synthetic files and an isolated usage ledger."""

import json

import pytest
from fastapi.testclient import TestClient

from claude_metrics.config import Settings
from claude_metrics.storage import connect
from claude_metrics.web.app import create_app

ORIGIN = "http://127.0.0.1:8765"


def entries(sid="session", prompt="PRIVATE_PROMPT <script>alert(1)</script>"):
    common = {"sessionId": sid, "timestamp": "2026-10-01T12:00:00Z", "cwd": "C:/fixture/project"}
    return [
        {**common, "type": "user", "uuid": "u", "message": {"content": prompt}},
        {
            **common,
            "type": "assistant",
            "uuid": "a",
            "parentUuid": "u",
            "requestId": "req",
            "message": {
                "id": "msg",
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "text", "text": "PRIVATE_REPLY"},
                    {"type": "thinking", "thinking": "PRIVATE_THINKING"},
                    {
                        "type": "tool_use",
                        "id": "tool",
                        "name": "Bash",
                        "input": {"command": "echo PRIVATE_INPUT"},
                    },
                ],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 5,
                },
            },
        },
        {
            **common,
            "type": "user",
            "uuid": "r",
            "parentUuid": "a",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool",
                        "is_error": False,
                        "content": "PRIVATE_OUTPUT <img src=x onerror=alert(1)>",
                    },
                ]
            },
        },
    ]


def write_log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "projects"
    settings = Settings(tmp_path / "data", (root,), "UTC")
    path = root / "session.jsonl"
    write_log(path, entries())
    return settings, path


def test_conversation_html_json_and_ledger_immutability(fixture):
    settings, source = fixture
    original = source.read_bytes()
    with TestClient(create_app(settings), base_url=ORIGIN) as web:
        before = web.get("/api/sessions/1").json()
        with connect(settings.database, readonly=True) as conn:
            ledger_before = "\n".join(conn.iterdump())
        response = web.get("/sessions/1/conversation")
        assert response.status_code == 200, response.text
        html = response.text
        for expected in (
            "PRIVATE_PROMPT",
            "PRIVATE_REPLY",
            "PRIVATE_THINKING",
            "PRIVATE_INPUT",
            "PRIVATE_OUTPUT",
            "Usage &amp; receipts",
            "Recorded thinking",
        ):
            assert expected in html
        assert "<script>alert" not in html and "<img src=x" not in html
        assert "&lt;script&gt;" in html and "&lt;img" in html
        assert 'aria-label="Report filters"' not in html
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in response.headers["content-security-policy"]
        data = web.get("/api/sessions/1/conversation").json()
        assert data["total_messages"] == 2
        assert data["messages"][1]["blocks"][2]["results"][0]["status"] == "success"
        assert web.get("/api/sessions/1").json() == before
        for path in ("/", "/sessions", "/sessions/1", "/api/sessions/1", "/api/data-health"):
            assert "PRIVATE_" not in web.get(path).text
        assert "/sessions/1/conversation" in web.get("/sessions").text
        assert "/sessions/1/conversation" in web.get("/sessions/1").text
        with connect(settings.database, readonly=True) as conn:
            ledger_after = "\n".join(conn.iterdump())
        assert ledger_after == ledger_before and "PRIVATE_" not in ledger_after
        assert source.read_bytes() == original


def test_subagent_sources_are_separate_and_other_session_source_ids_rejected(fixture):
    settings, source = fixture
    records = entries(prompt="WORKER_PROMPT")
    for entry in records:
        entry["agentId"] = "worker"
        if entry["type"] == "assistant":
            entry["requestId"] = "worker-req"
            entry["message"]["id"] = "worker-msg"
    write_log(source.parent / "session" / "subagents" / "agent-worker.jsonl", records)
    write_log(source.parent / "other.jsonl", entries(sid="other", prompt="UNRELATED_PROMPT"))
    with TestClient(create_app(settings), base_url=ORIGIN) as web:
        with connect(settings.database, readonly=True) as conn:
            pk = conn.execute(
                "SELECT session_pk FROM sessions WHERE session_id='session'"
            ).fetchone()[0]
            other = conn.execute(
                "SELECT file_id FROM source_files WHERE canonical_path LIKE '%other.jsonl'"
            ).fetchone()[0]
        path = f"/api/sessions/{pk}/conversation"
        data = web.get(path).json()
        assert len(data["sources"]) == 2
        assert data["source_name"] == "session.jsonl"
        assert "WORKER_PROMPT" not in json.dumps(data["messages"])
        assert "UNRELATED_PROMPT" not in json.dumps(data)
        worker = next(s["file_id"] for s in data["sources"] if s["kind"] == "subagent")
        assert "WORKER_PROMPT" in web.get(path, params={"file_id": worker}).text
        assert web.get(path, params={"file_id": other}).status_code == 404


def test_missing_source_and_changed_roots_preserve_usage(fixture, tmp_path):
    settings, source = fixture
    with TestClient(create_app(settings), base_url=ORIGIN) as web:
        before = web.get("/api/sessions/1").json()
        source.unlink()
        response = web.get("/sessions/1/conversation")
        assert response.status_code == 200 and "Conversation unavailable" in response.text
        assert web.get("/api/sessions/1").json() == before
    write_log(source, entries())
    different = Settings(settings.data_dir, (tmp_path / "different-root",), "UTC")
    with TestClient(create_app(different, scan_on_start=False), base_url=ORIGIN) as web:
        data = web.get("/api/sessions/1/conversation").json()
        assert "outside" in data["unavailable"] and data["messages"] == []


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "file_id=0",
        "file_id=../secret",
        "path=secret",
        "page=1&page=2",
        "model=anything",
        "page=abc",
    ],
)
def test_conversation_parameters_fail_closed(fixture, query):
    settings, _ = fixture
    with TestClient(create_app(settings), base_url=ORIGIN) as web:
        assert web.get("/api/sessions/1/conversation?" + query).status_code == 422
        assert web.get("/sessions/1/conversation?" + query).status_code == 422


def test_missing_session_page_and_loopback_security(fixture):
    settings, _ = fixture
    with TestClient(create_app(settings), base_url=ORIGIN) as web:
        for prefix in ("", "/api"):
            assert web.get(prefix + "/sessions/999/conversation").status_code == 404
            assert web.get(prefix + "/sessions/1/conversation?page=2").status_code == 404
            assert web.get(prefix + "/sessions/0/conversation").status_code == 422
            assert web.get(prefix + "/sessions/9223372036854775808/conversation").status_code == 422
        response = web.get(
            "/api/sessions/1/conversation", headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403
        response = web.get("/sessions/1/conversation", headers={"Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403


def test_new_content_is_read_on_demand_without_scan(fixture):
    settings, source = fixture
    with TestClient(create_app(settings), base_url=ORIGIN) as web:
        first = web.get("/api/sessions/1/conversation").json()
        records = entries() + [
            {"type": "assistant", "sessionId": "session", "message": {"content": "LIVE_APPEND"}}
        ]
        write_log(source, records)
        second = web.get("/api/sessions/1/conversation").json()
        assert second["total_messages"] == first["total_messages"] + 1
        assert "LIVE_APPEND" in json.dumps(second)
