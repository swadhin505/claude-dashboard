import json

import pytest

from claude_metrics.transcripts import reader
from claude_metrics.transcripts.reader import SourceUnavailable, read
from claude_metrics.transcripts.service import conversation


def row(text="hello", sid="session"):
    return json.dumps({"type": "user", "sessionId": sid, "message": {"content": text}}) + "\n"


def test_malformed_incomplete_and_mixed_session_lines(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(row() + "broken\n[]\n" + row("private", "other") + row("partial")[:-1])
    result = read(source, (tmp_path,), "session")
    assert len(result.messages) == 1
    assert result.messages[0].blocks[0].text == "hello"
    assert len(result.warnings) == 3


def test_missing_outside_root_and_non_jsonl_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = tmp_path / "outside.jsonl"
    source.write_text(row())
    with pytest.raises(SourceUnavailable, match="outside"):
        read(source, (root,), "session")
    with pytest.raises(SourceUnavailable, match="missing or unreadable"):
        read(root / "missing.jsonl", (root,), "session")
    with pytest.raises(SourceUnavailable, match="not a supported"):
        read(root / "secret.txt", (root,), "session")


def test_symlink_source_refused(tmp_path):
    source = tmp_path / "original.jsonl"
    source.write_text(row())
    linked = tmp_path / "link.jsonl"
    try:
        linked.symlink_to(source)
    except OSError:
        pytest.skip("Symlink creation is not permitted on this Windows account")
    with pytest.raises(SourceUnavailable, match="redirected"):
        read(linked, (tmp_path,), "session")


def test_oversized_line_is_skipped_and_following_line_keeps_source_number(tmp_path, monkeypatch):
    source = tmp_path / "session.jsonl"
    source.write_text(row("x" * 500) + row("after"))
    monkeypatch.setattr(reader, "MAX_LINE_BYTES", 120)
    result = read(source, (tmp_path,), "session")
    assert result.messages[0].line == 2
    assert result.messages[0].blocks[0].text == "after"
    assert any("oversized" in w for w in result.warnings)


def test_file_and_record_budgets_are_explicit(tmp_path, monkeypatch):
    source = tmp_path / "session.jsonl"
    source.write_bytes((row() * 5).encode("utf-8"))
    monkeypatch.setattr(reader, "MAX_RECORDS", 2)
    result = read(source, (tmp_path,), "session")
    assert len(result.messages) == 2
    assert any("Record limit" in w for w in result.warnings)
    monkeypatch.setattr(reader, "MAX_RECORDS", 20_000)
    monkeypatch.setattr(reader, "MAX_FILE_BYTES", len(row()) * 2)
    result = read(source, (tmp_path,), "session")
    assert len(result.messages) == 2
    assert any("bounded beginning" in w for w in result.warnings)


def test_no_external_tool_output_file_is_opened(tmp_path):
    secret = tmp_path / "output.txt"
    secret.write_text("SECRET_NOT_REQUESTED")
    source = tmp_path / "session.jsonl"
    source.write_text(row(f"<persisted-output>{secret}</persisted-output>"))
    result = read(source, (tmp_path,), "session")
    assert "SECRET_NOT_REQUESTED" not in repr(result)
    assert "persisted-output" in result.messages[0].blocks[0].text


def test_page_projection_clips_content_and_keeps_source_unchanged(tmp_path):
    source = tmp_path / "session.jsonl"
    content = row("x" * 25_000) * 26
    source.write_text(content)
    choices = [{"file_id": 1, "path": source, "name": source.name, "kind": "main"}]
    session = {"session_id": "session"}
    first = conversation(session, choices, (tmp_path,))
    assert len(first["messages"]) == 25
    assert first["pages"] == 2
    assert "path" not in first["sources"][0]
    assert "Shortened for display" in first["messages"][0]["blocks"][0]["text"]
    assert all(message["role"] == "user" for message in first["messages"])
    second = conversation(session, choices, (tmp_path,), page=2)
    assert len(second["messages"]) == 1 and second["messages"][0]["line"] == 26
    assert source.read_text() == content
    with pytest.raises(LookupError, match="page not found"):
        conversation(session, choices, (tmp_path,), page=3)
    with pytest.raises(LookupError, match="source not found"):
        conversation(session, choices, (tmp_path,), file_id=2)


def test_redirected_resolved_path_is_refused_without_reading_content(tmp_path, monkeypatch):
    from pathlib import Path

    source = tmp_path / "original.jsonl"
    target = tmp_path / "different.jsonl"
    source.write_text(row())
    target.write_text(row("SECRET_TARGET"))
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == source:
            return target
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(SourceUnavailable, match="redirected"):
        read(source, (tmp_path,), "session")


def test_truncation_while_skipping_oversized_record_terminates(monkeypatch):
    from io import BytesIO

    from claude_metrics.transcripts.models import Transcript

    monkeypatch.setattr(reader, "MAX_LINE_BYTES", 10)
    result = Transcript()
    # The file was larger at stat time; now EOF occurs in the oversized-record drain.
    assert list(reader._records(BytesIO(b"x" * 30), 100, result)) == []
    assert any("truncated during" in warning for warning in result.warnings)
