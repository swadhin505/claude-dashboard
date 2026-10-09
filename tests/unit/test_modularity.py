"""Prevent shared domain/calculation contracts from drifting back into outer layers."""

import ast
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_metrics import domain, reports
from claude_metrics.commands.parser import build_parser
from claude_metrics.ingestion import ledger
from claude_metrics.pricing import calculator, policy
from claude_metrics.runtime import default_runtime
from claude_metrics.web.presentation import TOKEN_LABELS


def test_one_exclusive_token_contract_and_legacy_imports():
    assert ledger.TOKEN_COLUMNS is domain.TOKEN_COLUMNS
    assert calculator.TOKEN_COLUMNS is domain.TOKEN_COLUMNS
    assert reports.TOKEN_COLUMNS is domain.TOKEN_COLUMNS
    assert tuple(TOKEN_LABELS) == domain.TOKEN_COLUMNS
    assert tuple(field.name for field in fields(domain.TokenUsage)) == (
        *domain.TOKEN_COLUMNS,
        "reasoning_tokens",
    )
    assert calculator.PricingPolicy is policy.PricingPolicy
    assert calculator.AUTO_POLICY is policy.AUTO_POLICY
    assert calculator.canonical is policy.canonical


@pytest.mark.parametrize("module", [domain, calculator, reports])
def test_core_has_no_outer_layer_imports(module):
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    forbidden = (
        "claude_metrics.ingestion",
        "claude_metrics.web",
        "claude_metrics.commands",
        "claude_metrics.application",
        "claude_metrics.runtime",
        "claude_metrics.storage",
        "claude_metrics.transcripts",
    )
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not any(name.startswith(forbidden) for name in imports)


@pytest.mark.parametrize("command", [["serve"], ["pricing", "apply"]])
def test_shared_cli_policy_defaults(command):
    args = build_parser().parse_args(command)
    assert args.cost_mode == policy.DEFAULT_COST_MODE
    assert policy.policy_for_mode(args.cost_mode) == policy.AUTO_POLICY
    assert policy.PricingPolicy().assume_provider is None  # old frozen policies stay strict


@pytest.mark.parametrize(
    "mode,provider,cache,expected",
    [
        ("auto", None, False, ("anthropic", True)),
        ("strict", None, False, (None, False)),
        ("strict", "anthropic", False, ("anthropic", False)),
        ("strict", None, True, (None, True)),
    ],
)
def test_policy_resolution_is_shared(mode, provider, cache, expected):
    result = policy.policy_for_mode(mode, assume_provider=provider, assume_cache_5m=cache)
    assert (result.assume_provider, result.assume_cache_5m) == expected


def test_unknown_policy_mode_fails_closed():
    with pytest.raises(ValueError, match="cost mode"):
        policy.policy_for_mode("guess")


def test_source_and_engine_must_agree_before_execution():
    current = default_runtime()
    with pytest.raises(ValueError, match="same agent"):
        replace(current, source=replace(current.source, agent_type="another-agent"))
    with pytest.raises(ValueError, match="replay"):
        replace(
            current,
            pricing=SimpleNamespace(
                AGENT_TYPE="claude", FORMULA_VERSION="missing", SUPPORTED_FORMULAS=set()
            ),
        )
