"""日K"应该已经存在"的最近日期。

交易日 15:00 之前，当天的日K还没有收盘，最近一根完整日K是上一交易日；
盘后、非交易日则是最近一个交易日本身。盘前把当天日期当成预期日期会造成
"日K滞后"的误报。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..cache import now_cn
from . import dates

MARKET_CLOSE = time(15, 0)


def required_last_bar_date(now: datetime | None = None) -> date:
    now = now or now_cn()
    today = now.date()
    latest = dates.latest_trade_date(today)
    if latest == today and now.time() < MARKET_CLOSE:
        return dates.latest_trade_date(today - timedelta(days=1))
    return latest
