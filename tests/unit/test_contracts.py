from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from claude_metrics.domain import TokenUsage
from claude_metrics.units import date_bounds, usd_to_nanos, utc_microseconds


def test_exclusive_tokens_and_reasoning_subset():
    usage = TokenUsage(
        input_uncached_tokens=100,
        cache_read_tokens=200,
        cache_write_5m_tokens=40,
        cache_write_1h_tokens=60,
        cache_write_unknown_tokens=0,
        output_tokens=50,
        reasoning_tokens=20,
    )
    assert usage.input_total == 400
    assert usage.all_tokens == 450
    with pytest.raises(FrozenInstanceError):
        usage.output_tokens = 60


def test_unknown_is_not_zero_and_unknown_ttl_is_not_lost():
    assert TokenUsage(input_uncached_tokens=10).all_tokens is None
    legacy = TokenUsage(
        input_uncached_tokens=3,
        cache_read_tokens=0,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
        cache_write_unknown_tokens=8,
        output_tokens=4,
    )
    assert legacy.all_tokens == 15


@pytest.mark.parametrize("bad", [-1, 1.5, True, "10", 2**63])
def test_invalid_counters_rejected(bad):
    with pytest.raises(ValueError):
        TokenUsage(output_tokens=bad)


def test_reasoning_cannot_exceed_output():
    with pytest.raises(ValueError, match="reasoning"):
        TokenUsage(output_tokens=2, reasoning_tokens=3)


@pytest.mark.parametrize(
    "value,expected",
    [("0.000000001", 1), ("1.23456789", 1234567890), (Decimal("0.01"), 10000000), (0, 0)],
)
def test_exact_currency(value, expected):
    assert usd_to_nanos(value) == expected


@pytest.mark.parametrize(
    "value", [0.1, True, "-1", "NaN", "Infinity", "0.0000000001", "10000000000", "not-money"]
)
def test_money_rejects_float_nonfinite_overflow_and_silent_rounding(value):
    with pytest.raises(ValueError):
        usd_to_nanos(value)


def test_timestamps_do_not_use_float_or_naive_local_time():
    assert utc_microseconds(datetime(2026, 1, 1, 0, 0, 0, 123456, UTC)) == 1767225600123456
    with pytest.raises(ValueError, match="timezone"):
        utc_microseconds(datetime(2026, 1, 1))


def test_kolkata_midnight_half_open_interval():
    start, end = date_bounds(date(2026, 10, 1), date(2026, 10, 1), "Asia/Kolkata")
    assert start == utc_microseconds(datetime(2026, 9, 30, 18, 30, tzinfo=UTC))
    assert end == utc_microseconds(datetime(2026, 10, 1, 18, 30, tzinfo=UTC))
    assert not start <= start - 1 < end
    assert start <= start < end
    assert not start <= end < end


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_dst_day_length_with_packaged_tzdata(day, hours):
    # Force loading from the dependency, including on systems with a system IANA database.
    from importlib.resources import files
    from zoneinfo import ZoneInfo

    with files("tzdata").joinpath("zoneinfo/America/New_York").open("rb") as stream:
        zone = ZoneInfo.from_file(stream)
        assert datetime(2026, 1, 1, tzinfo=zone).utcoffset().total_seconds() == -18000
    start, end = date_bounds(day, day, "America/New_York")
    assert end - start == hours * 3600 * 1_000_000


def test_reversed_range_rejected():
    with pytest.raises(ValueError):
        date_bounds(date(2026, 10, 2), date(2026, 10, 1), "UTC")
