"""Validate independent hand-written expectations, not an unbuilt Claude parser.

Phase 2 must pass real adapter/scanner output to these same expectation records.
Phase 1 checks their arithmetic, source evidence, privacy, and mutation scenarios.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from claude_metrics.domain import TokenUsage
from claude_metrics.units import usd_to_nanos

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures/claude"
CASES = json.loads((HERE / "claude-cases.json").read_text("utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_golden_counter_conservation_and_source_references(case):
    expected = case["expected"]
    assert TokenUsage(**expected["tokens"]).all_tokens == expected["all_tokens"]
    winners = []
    for filename, lines in case["winner_lines"].items():
        assert filename in case["files"]
        records = [
            json.loads(line) for line in (FIXTURES / filename).read_text("utf-8").splitlines()
        ]
        winners.extend(records[number - 1] for number in lines)
    assert len(winners) == expected["requests"]
    # This comparison uses Claude's combined write field directly, independently
    # of the future adapter's split/legacy normalization implementation.
    source_total = sum(
        sum(
            record["message"]["usage"][field]
            for field in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            )
        )
        for record in winners
    )
    assert source_total == expected["all_tokens"]
    if "subagent_tokens" in expected:
        assert expected["main_tokens"] + expected["subagent_tokens"] == expected["all_tokens"]


def _check_content_free(value):
    if isinstance(value, dict):
        assert (
            not {"text", "thinking", "input", "arguments", "toolUseResult", "output"} & value.keys()
        )
        if "content" in value:
            assert value["content"] == []
        for child in value.values():
            _check_content_free(child)
    elif isinstance(value, list):
        for child in value:
            _check_content_free(child)


def test_synthetic_fixture_privacy():
    for path in FIXTURES.rglob("*.jsonl"):
        if path.name == "malformed.jsonl":
            continue
        for line in path.read_text("utf-8").splitlines():
            _check_content_free(json.loads(line))
    scenario = json.loads((FIXTURES / "file-mutations.json").read_text("utf-8"))
    _check_content_free(json.loads(scenario["initial"]))
    # Reassemble fragments so the privacy check covers the previously partial record.
    joined = "".join(step["text"] for step in scenario["steps"] if step["operation"] == "append")
    for line in joined.splitlines():
        _check_content_free(json.loads(line))


def test_fixture_includes_invalid_and_unfinished_lines():
    lines = (FIXTURES / "malformed.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["type"] == "assistant"
    for line in lines[1:]:
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)


def test_file_mutation_recipe_reproduces_byte_boundaries(tmp_path):
    recipe = json.loads((FIXTURES / "file-mutations.json").read_text("utf-8"))
    path = tmp_path / "session.jsonl"
    path.write_bytes(recipe["initial"].encode("utf-8"))
    for index, step in enumerate(recipe["steps"]):
        if step["operation"] == "append":
            with path.open("ab") as stream:
                stream.write(step["text"].encode("utf-8"))
        elif step["operation"] == "truncate":
            path.write_bytes(b"")
        else:
            replacement = tmp_path / "replacement.jsonl"
            replacement.write_bytes(step["text"].encode("utf-8"))
            replacement.replace(path)
        if index == 1:
            assert not path.read_bytes().endswith(b"\n")
            with pytest.raises(json.JSONDecodeError):
                json.loads(path.read_bytes().splitlines()[-1])
        if index == 2:
            assert len(path.read_bytes().splitlines()) == 3
            assert json.loads(path.read_bytes().splitlines()[-1])["requestId"] == "req-sub"
    assert json.loads(path.read_bytes())["requestId"] == "req-a"


def test_local_midnight_expected_days():
    rows = [
        json.loads(line) for line in (FIXTURES / "midnight.jsonl").read_text("utf-8").splitlines()
    ]
    days = [
        datetime.fromisoformat(row["timestamp"]).astimezone(ZoneInfo("Asia/Kolkata")).date()
        for row in rows
    ]
    assert [str(day) for day in days] == ["2026-09-30", "2026-10-01"]


def test_illustrative_cost_expectations_are_hand_reproducible():
    data = json.loads((HERE / "cost-examples.json").read_text("utf-8"))
    rates = data["rates_usd_per_million"]
    mapping = {
        "input_uncached_tokens": "input",
        "cache_read_tokens": "cache_read",
        "cache_write_5m_tokens": "cache_write_5m",
        "cache_write_1h_tokens": "cache_write_1h",
        "output_tokens": "output",
    }
    for case in data["cases"]:
        usd = (
            sum(
                Decimal(case["tokens"][field]) * Decimal(rates[rate])
                for field, rate in mapping.items()
            )
            / 1_000_000
        )
        assert usd_to_nanos(usd) == case["expected_cost_nanos"]
    search = data["web_search_example"]
    assert usd_to_nanos(Decimal(search["usd_per_1000"]) * search["quantity"] / 1000) == 20_000_000
