"""HTTP filter validation and display pagination; no accounting calculations."""

from zoneinfo import ZoneInfoNotFoundError

from fastapi import HTTPException, Request
from pydantic import ValidationError

from claude_metrics import reports
from claude_metrics.config import Settings
from claude_metrics.web import schemas


def query(request: Request, settings: Settings) -> tuple[schemas.Query, reports.ReportFilter]:
    params = dict(request.query_params)
    if len(params) != len(request.query_params.multi_items()):
        raise HTTPException(422, "Repeated query parameters are not supported.")
    params = {key: value for key, value in params.items() if value != ""}
    params.setdefault("tz", settings.timezone)
    try:
        parsed = schemas.Query.model_validate(params)
        filters = reports.ReportFilter(
            parsed.tz, parsed.start, parsed.end, parsed.project, parsed.model
        )
        filters.where()
        return parsed, filters
    except (ValidationError, ValueError, ZoneInfoNotFoundError, OverflowError):
        raise HTTPException(
            422,
            "Invalid filters. Use paired YYYY-MM-DD dates, a valid IANA timezone, "
            "a non-negative cursor, and a limit from 1 to 200.",
        ) from None


def page_groups(data: dict, names: tuple[str, ...], q: schemas.Query) -> dict:
    counts = {name: len(data[name]) for name in names}
    for name in names:
        data[name] = data[name][q.cursor : q.cursor + q.limit]
    data["group_counts"] = counts
    data["next_cursor"] = (
        q.cursor + q.limit if any(n > q.cursor + q.limit for n in counts.values()) else None
    )
    return data
