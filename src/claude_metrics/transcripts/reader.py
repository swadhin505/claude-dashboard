"""Bounded, read-only access to ledger-linked JSONL under configured source roots."""

import json
import os
import stat
from pathlib import Path

from claude_metrics.transcripts.models import Transcript
from claude_metrics.transcripts.parser import parse

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 20_000


class SourceUnavailable(Exception):
    """A safe user-facing reason, without filesystem exceptions or content."""


def _allowed_path(path: Path, roots: tuple[Path, ...]) -> Path:
    if not path.is_absolute() or path.suffix.lower() != ".jsonl":
        raise SourceUnavailable("This source is not a supported local JSONL file.")
    resolved = path.resolve(strict=True)
    if resolved != path.absolute() or path.is_symlink() or path.is_junction():
        raise SourceUnavailable("Linked or redirected transcript paths are not opened.")
    for root in roots:
        if resolved.is_relative_to(root.resolve()):
            return resolved
    raise SourceUnavailable("This source is outside the currently configured Claude folders.")


def read(path: Path, roots: tuple[Path, ...], session_id: str) -> Transcript:
    result = Transcript()
    try:
        allowed = _allowed_path(path, roots)
        before = allowed.stat()
        if not stat.S_ISREG(before.st_mode):
            raise SourceUnavailable("This source is not a regular file.")
        with allowed.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise SourceUnavailable("The source changed while opening. Please reload.")
            # Check again after opening, before reading any content.
            _allowed_path(path, roots)
            size = min(opened.st_size, MAX_FILE_BYTES)
            if opened.st_size > MAX_FILE_BYTES:
                result.warn("Source exceeds 32 MiB. Only its bounded beginning is shown.")
            parse(_records(source, size, result), session_id, result)
            after = os.fstat(source.fileno())
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                result.warn("The log changed during this read. Reload to see newer content.")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceUnavailable(
            "The original transcript is missing or unreadable. Retained usage is still available."
        ) from exc
    return result


def _records(source, size: int, result: Transcript):
    count = 0
    while source.tell() < size:
        if count == MAX_RECORDS:
            result.warn("Record limit reached (20,000). Only the beginning of this log is shown.")
            return
        count += 1
        raw = source.readline(min(MAX_LINE_BYTES + 1, size - source.tell()))
        if len(raw) > MAX_LINE_BYTES:
            result.warn("An oversized record was skipped (over 2 MiB).")
            while raw and not raw.endswith(b"\n") and source.tell() < size:
                raw = source.readline(min(MAX_LINE_BYTES, size - source.tell()))
            if not raw:
                result.warn("The source was truncated during this read. Please reload.")
                return
            continue
        if not raw.endswith(b"\n"):
            result.warn(
                "An incomplete trailing record was omitted (active write or read limit). "
                "Reload after the writer finishes."
            )
            return
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            result.warn("Malformed JSON records were skipped.")
            continue
        if not isinstance(entry, dict):
            result.warn("Non-object JSON records were skipped.")
            continue
        yield count, entry
