"""Claude JSONL boundary. Raw messages exist only while a record is normalized."""

import hashlib
import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath

from claude_metrics.adapters.base import SourceFile, SourceRecord
from claude_metrics.config import Settings
from claude_metrics.domain import (
    BillableUnit,
    NormalizedEvent,
    Project,
    QuerySource,
    Request,
    Session,
    TokenUsage,
    ToolCall,
    Turn,
    counter,
)
from claude_metrics.project_paths import canonical_project_root
from claude_metrics.units import utc_microseconds

PARSER_VERSION = "claude-jsonl/3"


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def tokens(usage: Mapping) -> TokenUsage:
    combined = usage.get("cache_creation_input_tokens")
    counter(combined, "cache_creation_input_tokens")
    split = mapping(usage.get("cache_creation"))
    if split:
        five = split.get("ephemeral_5m_input_tokens")
        hour = split.get("ephemeral_1h_input_tokens")
        counter(five, "cache_write_5m")
        counter(hour, "cache_write_1h")
        if five is None or hour is None:
            raise ValueError("incomplete_cache_split")
        if combined is not None and combined != five + hour:
            raise ValueError("inconsistent_cache_split")
        unknown = 0
    elif combined is not None:
        five, hour, unknown = 0, 0, combined
    else:
        five = hour = unknown = None
    return TokenUsage(
        input_uncached_tokens=usage.get("input_tokens"),
        cache_read_tokens=usage.get("cache_read_input_tokens"),
        cache_write_5m_tokens=five,
        cache_write_1h_tokens=hour,
        cache_write_unknown_tokens=unknown,
        output_tokens=usage.get("output_tokens"),
        reasoning_tokens=usage.get("reasoning_tokens"),
    )


class ClaudeAdapter:
    def __init__(self, context: dict[str, str] | None = None):
        self.context = dict(context or {})
        self.discovery_warnings: list[tuple[str, str]] = []

    def discover(self, config: Settings) -> Iterable[SourceFile]:
        seen: set[str] = set()
        for root in config.source_dirs:
            if not root.is_dir():
                self.discovery_warnings.append((str(root), "source_missing"))
                continue

            def on_error(error: OSError, root: Path = root) -> None:
                self.discovery_warnings.append((str(error.filename or root), "source_inaccessible"))

            for directory, dirs, files in os.walk(root, onerror=on_error, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not Path(directory, d).is_symlink())
                for name in sorted(files):
                    path = Path(directory, name)
                    if path.suffix.lower() != ".jsonl" or path.is_symlink():
                        continue
                    canonical = str(path.resolve())
                    key = os.path.normcase(canonical)
                    if key not in seen:
                        seen.add(key)
                        yield SourceFile(path=Path(canonical), agent_type="claude")

    def parse(self, record: SourceRecord) -> Iterable[NormalizedEvent]:
        p, source = record.payload, record.source
        kind = p.get("type")
        # Progress events can contain duplicated nested messages. They are not requests.
        if kind not in ("assistant", "user", "system"):
            return
        sid = text(p.get("sessionId"))
        if not sid:
            raise ValueError("missing_session_id")
        timestamp = text(p.get("timestamp"))
        occurred = utc_microseconds(datetime.fromisoformat(timestamp)) if timestamp else None
        cwd = text(p.get("cwd"))
        project_id = None
        if cwd:
            # Source paths may come from another OS; don't resolve them on the host OS.
            display_root = cwd.replace("\\", "/").rstrip("/") or "/"
            root = canonical_project_root(cwd)
            project_id = hashlib.sha256(("claude:" + root).encode()).hexdigest()
            yield Project(
                agent_type="claude",
                project_id=project_id,
                canonical_root=root,
                display_name=PurePosixPath(display_root).name or display_root,
            )
        yield Session(
            agent_type="claude",
            session_id=sid,
            source=source,
            project_id=project_id,
            cwd=cwd,
            branch=text(p.get("gitBranch")),
            started_at_us=occurred,
            ended_at_us=occurred,
        )
        msg = mapping(p.get("message"))
        content = msg.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        agent_id = text(p.get("agentId")) or text(mapping(p.get("data")).get("agentId"))
        path = Path(source.path)
        in_subdir = "subagents" in path.parts
        if not agent_id and in_subdir and path.stem.startswith("agent-"):
            agent_id = path.stem.removeprefix("agent-")
        subagent = in_subdir or bool(agent_id) or p.get("isSidechain") is True
        stream = sid + ":" + (agent_id or ("sidechain" if subagent else "main"))
        turn = self.context.get(stream)
        if kind == "user":
            results = [b for b in blocks if b.get("type") == "tool_result"]
            # Claude meta user records and tool results do not initiate user turns.
            if not results and p.get("isMeta") is not True:
                prompt = text(p.get("uuid"))
                turn = (
                    prompt
                    or hashlib.sha256(
                        f"{sid}:{source.path}:{source.generation}:{source.byte_offset}".encode()
                    ).hexdigest()
                )
                self.context[stream] = turn
                yield Turn(
                    agent_type="claude",
                    session_id=sid,
                    turn_id=turn,
                    prompt_id=prompt,
                    started_at_us=occurred,
                    source=source,
                )
            for block in results:
                tool_id = text(block.get("tool_use_id"))
                if tool_id:
                    error = block.get("is_error")
                    yield ToolCall(
                        agent_type="claude",
                        session_id=sid,
                        tool_call_id=tool_id,
                        name="unknown",
                        source=source,
                        turn_id=turn,
                        success=not error if type(error) is bool else None,
                    )
            return
        if kind != "assistant":
            return
        usage = msg.get("usage")
        if usage is None:  # structural records are allowed, not assumed zero-cost
            return
        if not isinstance(usage, dict) or occurred is None or not text(msg.get("model")):
            raise ValueError("invalid_request")
        for field in ("speed", "service_tier", "inference_geo"):
            if usage.get(field) is not None and text(usage[field]) is None:
                raise ValueError("invalid_pricing_modifier")
        if usage.get("server_tool_use") is not None and not isinstance(
            usage["server_tool_use"], dict
        ):
            raise ValueError("invalid_server_tool_usage")
        request_id, message_id = text(p.get("requestId")), text(msg.get("id"))
        if (p.get("requestId") is not None and request_id is None) or (
            msg.get("id") is not None and message_id is None
        ):
            raise ValueError("invalid_request_identity")
        fork = mapping(p.get("forkedFrom"))
        parent = text(fork.get("sessionId")) if text(fork.get("messageUuid")) else None
        if parent == sid:
            raise ValueError("self_replay")
        # Record-specific copy metadata plus stable identity, not mere session ancestry.
        if parent and not (request_id or message_id):
            raise ValueError("unidentifiable_replay")
        yield Request(
            agent_type="claude",
            session_id=sid,
            occurred_at_us=occurred,
            model_raw=msg["model"],
            tokens=tokens(usage),
            source=source,
            request_id=request_id,
            message_id=message_id,
            turn_id=turn,
            query_source=QuerySource.SUBAGENT if subagent else QuerySource.MAIN,
            agent_id=agent_id,
            speed=text(usage.get("speed")),
            service_tier=text(usage.get("service_tier")),
            inference_geo=text(usage.get("inference_geo")),
            is_final=True if text(msg.get("stop_reason")) else None,
            replay_of_session_id=parent,
        )
        for block in blocks:
            if block.get("type") == "tool_use":
                tool_id, name = text(block.get("id")), text(block.get("name"))
                if tool_id and name:
                    yield ToolCall(
                        agent_type="claude",
                        session_id=sid,
                        tool_call_id=tool_id,
                        name=name,
                        source=source,
                        turn_id=turn,
                        request_id=request_id,
                    )
        for unit_type, quantity in mapping(usage.get("server_tool_use")).items():
            counter(quantity, "server_tool_quantity")
            if quantity is None:
                raise ValueError("invalid_server_tool_quantity")
            yield BillableUnit(
                agent_type="claude",
                session_id=sid,
                unit_type=unit_type,
                quantity=quantity,
                request_id=request_id,
                source=source,
            )
