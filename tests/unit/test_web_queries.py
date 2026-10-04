"""Display pagination must leave full-selection accounting totals untouched."""

import pytest

from claude_metrics.web.queries import page_groups
from claude_metrics.web.schemas import Query


@pytest.mark.parametrize("cursor,next_cursor", [(0, 1), (2, 3), (3, None), (20, None)])
def test_independent_group_pages_preserve_totals(cursor, next_cursor):
    total = {"known_cost_nanos": 9_007_199_254_740_993, "requests": 19}
    daily, models = list(range(4)), list(range(2))
    data = {"total": total, "daily": daily, "models": models}
    result = page_groups(data, ("daily", "models"), Query(tz="UTC", cursor=cursor, limit=1))
    assert result is data
    assert result["total"] is total
    assert result["daily"] == daily[cursor : cursor + 1]
    assert result["models"] == models[cursor : cursor + 1]
    assert result["group_counts"] == {"daily": 4, "models": 2}
    assert result["next_cursor"] == next_cursor
