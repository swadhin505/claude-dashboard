"""Source lookup and bounded display projection. No database writes or content cache."""

from dataclasses import asdict
from pathlib import Path

from claude_metrics.transcripts.models import Transcript
from claude_metrics.transcripts.reader import SourceUnavailable, read

PAGE_SIZE = 25
MAX_TEXT_CHARS = 20_000
MAX_PAGE_CHARS = 500_000


def sources(conn, session_pk: int) -> tuple[dict, list[dict]]:
    session = conn.execute(
        "SELECT session_pk, session_id, agent_type, cwd, branch FROM sessions WHERE session_pk=?",
        (session_pk,),
    ).fetchone()
    if session is None or session["agent_type"] != "claude":
        raise LookupError("Claude session not found in this ledger.")
    # Observations carry the source session, including explicitly copied/forked requests.
    # The canonical request owner alone is not proof that its file belongs to this session.
    rows = conn.execute(
        """SELECT file_id,canonical_path FROM source_files WHERE agent_type='claude'
        AND file_id IN (
          SELECT source_file_id FROM request_observations WHERE source_session_pk=?
          UNION SELECT source_file_id FROM turns WHERE session_pk=?
          UNION SELECT source_file_id FROM tool_calls WHERE session_pk=?
        ) ORDER BY file_id""",
        (session_pk, session_pk, session_pk),
    ).fetchall()
    choices = []
    for row in rows:
        path = Path(row["canonical_path"])
        kind = "subagent" if "subagents" in path.parts or path.stem.startswith("agent-") else "main"
        choices.append({"file_id": row["file_id"], "path": path, "name": path.name, "kind": kind})
    choices.sort(key=lambda source: (source["kind"] != "main", source["file_id"]))
    return dict(session), choices


def conversation(
    session: dict,
    choices: list[dict],
    roots: tuple[Path, ...],
    *,
    file_id: int | None = None,
    page: int = 1,
) -> dict:
    if page < 1:
        raise ValueError("Page must be positive.")
    selected = (
        next((s for s in choices if s["file_id"] == file_id), None)
        if file_id
        else (choices[0] if choices else None)
    )
    if file_id is not None and selected is None:
        raise LookupError("Transcript source not found for this session.")
    unavailable = None
    transcript = Transcript()
    if selected:
        try:
            transcript = read(selected["path"], roots, session["session_id"])
        except SourceUnavailable as exc:
            unavailable = str(exc)
    else:
        unavailable = "No original source is linked to this session. Usage history is retained."
    total = len(transcript.messages)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        raise LookupError(
            "Conversation page not found. The source may have changed; reopen page 1."
        )
    messages = []
    remaining = MAX_PAGE_CHARS

    def clip(text: str) -> str:
        nonlocal remaining
        limit = min(MAX_TEXT_CHARS, remaining)
        remaining -= min(len(text), limit)
        if len(text) > limit:
            transcript.warn("Some content is shortened for display. The original log is unchanged.")
            return text[:limit] + "\n[Shortened for display]"
        return text

    for message in transcript.messages[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]:
        item = asdict(message)
        item.pop("ended")
        for key, value in item.items():
            if key != "role" and isinstance(value, str):
                item[key] = clip(value)
        for block in item["blocks"]:
            for key in ("text", "name", "tool_id"):
                if block[key] is not None:
                    block[key] = clip(block[key])
            for output in block["results"]:
                output["text"] = clip(output["text"])
                if output["timestamp"]:
                    output["timestamp"] = clip(output["timestamp"])
        messages.append(item)
    return {
        "session": session,
        "sources": [{k: v for k, v in s.items() if k != "path"} for s in choices],
        "file_id": selected["file_id"] if selected else None,
        "source_name": selected["name"] if selected else None,
        "messages": messages,
        "total_messages": total,
        "page": page,
        "pages": pages,
        "unavailable": unavailable,
        "warnings": transcript.warnings,
    }
