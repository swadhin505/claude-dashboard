"""Coverage for the standalone Claude Code session inspector."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "inspect_claude_sessions.py"
SPEC = importlib.util.spec_from_file_location("inspect_claude_sessions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
inspector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = inspector
SPEC.loader.exec_module(inspector)


def test_inspector_deduplicates_streams_and_pairs_tool_results(tmp_path: Path) -> None:
    transcript = tmp_path / "projects" / "demo" / "session.jsonl"
    transcript.parent.mkdir(parents=True)
    entries = [
        {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "u-1",
            "timestamp": "2026-10-01T10:00:00Z",
            "cwd": "C:/work/demo",
            "message": {"content": [{"type": "text", "text": "Inspect this file"}]},
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "uuid": "a-1",
            "parentUuid": "u-1",
            "requestId": "request-1",
            "timestamp": "2026-10-01T10:00:01Z",
            "effort": "high",
            "message": {
                "id": "message-1",
                "model": "claude-sonnet-4-6",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "I should read it."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "C:/work/demo/example.py"},
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
        {
            "type": "user",
            "sessionId": "session-1",
            "uuid": "u-2",
            "parentUuid": "a-1",
            "timestamp": "2026-10-01T10:00:02Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "print('hello')",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "sessionId": "session-1",
            "uuid": "a-2",
            "parentUuid": "u-2",
            "requestId": "request-1",
            "timestamp": "2026-10-01T10:00:03Z",
            "effort": "high",
            "message": {
                "id": "message-1",
                "model": "claude-sonnet-4-6",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "The file prints hello."}],
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 8,
                    "server_tool_use": {"web_search_requests": 1},
                },
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")

    report = inspector.inspect_session(transcript, "demo")

    assert report["session"]["id"] == "session-1"
    assert report["session"]["models"] == {"claude-sonnet-4-6": 1}
    assert report["counts"]["assistant_stream_snapshots_merged"] == 1
    assert report["usage"] == {
        "input_tokens": 10,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "output_tokens": 8,
        "reasoning_tokens": 0,
        "peak_context_tokens": 15,
        "assistant_usage_records": 1,
    }
    assert report["server_tool_use"] == {"web_search_requests": 1}
    assert report["tool_calls"][0]["results"][0]["content"] == "print('hello')"
    assert report["messages"][1]["content"] == "The file prints hello."

    summary = inspector.inspect_session(transcript, "demo", summary_only=True)

    assert "messages" not in summary
    assert summary["tool_calls"] == [
        {
            "id": "tool-1",
            "name": "Read",
            "category": "Read",
            "result_count": 1,
            "has_error": False,
        }
    ]
