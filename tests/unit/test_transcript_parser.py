"""Synthetic transcripts only; no personal source folders or accounting projections."""

from claude_metrics.transcripts.models import Transcript
from claude_metrics.transcripts.parser import parse


def record(role, content, *, sid="session", uuid=None, parent=None, mid=None, **extra):
    return {
        "type": role,
        "sessionId": sid,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": "2026-10-01T12:00:00Z",
        "message": {"id": mid, "content": content},
        **extra,
    }


def text(value):
    return {"type": "text", "text": value}


def call(tool_id="tool", **kwargs):
    return {"type": "tool_use", "id": tool_id, "name": "Bash", "input": kwargs}


def output(tool_id="tool", content="test output", **kwargs):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, **kwargs}


def parsed(*entries):
    result = Transcript()
    parse(enumerate(entries, 1), "session", result)
    return result


def test_prompt_reply_tool_and_thinking_order_and_pairing():
    result = parsed(
        record("user", "Run tests", uuid="u"),
        record(
            "assistant",
            [
                {"type": "thinking", "thinking": "Check tests"},
                text("Testing now"),
                call(command="pytest"),
            ],
            uuid="a",
            parent="u",
        ),
        record("user", [output(content=[text("passed")], is_error=False)], uuid="r", parent="a"),
        record("assistant", "All done", uuid="b", parent="r"),
    )
    assert [m.role for m in result.messages] == ["user", "assistant", "assistant"]
    blocks = result.messages[1].blocks
    assert [b.kind for b in blocks] == ["thinking", "text", "tool_use"]
    assert '"command": "pytest"' in blocks[2].text
    assert blocks[2].results[0].text == "passed"
    assert blocks[2].results[0].status == "success"
    assert blocks[2].results[0].line == 3


def test_cumulative_and_additive_chunks_merge_only_adjacent_compatible_identity():
    a = record("assistant", [text("Hel")], mid="m", uuid="a")
    b = record("assistant", [text("Hello"), call(command="ls")], mid="m", uuid="b", parent="a")
    c = record("assistant", [text("More")], mid="m", uuid="c", parent="b")
    result = parsed(a, b, c, record("user", "again"), a)
    assert len(result.messages) == 3
    assert [b.text for b in result.messages[0].blocks if b.kind == "text"] == ["Hello", "More"]
    assert result.messages[0].last_line == 3
    assert result.messages[-1].blocks[0].text == "Hel"


def test_end_turn_does_not_replace_a_distinct_later_block():
    a = record("assistant", [text("Hello")], mid="m")
    a["message"]["stop_reason"] = "end_turn"
    result = parsed(a, record("assistant", [text("Hello again")], mid="m"))
    assert [b.text for b in result.messages[0].blocks] == ["Hello", "Hello again"]


def test_different_request_or_stream_or_branch_never_coalesces():
    for extra in ({"requestId": "other"}, {"agentId": "worker"}, {"parentUuid": "root"}):
        a = record("assistant", [text("a")], mid="m", uuid="a", requestId="first")
        b = record("assistant", [text("ab")], mid="m", parent="a", **extra)
        assert len(parsed(a, b).messages) == 2


def test_results_follow_ancestry_not_last_call_with_same_id():
    result = parsed(
        record("user", "root", uuid="root"),
        record("assistant", [call(command="first")], uuid="first", parent="root"),
        record("assistant", [call(command="second")], uuid="second", parent="root"),
        record("user", [output()], uuid="r", parent="first"),
    )
    assert result.messages[1].blocks[0].results[0].text == "test output"
    assert result.messages[2].blocks[0].results == []
    assert any("Branches" in w for w in result.warnings)


def test_unresolved_parent_and_ambiguous_calls_keep_orphan_result_visible():
    for parent in (None, "missing"):
        result = parsed(
            record("assistant", [call()]),
            record("assistant", [call()]),
            record("user", [output()], parent=parent),
        )
        assert result.messages[-1].role == "tool"
        assert not result.messages[0].blocks[0].results
        assert not result.messages[1].blocks[0].results


def test_progress_and_other_session_content_cannot_leak_or_pair():
    result = parsed(
        record("assistant", [call()]),
        record("progress", [text("nested duplicate")]),
        record("user", [output(content="other session")], sid="other"),
        record("user", [output(content="worker output")], agentId="worker"),
    )
    assert len(result.messages) == 2
    assert result.messages[0].blocks[0].results == []
    assert result.messages[1].blocks[0].results[0].text == "worker output"


def test_mixed_user_prompt_and_results_keep_prompt_and_do_not_duplicate_output():
    result = parsed(
        record("assistant", [call()]),
        record("user", [output(), text("Also check formatting")]),
    )
    assert result.messages[-1].role == "user"
    assert len(result.messages[-1].blocks) == 1
    assert result.messages[-1].blocks[0].text == "Also check formatting"
    assert len(result.messages[0].blocks[0].results) == 1


def test_absent_status_is_not_success_and_explicit_error_preserved():
    result = parsed(
        record("assistant", [call("a"), call("b"), call("c")]),
        record("user", [output("a"), output("b", is_error=True)]),
    )
    blocks = result.messages[0].blocks
    assert blocks[0].results[0].status == "recorded"
    assert blocks[1].results[0].status == "error"
    assert not blocks[2].results


def test_context_compaction_and_attachments_are_explicit():
    result = parsed(
        record("user", "injected", isMeta=True),
        record("user", "summary", isCompactSummary=True),
        record("system", [], subtype="compact_boundary"),
        record("user", [{"type": "image", "source": {"data": "SECRET_BASE64"}}]),
    )
    assert [m.role for m in result.messages] == ["context", "summary", "summary", "user"]
    assert "SECRET_BASE64" not in repr(result)
    assert result.messages[-1].blocks[0].text == "[image content not displayed]"


def test_streamed_tool_snapshots_update_input_and_pair_once():
    result = parsed(
        record("assistant", [call(command="p")], mid="m", uuid="a"),
        record("assistant", [call(command="pytest")], mid="m", uuid="b", parent="a"),
        record("user", [output()], parent="b"),
    )
    assert len(result.messages) == 1
    assert "pytest" in result.messages[0].blocks[0].text
    assert len(result.messages[0].blocks[0].results) == 1


def test_invalid_unicode_and_unknown_blocks_are_safe():
    result = parsed(record("user", [text("\ud800"), None, {"type": ["invalid"]}]))
    assert result.messages[0].blocks[0].text == "?"
    assert len(result.messages[0].blocks) == 3


def test_tool_input_preserves_arbitrary_json_not_just_content_blocks():
    import json

    block = call()
    block["input"] = ["argument", {"count": 9999999999999999}, None, False]
    result = parsed(record("assistant", [block]))
    assert json.loads(result.messages[0].blocks[0].text) == block["input"]


def test_duplicate_uuid_is_not_enough_to_pair_a_result():
    result = parsed(
        record("assistant", [call()], uuid="reused"),
        record("assistant", [call()], uuid="reused"),
        record("user", [output()], parent="reused"),
    )
    assert result.messages[-1].role == "tool"
    assert all(not m.blocks[0].results for m in result.messages[:2])


def test_cyclic_parent_links_do_not_hang_or_mispair():
    result = parsed(
        record("user", "a", uuid="a", parent="b"),
        record("user", "b", uuid="b", parent="a"),
        record("user", [output()], parent="a"),
    )
    assert result.messages[-1].role == "tool"


def test_ancestry_budget_leaves_result_visible_instead_of_guessing(monkeypatch):
    from claude_metrics.transcripts import parser

    monkeypatch.setattr(parser, "MAX_ANCESTRY", 1)
    result = parsed(
        record("assistant", [call()], uuid="call"),
        record("user", "context", uuid="context", parent="call"),
        record("user", [output()], parent="context"),
    )
    assert not result.messages[0].blocks[0].results
    assert result.messages[-1].role == "tool"
    assert any("ancestry lookup limit" in w for w in result.warnings)
