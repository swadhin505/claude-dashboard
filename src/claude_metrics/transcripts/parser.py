"""Claude content parsing only: no SQL, paths, accounting, or external asset reads.

Keep source order (including retries/forks), not a guessed canonical conversation.
Only adjacent compatible assistant snapshots coalesce. Tool results resolve along
UUID ancestry where available; ambiguous/orphaned results remain visible separately.
"""

import json
from collections.abc import Iterable

from claude_metrics.transcripts.models import Block, Message, Result, Transcript

MAX_BLOCKS = 256
MAX_ANCESTRY = 1024


def string(value: object) -> str | None:
    # JSON permits escaped lone surrogates; keep invalid Unicode out of HTML/JSON responses.
    return (
        value.encode("utf-8", "replace").decode("utf-8")
        if isinstance(value, str) and value
        else None
    )


def readable(value: object) -> str:
    """Do not render embedded images/documents or follow paths in tool results."""
    if isinstance(value, str):
        return string(value) or ""
    if isinstance(value, list):
        parts = []
        for block in value[:MAX_BLOCKS]:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(string(block.get("text")) or "")
            else:
                parts.append("[Non-text content omitted]")
        if len(value) > MAX_BLOCKS:
            parts.append("[Additional content blocks omitted]")
        return "\n\n".join(parts)
    return json.dumps(value, ensure_ascii=True, indent=2)


def content_blocks(content: object, line: int, timestamp: str | None) -> list[Block]:
    if isinstance(content, str):
        return [Block("text", string(content))] if content else []
    if not isinstance(content, list):
        return []
    result = []
    for raw in content[:MAX_BLOCKS]:
        if not isinstance(raw, dict):
            result.append(Block("notice", "[Unrecognized content block omitted]"))
            continue
        kind = raw.get("type")
        if kind in ("text", "thinking"):
            result.append(Block(kind, string(raw.get(kind)) or ""))
        elif kind == "tool_use":
            result.append(
                Block(
                    kind,
                    json.dumps(raw["input"], ensure_ascii=True, indent=2)
                    if "input" in raw
                    else "[Input not recorded]",
                    string(raw.get("id")),
                    string(raw.get("name")) or "Unnamed tool",
                )
            )
        elif kind == "tool_result":
            error = raw.get("is_error")
            status = "error" if error is True else "success" if error is False else "recorded"
            result.append(
                Block(
                    kind,
                    "",
                    string(raw.get("tool_use_id")),
                    results=[
                        Result(
                            readable(raw["content"])
                            if "content" in raw
                            else "[Output not recorded]",
                            status,
                            line,
                            timestamp,
                        )
                    ],
                )
            )
        else:
            label = string(kind) or "unknown"
            result.append(Block("notice", f"[{label} content not displayed]"))
    if len(content) > MAX_BLOCKS:
        result.append(Block("notice", "[Additional content blocks omitted]"))
    return result


def _aligned(a: Block, b: Block) -> bool:
    if a.kind != b.kind:
        return False
    if a.kind == "tool_use" and a.tool_id and b.tool_id:
        return a.tool_id == b.tool_id
    if a.kind in ("text", "thinking"):
        return a.text.startswith(b.text) or b.text.startswith(a.text)
    return a == b


def _merge(previous: Message, current: Message) -> None:
    cumulative = not previous.ended and all(
        _aligned(a, b) for a, b in zip(previous.blocks, current.blocks, strict=False)
    )
    if cumulative:
        for index, block in enumerate(current.blocks):
            if index == len(previous.blocks):
                previous.blocks.append(block)
            elif block.kind == "tool_use" or len(block.text) >= len(previous.blocks[index].text):
                previous.blocks[index] = block
    else:
        for block in current.blocks:
            if block not in previous.blocks and not (
                block.kind == "tool_use"
                and block.tool_id
                and any(
                    b.kind == "tool_use" and b.tool_id == block.tool_id for b in previous.blocks
                )
            ):
                previous.blocks.append(block)
    previous.last_line = current.last_line
    previous.ended = previous.ended or current.ended
    previous.request_id = current.request_id or previous.request_id
    previous.model = current.model or previous.model


def parse(records: Iterable[tuple[int, dict]], session_id: str, result: Transcript) -> None:
    # Nodes include meta records to keep ancestry intact, but only eligible content is shown.
    nodes: dict[str, tuple[str | None, Message | None]] = {}
    ambiguous_uuids: set[str] = set()
    calls: dict[tuple[str, str], dict[int, list[Block]]] = {}
    previous: Message | None = None
    previous_uuid: str | None = None
    child_parents: set[tuple[str, str]] = set()
    for line, entry in records:
        if entry.get("sessionId") != session_id:
            previous = None
            continue
        kind = entry.get("type")
        if kind not in ("user", "assistant", "system"):
            # Progress records repeat nested content; they are not additional messages.
            continue
        payload = entry.get("message")
        payload = payload if isinstance(payload, dict) else {}
        uuid, parent = string(entry.get("uuid")), string(entry.get("parentUuid"))
        stream = string(entry.get("agentId")) or (
            "sidechain" if entry.get("isSidechain") is True else "main"
        )
        if uuid in nodes:
            # Repeated UUIDs may be snapshots; do not silently discard conflicting records.
            ambiguous_uuids.add(uuid)
            result.warn("Repeated message UUIDs found. Source order is preserved.")
        role = kind
        if entry.get("isCompactSummary") is True:
            role = "summary"
        elif entry.get("isMeta") is True:
            role = "context"
        elif kind == "system" and entry.get("subtype") == "compact_boundary":
            role = "summary"
        timestamp = string(entry.get("timestamp"))
        blocks = content_blocks(payload.get("content", entry.get("content")), line, timestamp)
        if role == "summary" and not blocks:
            blocks = [Block("notice", "Context compaction boundary")]
        current = Message(
            role,
            line,
            line,
            timestamp,
            uuid,
            parent,
            string(payload.get("id")),
            string(entry.get("requestId")),
            string(payload.get("model")),
            stream,
            blocks,
            payload.get("stop_reason") == "end_turn",
        )
        compatible = (
            previous is not None
            and role == previous.role == "assistant"
            and current.message_id is not None
            and current.message_id == previous.message_id
            and current.stream == previous.stream
            and (not parent or parent == previous_uuid)
            and (
                not current.request_id
                or not previous.request_id
                or current.request_id == previous.request_id
            )
        )
        if parent and uuid and not compatible:
            key = (stream, parent)
            if key in child_parents:
                result.warn("Branches or retries found. All records are shown in source order.")
            child_parents.add(key)
        if compatible:
            _merge(previous, current)
            current = previous
            if len(current.blocks) > MAX_BLOCKS:
                current.blocks = current.blocks[:MAX_BLOCKS]
                result.warn("A streamed reply exceeded 256 blocks. Additional blocks were omitted.")
        else:
            result.messages.append(current)
        if uuid:
            nodes[uuid] = (None, None) if uuid in ambiguous_uuids else (parent, current)
        previous, previous_uuid = current, uuid

    # Pair after coalescing, so cumulative snapshots cannot duplicate tool outputs.
    for message in result.messages:
        kept = []
        for block in message.blocks:
            if block.kind == "tool_use" and block.tool_id:
                owners = calls.setdefault((message.stream, block.tool_id), {})
                owners.setdefault(id(message), []).append(block)
            if block.kind != "tool_result" or not block.tool_id:
                kept.append(block)
                continue
            candidates = calls.get((message.stream, block.tool_id), {})
            if not candidates:
                kept.append(block)
                continue
            target = None
            if message.parent_uuid:
                ancestor = message.parent_uuid
                visited = set()
                # Bound malformed/cyclic ancestry. Never pair across an unresolved branch.
                while ancestor in nodes and ancestor not in visited:
                    if len(visited) == MAX_ANCESTRY:
                        result.warn(
                            "Tool ancestry lookup limit reached; unmatched results remain visible."
                        )
                        break
                    visited.add(ancestor)
                    ancestor, owner = nodes[ancestor]
                    matches = candidates.get(id(owner), [])
                    if len(matches) == 1:
                        target = matches[0]
                        break
            elif len(candidates) == 1:
                matches = next(iter(candidates.values()))
                if len(matches) == 1:
                    target = matches[0]
            if target is not None:
                target.results.extend(block.results)
            else:
                kept.append(block)
        message.blocks = kept
        if message.role == "user" and kept and all(b.kind == "tool_result" for b in kept):
            message.role = "tool"
    result.messages = [m for m in result.messages if m.blocks]
