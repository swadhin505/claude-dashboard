"""Presentation must never alter accounting values or imply tiny costs are free."""

import pytest

from claude_metrics.web.presentation import bars, money, money_brief


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "Unavailable"),
        (0, "$0.00"),
        (1, "<$0.01"),
        (5_000_000, "<$0.01"),
        (9_999_999, "<$0.01"),
        (10_000_000, "$0.01"),
        (14_999_999, "$0.01"),
        (15_000_000, "$0.02"),
        (25_000_000, "$0.02"),
        (25_000_001, "$0.03"),
        (995_000_000, "$1.00"),
        (5_143_653_450, "$5.14"),
        (9_007_199_254_740_993, "$9,007,199.25"),
        (9_223_372_036_854_775_807, "$9,223,372,036.85"),
    ],
)
def test_brief_money_uses_integer_half_even_rounding(value, expected):
    assert money_brief(value) == expected


def test_exact_value_remains_available_separately():
    assert money_brief(5_143_653_450) == "$5.14"
    assert money(5_143_653_450) == "$5.14365345"
    assert money(1) == "$0.000000001"


def test_activity_geometry_does_not_change_accounting():
    rows = [{"known_tokens": value} for value in (0, 1, 55, 110)]
    original = [row.copy() for row in rows]
    result = bars(rows, "known_tokens")
    assert [bar["height"] for bar in result] == [0, 1, 55, 110]
    assert rows == original
    assert [bar["row"] for bar in result] == rows
    assert bars([], "known_tokens") == []
    assert bars([{"known_tokens": 0}], "known_tokens")[0]["height"] == 0
