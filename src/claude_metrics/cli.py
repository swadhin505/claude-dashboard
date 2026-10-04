"""Stable CLI entry point, output formatting, and exit-code handling."""

import json
import sqlite3
import sys
from collections.abc import Sequence

from claude_metrics.commands.handlers import doctor, execute
from claude_metrics.commands.parser import build_parser

__all__ = ["doctor", "main"]


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2))
        return
    for key, value in result.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True)
        print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute(args)
        if result is None:
            return 0
        _emit(result, args.json)
        return 0 if result.get("ok", True) else 1
    except (ValueError, OSError, sqlite3.Error) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
