"""CLI parsing remains side-effect free and compatible across the module split."""

from datetime import date
from pathlib import Path

import pytest

from claude_metrics.commands.parser import build_parser


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["init"], {"command": "init", "json": False}),
        (["doctor", "--json"], {"command": "doctor", "json": True}),
        (["scan"], {"command": "scan", "full": False}),
        (["scan", "--full"], {"command": "scan", "full": True}),
        (["serve"], {"command": "serve", "port": 8765, "no_scan": False, "cost_mode": "auto"}),
        (["serve", "--no-scan", "--cost-mode", "strict"], {"no_scan": True, "cost_mode": "strict"}),
        (["pricing", "info"], {"command": "pricing", "pricing_command": "info"}),
        (["pricing", "apply"], {"cost_mode": "auto", "reprice": False}),
        (["pricing", "verify"], {"pricing_command": "verify"}),
        (["report", "overview"], {"command": "report", "report_command": "overview"}),
        (["report", "sessions"], {"limit": 50, "after": 0}),
        (["report", "session", "--session-pk", "3"], {"session_pk": 3, "limit": 50}),
        (["report", "health"], {"report_command": "health"}),
        (["backup", "--output", "saved"], {"command": "backup", "output": Path("saved")}),
        (["support-bundle", "--output", "support"], {"output": Path("support")}),
    ],
)
def test_command_defaults_and_flags(argv, expected, tmp_path):
    args = vars(build_parser().parse_args(argv))
    assert {key: args[key] for key in expected} == expected
    assert not (tmp_path / "app").exists()


def test_typed_dates_paths_and_explicit_estimate_flags():
    args = build_parser().parse_args(
        [
            "pricing",
            "apply",
            "--from",
            "2026-09-01",
            "--to",
            "2026-09-30",
            "--source-dir",
            "first",
            "--source-dir",
            "second",
            "--cost-mode",
            "strict",
            "--assume-provider",
            "anthropic",
            "--assume-cache-5m",
            "--reprice",
        ]
    )
    assert (args.start, args.end) == (date(2026, 9, 1), date(2026, 9, 30))
    assert args.source_dir == [Path("first"), Path("second")]
    assert args.cost_mode == "strict" and args.assume_provider == "anthropic"
    assert args.assume_cache_5m and args.reprice


@pytest.mark.parametrize("argv", [[], ["pricing"], ["report"], ["report", "session"], ["backup"]])
def test_incomplete_commands_keep_argparse_exit_code(argv):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2
