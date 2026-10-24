"""Read-only Claude Code session inspector.

Run interactively:
    python scripts/inspect_claude_sessions.py

The inspector reads Claude Code JSONL files directly. It does not import the
dashboard package, create a database, write cache files, start a server, or
modify any transcript.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "claude-session-inspector/1"
TITLE_LIMIT = 96


@dataclass(frozen=True)
class SessionSource:
    """One discovered Claude Code transcript and the project directory containing it."""

    project: str
    path: Path


def main() -> int:
    args = parse_args()
    if args.file is not None:
        report = inspect_session(
            args.file.resolve(strict=True), args.file.parent.name, args.summary
        )
        print_json(report)
        return 0

    projects_dir = args.projects_dir or default_projects_dir()
    sources = discover_sessions(projects_dir)
    if not sources:
        print(f"No Claude Code JSONL sessions found below: {projects_dir}", file=sys.stderr)
        return 1

    return run_interactive(sources, args.summary)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects-dir",
        type=Path,
        help=(
            "Claude projects directory "
            "(default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)"
        ),
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Inspect one JSONL transcript directly instead of opening the interactive picker",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print session metadata, counts, usage, and tool metadata without transcript bodies",
    )
    return parser.parse_args()


def default_projects_dir() -> Path:
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return config_dir / "projects"


def discover_sessions(projects_dir: Path) -> list[SessionSource]:
    """Return regular JSONL transcript files, grouped by their first project directory."""

    if not projects_dir.is_dir():
        return []

    sources: list[SessionSource] = []
    for path in projects_dir.rglob("*.jsonl"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.relative_to(projects_dir)
        except ValueError:
            continue
        project = relative.parts[0] if len(relative.parts) > 1 else "(root)"
        sources.append(SessionSource(project=project, path=path))
    return sorted(
        sources,
        key=lambda source: (source.project.casefold(), source.path.name.casefold()),
    )


def run_interactive(sources: list[SessionSource], summary_only: bool) -> int:
    by_project: dict[str, list[SessionSource]] = {}
    for source in sources:
        by_project.setdefault(source.project, []).append(source)
    projects = sorted(by_project, key=str.casefold)

    while True:
        print("\nClaude Code projects")
        for number, project in enumerate(projects, start=1):
            print(f"  {number:>3}. {project} ({len(by_project[project])} sessions)")
        print("    a. All projects")
        print("    q. Quit")

        choice = input("Choose a project: ").strip().casefold()
        if choice in {"q", "quit"}:
            return 0
        if choice in {"a", "all"}:
            selected = sources
        elif choice.isdigit() and 1 <= int(choice) <= len(projects):
            selected = by_project[projects[int(choice) - 1]]
        else:
            print("Please enter a listed number, a, or q.")
            continue

        choose_session(selected, summary_only)


def choose_session(sources: list[SessionSource], summary_only: bool) -> None:
    previews = [session_preview(source) for source in sources]
    while True:
        print("\nClaude Code sessions")
        for number, preview in enumerate(previews, start=1):
            session = preview["session"]
            counts = preview["counts"]
            usage = preview["usage"]
            title = session["title"] or "(untitled)"
            ended = session["ended_at"] or "unknown time"
            print(
                f"  {number:>3}. {title} | {ended} | "
                f"prompts {counts['user_messages']}, tools {counts['tool_calls']}, "
                f"out {usage['output_tokens']:,}"
            )
        print("    b. Back")
        print("    q. Quit")

        choice = input("Choose a session: ").strip().casefold()
        if choice in {"b", "back"}:
            return
        if choice in {"q", "quit"}:
            raise SystemExit(0)
        if not (choice.isdigit() and 1 <= int(choice) <= len(sources)):
            print("Please enter a listed number, b, or q.")
            continue

        source = sources[int(choice) - 1]
        report = inspect_session(source.path, source.project, summary_only)
        print()
        print_json(report)
        input("\nPress Enter to return to the session list.")


def session_preview(source: SessionSource) -> dict[str, Any]:
    return inspect_session(source.path, source.project, summary_only=True)


def inspect_session(path: Path, project: str, summary_only: bool = False) -> dict[str, Any]:
    """Parse a single Claude Code JSONL transcript into a portable JSON report.

    This is deliberately a transparent file inspector, not an AgentsView clone:
    it keeps messages in file order and reports DAG branching rather than
    splitting a fork into virtual sessions or persisting any derived state.
    """

    raw_entries, malformed_lines = read_jsonl(path)
    state = _ParseState(path=path, project=project, summary_only=summary_only)

    for line_number, entry in raw_entries:
        state.consume(entry, line_number)
    state.finish()
    return state.report(malformed_lines)


def read_jsonl(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    entries: list[tuple[int, dict[str, Any]]] = []
    malformed_lines = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed_lines += 1
                continue
            if isinstance(value, dict):
                entries.append((line_number, value))
            else:
                malformed_lines += 1
    return entries, malformed_lines


class _ParseState:
    def __init__(self, path: Path, project: str, summary_only: bool) -> None:
        self.path = path
        self.project = project
        self.summary_only = summary_only
        self.messages: list[dict[str, Any]] = []
        self.assistant_positions: dict[str, int] = {}
        self.tool_calls_by_id: dict[str, dict[str, Any]] = {}
        self.unmatched_tool_results: list[dict[str, Any]] = []
        self.timestamps: list[tuple[datetime, str]] = []
        self.session_ids: set[str] = set()
        self.top_level_metadata: dict[str, str] = {}
        self.custom_title = ""
        self.ai_title = ""
        self.first_user_text = ""
        self.forked_from: dict[str, str] | None = None
        self.uuid_to_parent: dict[str, str] = {}
        self.duplicate_assistant_messages = 0
        self.ignored_meta_entries = 0

    def consume(self, entry: dict[str, Any], line_number: int) -> None:
        self._capture_metadata(entry)
        kind = string(entry.get("type"))
        if kind == "custom-title":
            self.custom_title = string(entry.get("customTitle")) or self.custom_title
            return
        if kind == "ai-title":
            self.ai_title = string(entry.get("aiTitle")) or self.ai_title
            return
        if kind == "system":
            self._capture_legacy_rename(entry)
            return
        if kind not in {"user", "assistant"}:
            return

        if kind == "user" and entry.get("isMeta") is True:
            self.ignored_meta_entries += 1
            self._capture_tool_results(entry, line_number)
            return

        message = mapping(entry.get("message"))
        blocks = content_blocks(message.get("content"))
        text, thinking, tool_calls, tool_results = parse_blocks(blocks)
        self._capture_tool_results_from_values(tool_results, entry, line_number)

        if kind == "user" and not self.first_user_text and text:
            self.first_user_text = text
        if kind == "user" and not text and not thinking:
            return

        normalized = self._normalized_message(
            entry=entry,
            line_number=line_number,
            role=kind,
            text=text,
            thinking=thinking,
            tool_calls=tool_calls,
        )
        if kind == "assistant":
            message_id = normalized["claude_message_id"]
            if message_id:
                prior = self.assistant_positions.get(message_id)
                if prior is not None:
                    self.messages[prior] = merge_assistant_messages(
                        self.messages[prior], normalized
                    )
                    self.duplicate_assistant_messages += 1
                    self._register_tool_calls(self.messages[prior])
                    return
                self.assistant_positions[message_id] = len(self.messages)
        self.messages.append(normalized)
        self._register_tool_calls(normalized)

    def _capture_metadata(self, entry: Mapping[str, Any]) -> None:
        session_id = string(entry.get("sessionId"))
        if session_id:
            self.session_ids.add(session_id)
        timestamp = string(entry.get("timestamp"))
        parsed_timestamp = parse_timestamp(timestamp)
        if parsed_timestamp is not None and timestamp:
            self.timestamps.append((parsed_timestamp, timestamp))
        for key, output_key in (
            ("cwd", "cwd"),
            ("gitBranch", "git_branch"),
            ("version", "claude_code_version"),
            ("entrypoint", "entrypoint"),
            ("sessionKind", "session_kind"),
            ("agentSetting", "agent_setting"),
        ):
            value = string(entry.get(key))
            if value and output_key not in self.top_level_metadata:
                self.top_level_metadata[output_key] = value
        fork = mapping(entry.get("forkedFrom"))
        parent_id = string(fork.get("sessionId"))
        message_uuid = string(fork.get("messageUuid"))
        if parent_id and message_uuid:
            self.forked_from = {"session_id": parent_id, "message_uuid": message_uuid}
        uuid = string(entry.get("uuid"))
        parent_uuid = string(entry.get("parentUuid"))
        if uuid:
            self.uuid_to_parent[uuid] = parent_uuid

    def _capture_legacy_rename(self, entry: Mapping[str, Any]) -> None:
        content = string(entry.get("content"))
        match = re.search(r"(?:^|\s)/rename\s+(.+)$", content)
        if match:
            self.custom_title = match.group(1).strip()

    def _capture_tool_results(self, entry: Mapping[str, Any], line_number: int) -> None:
        message = mapping(entry.get("message"))
        _, _, _, tool_results = parse_blocks(content_blocks(message.get("content")))
        self._capture_tool_results_from_values(tool_results, entry, line_number)

    def _capture_tool_results_from_values(
        self,
        tool_results: Iterable[dict[str, Any]],
        entry: Mapping[str, Any],
        line_number: int,
    ) -> None:
        for result in tool_results:
            result["timestamp"] = string(entry.get("timestamp"))
            result["source_line"] = line_number
            tool_id = result.get("tool_use_id")
            call = self.tool_calls_by_id.get(tool_id) if isinstance(tool_id, str) else None
            if call is None:
                self.unmatched_tool_results.append(result)
                continue
            call["results"].append(result)

    def _normalized_message(
        self,
        *,
        entry: Mapping[str, Any],
        line_number: int,
        role: str,
        text: str,
        thinking: str,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        message = mapping(entry.get("message"))
        usage = normalized_usage(message.get("usage"))
        model = string(message.get("model"))
        effort = string(entry.get("effort"))
        return {
            "role": role,
            "timestamp": string(entry.get("timestamp")),
            "source_line": line_number,
            "uuid": string(entry.get("uuid")),
            "parent_uuid": string(entry.get("parentUuid")),
            "prompt_source": string(entry.get("promptSource")),
            "is_sidechain": entry.get("isSidechain") is True,
            "content": text,
            "thinking": thinking,
            "tool_calls": tool_calls,
            "model": model,
            "reasoning_effort": effort,
            "claude_message_id": string(message.get("id")),
            "claude_request_id": string(entry.get("requestId")),
            "stop_reason": string(message.get("stop_reason")),
            "usage": usage,
        }

    def _register_tool_calls(self, message: Mapping[str, Any]) -> None:
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            return
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool_id = call.get("id")
            if isinstance(tool_id, str) and tool_id:
                existing = self.tool_calls_by_id.get(tool_id)
                if existing is not None and existing is not call:
                    call["results"] = existing.get("results", [])
                self.tool_calls_by_id[tool_id] = call

    def finish(self) -> None:
        if self.unmatched_tool_results:
            remaining: list[dict[str, Any]] = []
            for result in self.unmatched_tool_results:
                tool_id = result.get("tool_use_id")
                call = self.tool_calls_by_id.get(tool_id) if isinstance(tool_id, str) else None
                if call is None:
                    remaining.append(result)
                else:
                    call["results"].append(result)
            self.unmatched_tool_results = remaining

    def report(self, malformed_lines: int) -> dict[str, Any]:
        usage = usage_summary(self.messages)
        tool_calls = [call for message in self.messages for call in message["tool_calls"]]
        models, reasoning_efforts, server_tool_use = session_metadata_counts(self.messages)
        user_messages = sum(message["role"] == "user" for message in self.messages)
        assistant_messages = sum(message["role"] == "assistant" for message in self.messages)
        thinking_messages = sum(bool(message["thinking"]) for message in self.messages)
        session_id = next(iter(self.session_ids), self.path.stem)
        title = first_non_empty(
            self.custom_title, self.ai_title, title_preview(self.first_user_text)
        )
        metadata = {
            "id": session_id,
            "title": title,
            "project_directory": self.project,
            "source_file": str(self.path),
            "started_at": self._timestamp_bound(min),
            "ended_at": self._timestamp_bound(max),
            "models": dict(sorted(models.items())),
            "reasoning_efforts": dict(sorted(reasoning_efforts.items())),
            "forked_from": self.forked_from,
            **self.top_level_metadata,
        }
        counts = {
            "messages": len(self.messages),
            "user_messages": user_messages,
            "assistant_messages": assistant_messages,
            "thinking_messages": thinking_messages,
            "tool_calls": len(tool_calls),
            "tool_results": sum(len(call["results"]) for call in tool_calls),
            "malformed_lines": malformed_lines,
            "assistant_stream_snapshots_merged": self.duplicate_assistant_messages,
            "meta_entries_ignored": self.ignored_meta_entries,
            "unmatched_tool_results": len(self.unmatched_tool_results),
        }
        parser_notes = {
            "read_only": True,
            "branching_detected": has_branching(self.uuid_to_parent),
            "raw_session_ids_seen": sorted(self.session_ids),
            "source_order_preserved": True,
            "note": (
                "This inspector reports one JSONL file in source order. "
                "Unlike AgentsView, it does not split Claude DAG forks into "
                "separate virtual sessions."
            ),
        }
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "session": metadata,
            "counts": counts,
            "usage": usage,
            "server_tool_use": dict(sorted(server_tool_use.items())),
            "parser_notes": parser_notes,
            "tool_calls": tool_call_summaries(tool_calls) if self.summary_only else tool_calls,
        }
        if self.unmatched_tool_results:
            report["unmatched_tool_results"] = self.unmatched_tool_results
        if not self.summary_only:
            report["messages"] = self.messages
        return report

    def _timestamp_bound(self, selector: Any) -> str | None:
        if not self.timestamps:
            return None
        return selector(self.timestamps, key=lambda item: item[0])[1]


def content_blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def parse_blocks(
    blocks: Iterable[Mapping[str, Any]],
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in blocks:
        kind = string(block.get("type"))
        if kind == "text":
            if value := string(block.get("text")):
                text_parts.append(value)
        elif kind in {"thinking", "redacted_thinking"}:
            if value := string(block.get("thinking")) or string(block.get("text")):
                thinking_parts.append(value)
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": string(block.get("id")),
                    "name": string(block.get("name")) or "unknown",
                    "category": tool_category(string(block.get("name"))),
                    "input": block.get("input"),
                    "results": [],
                }
            )
        elif kind == "tool_result":
            tool_results.append(
                {
                    "tool_use_id": string(block.get("tool_use_id")),
                    "is_error": block.get("is_error") is True,
                    "content": block.get("content"),
                }
            )
    return "\n".join(text_parts), "\n".join(thinking_parts), tool_calls, tool_results


def normalized_usage(value: object) -> dict[str, Any] | None:
    usage = mapping(value)
    if not usage:
        return None
    result: dict[str, Any] = {}
    for field in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "reasoning_tokens",
    ):
        count = integer(usage.get(field))
        if count is not None:
            result[field] = count
    server_tool_use = {
        name: count
        for name, raw_count in mapping(usage.get("server_tool_use")).items()
        if isinstance(name, str)
        for count in [integer(raw_count)]
        if count is not None
    }
    if server_tool_use:
        result["server_tool_use"] = server_tool_use
    return result


def merge_assistant_messages(previous: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Keep final request metadata while retaining the fuller visible snapshot."""

    merged = dict(previous)
    for key in (
        "timestamp",
        "parent_uuid",
        "prompt_source",
        "model",
        "reasoning_effort",
        "claude_request_id",
        "stop_reason",
        "usage",
    ):
        value = candidate.get(key)
        if value is not None and value != "" and value != {}:
            merged[key] = candidate[key]
    for key in ("content", "thinking"):
        merged[key] = richer_text(str(previous.get(key, "")), str(candidate.get(key, "")))
    merged["tool_calls"] = merge_tool_calls(
        previous.get("tool_calls", []), candidate.get("tool_calls", [])
    )
    return merged


def merge_tool_calls(previous: object, candidate: object) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    for group in (previous, candidate):
        if not isinstance(group, list):
            continue
        for index, raw_call in enumerate(group):
            if not isinstance(raw_call, dict):
                continue
            raw_id = raw_call.get("id")
            key = raw_id if isinstance(raw_id, str) else f"anonymous-{index}"
            if key not in calls:
                calls[key] = raw_call
                ordered.append(key)
                continue
            existing = calls[key]
            raw_call["results"] = existing.get("results", [])
            calls[key] = raw_call
    return [calls[key] for key in ordered]


def richer_text(previous: str, candidate: str) -> str:
    if candidate.startswith(previous):
        return candidate
    if previous.startswith(candidate):
        return previous
    return candidate if len(candidate) >= len(previous) else previous


def usage_summary(messages: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    totals = Counter[str]()
    peak_context_tokens = 0
    usage_records = 0
    for message in messages:
        usage = mapping(message.get("usage"))
        if not usage:
            continue
        usage_records += 1
        request_context = 0
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = integer(usage.get(key)) or 0
            totals[key] += value
            request_context += value
        totals["output_tokens"] += integer(usage.get("output_tokens")) or 0
        totals["reasoning_tokens"] += integer(usage.get("reasoning_tokens")) or 0
        peak_context_tokens = max(peak_context_tokens, request_context)
    return {
        "input_tokens": totals["input_tokens"],
        "cache_creation_input_tokens": totals["cache_creation_input_tokens"],
        "cache_read_input_tokens": totals["cache_read_input_tokens"],
        "output_tokens": totals["output_tokens"],
        "reasoning_tokens": totals["reasoning_tokens"],
        "peak_context_tokens": peak_context_tokens,
        "assistant_usage_records": usage_records,
    }


def session_metadata_counts(
    messages: Iterable[Mapping[str, Any]],
) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """Count only final, de-duplicated assistant message metadata."""

    models: Counter[str] = Counter()
    reasoning_efforts: Counter[str] = Counter()
    server_tool_use: Counter[str] = Counter()
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if model := string(message.get("model")):
            models[model] += 1
        if effort := string(message.get("reasoning_effort")):
            reasoning_efforts[effort] += 1
        usage = mapping(message.get("usage"))
        for name, count in mapping(usage.get("server_tool_use")).items():
            if isinstance(name, str) and (quantity := integer(count)) is not None:
                server_tool_use[name] += quantity
    return models, reasoning_efforts, server_tool_use


def tool_call_summaries(tool_calls: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return useful tool metadata without exposing large input/result bodies."""

    summaries: list[dict[str, Any]] = []
    for call in tool_calls:
        results = call.get("results", [])
        if not isinstance(results, list):
            results = []
        summaries.append(
            {
                "id": string(call.get("id")),
                "name": string(call.get("name")),
                "category": string(call.get("category")),
                "result_count": len(results),
                "has_error": any(
                    isinstance(result, Mapping) and result.get("is_error") is True
                    for result in results
                ),
            }
        )
    return summaries


def tool_category(name: str | None) -> str:
    if not name:
        return "Other"
    normalized = name.casefold()
    if normalized in {"read", "glob", "grep"}:
        return "Read"
    if normalized in {"edit", "write", "notebookedit", "apply_patch"}:
        return "Write"
    if normalized in {"bash", "bashoutput", "computer"}:
        return "Execution"
    if normalized in {"task", "agent"} or "subagent" in normalized:
        return "Task"
    if normalized in {"skill", "toolsearch"}:
        return "Skill"
    return name


def has_branching(uuid_to_parent: Mapping[str, str]) -> bool:
    children = Counter(parent for parent in uuid_to_parent.values() if parent)
    return any(count > 1 for count in children.values())


def title_preview(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= TITLE_LIMIT:
        return compact
    return compact[: TITLE_LIMIT - 1].rstrip() + "…"


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def first_non_empty(*values: str) -> str:
    return next((value for value in values if value), "")


def print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
