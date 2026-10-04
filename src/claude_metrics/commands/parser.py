"""CLI argument definitions; no filesystem or application execution."""

import argparse
from datetime import date
from pathlib import Path

from claude_metrics import __version__
from claude_metrics.config import DEFAULT_CONFIG_FILENAME
from claude_metrics.pricing import policy


def _options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        help=f"TOML settings file (default: ./{DEFAULT_CONFIG_FILENAME} if present)",
    )
    parser.add_argument("--data-dir", type=Path, help="application data directory")
    parser.add_argument(
        "--source-dir", type=Path, action="append", help="Claude projects root (repeatable)"
    )
    parser.add_argument("--timezone", help="IANA timezone, e.g. Asia/Kolkata")
    parser.add_argument("--json", action="store_true", help="machine-readable diagnostics")


def _dates(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from", dest="start", type=date.fromisoformat, help="first local date YYYY-MM-DD"
    )
    parser.add_argument(
        "--to", dest="end", type=date.fromisoformat, help="last local date YYYY-MM-DD"
    )


def _pricing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cost-mode",
        choices=["auto", "strict"],
        default=policy.DEFAULT_COST_MODE,
        help="auto: labelled Anthropic/5m-cache estimates; strict: no such assumptions "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--assume-provider",
        choices=["anthropic"],
        help="enable Anthropic API-equivalent estimates even in strict mode",
    )
    parser.add_argument(
        "--assume-cache-5m",
        action="store_true",
        help="estimate unknown-TTL writes at the 5-minute rate even in strict mode",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-metrics", description="Claude Dashboard — local usage ledger"
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    _options(commands.add_parser("init", help="create/migrate the application's SQLite ledger"))
    _options(commands.add_parser("doctor", help="inspect setup without ingesting transcripts"))
    for name in ("backup", "support-bundle"):
        maintenance = commands.add_parser(name, help="export to a new directory; never overwrite")
        _options(maintenance)
        maintenance.add_argument("--output", type=Path, required=True)
    scanning = commands.add_parser("scan", help="ingest local Claude JSONL files offline")
    _options(scanning)
    scanning.add_argument("--full", action="store_true", help="reparse all available source files")
    serving = commands.add_parser("serve", help="scan, price and serve the loopback-only dashboard")
    _options(serving)
    serving.add_argument("--port", type=int, default=8765)
    serving.add_argument(
        "--no-scan", action="store_true", help="show retained data without startup scan"
    )
    _pricing_options(serving)
    pricing = commands.add_parser("pricing", help="offline price catalog")
    prices = pricing.add_subparsers(dest="pricing_command", required=True)
    _options(prices.add_parser("info", help="verify and describe the selected offline catalog"))
    update = prices.add_parser("update", help="explicit commit/checksum-pinned LiteLLM download")
    _options(update)
    update.add_argument("--commit", required=True)
    update.add_argument("--sha256", required=True)
    apply = prices.add_parser("apply", help="create reproducible receipts for unpriced revisions")
    _options(apply)
    _dates(apply)
    _pricing_options(apply)
    apply.add_argument(
        "--reprice",
        action="store_true",
        help="explicitly select a new receipt policy; retain old receipts",
    )
    apply.add_argument(
        "--discrepancy-tolerance-nanos",
        type=int,
        default=policy.DEFAULT_DISCREPANCY_TOLERANCE_NANOS,
    )
    _options(prices.add_parser("verify", help="read-only frozen receipt and catalog replay audit"))
    report = commands.add_parser("report", help="read-only ledger reports")
    reports = report.add_subparsers(dest="report_command", required=True)
    for name in ("overview", "sessions", "session", "health"):
        command = reports.add_parser(name)
        _options(command)
        if name != "health":
            _dates(command)
            command.add_argument("--project", help="exact normalized project ID")
            command.add_argument("--model", help="exact raw model ID")
        if name in ("sessions", "session"):
            command.add_argument("--limit", type=int, default=50)
            command.add_argument("--after", type=int, default=0)
        if name == "session":
            command.add_argument("--session-pk", type=int, required=True)
    return parser
