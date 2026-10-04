"""Exact currency and time boundaries shared by adapters and reports."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

from claude_metrics.domain import counter

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def usd_to_nanos(value: str | Decimal | int) -> int:
    if isinstance(value, (float, bool)):
        raise ValueError("money must be decimal text, Decimal, or integer, never float/bool")
    try:
        with localcontext() as context:
            context.prec = 50
            number = Decimal(value)
            scaled = number * 1_000_000_000
            if not scaled.is_finite() or scaled != scaled.to_integral_value():
                raise ValueError("money must be finite and exactly representable in nanodollars")
            result = int(scaled)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("invalid decimal money") from exc
    counter(result, "cost_nanos")
    return result


def utc_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp requires an explicit timezone")
    delta = value.astimezone(UTC) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def date_bounds(start: date, end_inclusive: date, timezone: str) -> tuple[int, int]:
    if end_inclusive < start:
        raise ValueError("end date precedes start date")
    if end_inclusive == date.max:
        raise ValueError("end date must allow a next-day exclusive boundary")
    zone = ZoneInfo(timezone)
    first = datetime.combine(start, time.min, zone)
    last = datetime.combine(end_inclusive + timedelta(days=1), time.min, zone)
    return utc_microseconds(first), utc_microseconds(last)
