"""Optional per-stock metadata and sentiment tools, isolated from realtime fetch."""

from __future__ import annotations

from datetime import datetime, timezone

from ..app import mcp
from ..cache import TTL_INFO, cached
from ..data_sources import akshare_src as ak
from ..utils import codes
from ._helpers import DISCLAIMER_NOT_ADVICE, guard, ok


def _data_date(row: dict) -> str | None:
    for key in ("交易日", "数据日期", "日期", "trade_date", "date"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


@mcp.tool
@guard
@cached(ttl=TTL_INFO)
def get_stock_basic_info(symbol: str) -> dict:
    """获取单只A股基本资料。独立于实时行情，不影响 a___fetch。

    参数 symbol: 股票代码，如 600519。
    返回 data: 东方财富个股资料字段；retrieved_at 为本服务获取时间，不代表每个字段的更新日期。
    """
    code = codes.normalize(symbol)
    data = ak.individual_info(code)
    return ok(
        data=data,
        symbol=code,
        count=len(data),
        source="akshare / 东方财富个股资料",
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        note="基本资料为独立慢速查询；失败不会影响实时行情工具。",
        disclaimer=DISCLAIMER_NOT_ADVICE,
    )


@mcp.tool
@guard
@cached(ttl=300)
def get_stock_sentiment(symbol: str) -> dict:
    """获取单只A股千股千评/市场关注度辅助指标。独立于实时行情。

    参数 symbol: 股票代码，如 600519。
    注意：当前 AkShare 接口先获取东方财富全市场千股千评快照，再筛选该股票；
    因此本工具可能较慢，但不会拖慢 a___fetch。结果缓存5分钟。
    """
    code = codes.normalize(symbol)
    data = ak.stock_comment_one(code)
    if not data:
        return ok(
            data={},
            symbol=code,
            count=0,
            source="akshare / 东方财富千股千评",
            data_date=None,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            note="本次快照中未找到该股票；该工具独立运行，不影响实时行情。",
            disclaimer=DISCLAIMER_NOT_ADVICE,
        )

    return ok(
        data=data,
        symbol=code,
        count=1,
        source="akshare / 东方财富千股千评",
        data_date=_data_date(data),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        note="辅助指标为市场快照数据，可能存在更新延迟；不代表实时行情。",
        disclaimer=DISCLAIMER_NOT_ADVICE,
    )
