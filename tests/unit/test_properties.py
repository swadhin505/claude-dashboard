"""Deterministic property-style fuzzing without another development dependency."""

import copy
import json
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from claude_metrics.config import Settings
from claude_metrics.domain import SQLITE_MAX_INT, counter
from claude_metrics.ingestion.scanner import scan
from claude_metrics.reports import ReportFilter, overview
from claude_metrics.storage import connect
from claude_metrics.units import date_bounds, utc_microseconds

FIXTURE = Path(__file__).parents[1] / "fixtures/claude/normal.jsonl"


def test_random_duplicate_order_conserves_greatest_usage(tmp_path):
    randomizer = random.Random(73219)
    original = json.loads(FIXTURE.read_text().splitlines()[1])
    for case in range(24):
        root = tmp_path / str(case) / "unicode-α # &"
        root.mkdir(parents=True)
        values = [randomizer.randrange(1, 10000) for _ in range(8)]
        randomizer.shuffle(values)
        records = []
        for value in values:
            row = copy.deepcopy(original)
            row["message"]["usage"]["output_tokens"] = value
            records.append(json.dumps(row))
        (root / "history.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
        settings = Settings(root.parent / "app", (root,), "UTC")
        scan(settings)
        scan(settings)
        with connect(settings.database, readonly=True) as conn:
            total = overview(conn, ReportFilter("UTC"))["total"]
        assert total["requests"] == 1
        assert total["tokens"]["output_tokens"] == max(values)


@pytest.mark.parametrize(
    "value", [-1, SQLITE_MAX_INT + 1, True, False, 1.0, float("nan"), "5", [], {}]
)
def test_bad_counters_never_coerce_to_money(value):
    with pytest.raises(ValueError):
        counter(value, "tokens")


def test_fuzz_malformed_records_do_not_block_valid_usage(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    original = json.loads(FIXTURE.read_text().splitlines()[1])
    malformed = []
    for index, value in enumerate([-1, 2**63, True, 1.2, "SECRET_VALUE", [], {}, None]):
        row = copy.deepcopy(original)
        row["message"]["id"] = f"bad-{index}"
        row["message"]["usage"]["input_tokens"] = value
        malformed.append(json.dumps(row))
    # Null is a supported unknown counter, not a malformed numeric value.
    text = "\n".join(malformed) + '\n{"broken":\n[]\n{"duplicate":1,"duplicate":2}\n'
    text += json.dumps(original) + "\n"
    (root / "invalid.jsonl").write_text(text, encoding="utf-8")
    settings = Settings(tmp_path / "app", (root,), "UTC")
    result = scan(settings)
    assert result["malformed_rows"] >= 10
    with connect(settings.database, readonly=True) as conn:
        assert overview(conn, ReportFilter("UTC"))["total"]["requests"] >= 1
        assert "SECRET_VALUE" not in "\n".join(conn.iterdump())


def test_random_timezone_bounds_contain_local_noon():
    randomizer = random.Random(94)
    zones = ["UTC", "Asia/Kolkata", "America/New_York", "Australia/Lord_Howe", "Europe/London"]
    for _ in range(500):
        day = date(2024, 1, 1) + timedelta(days=randomizer.randrange(1500))
        zone = randomizer.choice(zones)
        start, end = date_bounds(day, day, zone)
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(zone))
        assert start <= utc_microseconds(noon) < end
        assert utc_microseconds(noon) == utc_microseconds(noon.astimezone(UTC))
        assert end == date_bounds(day + timedelta(days=1), day + timedelta(days=1), zone)[0]
