"""Formatting only: exact money text and accessible server-generated chart geometry."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from claude_metrics.units import EPOCH

TOKEN_LABELS = {
    "input_uncached_tokens": "Uncached input",
    "cache_read_tokens": "Cache read",
    "cache_write_5m_tokens": "Cache write · 5 min",
    "cache_write_1h_tokens": "Cache write · 1 hour",
    "cache_write_unknown_tokens": "Cache write · unknown TTL",
    "output_tokens": "Output",
}


def money(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    whole, fraction = divmod(value, 1_000_000_000)
    # Preserve every nanodollar, with at least two decimal places.
    tail = f"{fraction:09d}".rstrip("0").ljust(2, "0")
    return f"${whole:,}.{tail}"


def number(value: int | None) -> str:
    return "Unknown" if value is None else f"{value:,}"


def money_brief(value: int | None) -> str:
    """Display cents with integer half-even rounding; never turn positive usage into $0."""
    if value is None:
        return "Unavailable"
    if 0 < value < 10_000_000:
        return "<$0.01"
    cents, remainder = divmod(value, 10_000_000)
    if remainder > 5_000_000 or (remainder == 5_000_000 and cents % 2):
        cents += 1
    return f"${cents // 100:,}.{cents % 100:02d}"


def timestamp(value: int | None, timezone: str) -> str:
    if value is None:
        return "Not recorded"
    return (
        (EPOCH + timedelta(microseconds=value))
        .astimezone(ZoneInfo(timezone))
        .strftime("%d %b %Y, %H:%M:%S %Z")
    )


def bars(rows: list[dict], key: str) -> list[dict]:
    """Integer-scaled display geometry; raw accounting values are never changed."""
    maximum = max((row[key] for row in rows), default=0) or 1
    return [
        {"row": row, "height": row[key] * 110 // maximum, "x": index * 30 + 8}
        for index, row in enumerate(rows)
    ]
