"""Date extraction and freshness checks for source records.

These helpers deliberately use the Asia/Shanghai calendar date so UTC-hosted
Render instances do not mislabel China-market records around midnight.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

MARKET_FLOW_MAX_AGE_DAYS = 14
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_record_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None
    text = text[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def latest_record_date(records: list[dict], fields=("date", "trade_date", "日期")) -> date | None:
    parsed = []
    for row in records or []:
        if not isinstance(row, dict):
            continue
        for field in fields:
            value = parse_record_date(row.get(field))
            if value is not None:
                parsed.append(value)
                break
    return max(parsed) if parsed else None


def data_age_days(data_date: date, today: date | None = None) -> int:
    reference = today or datetime.now(_SHANGHAI).date()
    return (reference - data_date).days
