
"""B. 资金流向 tools.

每个工具均带口径警告: 主力/大单拆分是东方财富按单笔成交金额机械分桶估算,
各软件口径不一,仅供参考,不代表真实机构意图。
"""

from __future__ import annotations

from typing import Literal

from ..app import mcp
from ..cache import cached, ttl_intraday_official
from ..data_sources import akshare_src as ak
from ._helpers import (
    DISCLAIMER_FUNDFLOW,
    DISCLAIMER_NOT_ADVICE,
    clamp_limit,
    guard,
    ok,
)


@mcp.tool
@guard
@cached(ttl=ttl_intraday_official)
def get_individual_fund_flow(symbol: str) -> dict:
    """获取个股近 100 日资金流向。

    返回主力/超大单/大单/中单/小单净流入及净占比。
    主力资金为东方财富按单笔成交金额机械分桶估算，仅供参考。
    """
    data = ak.individual_fund_flow(symbol)
    return ok(
        data,
        symbol=symbol,
        count=len(data),
        source="akshare / 东方财富",
        disclaimer=DISCLAIMER_FUNDFLOW,
        note=DISCLAIMER_NOT_ADVICE,
    )


@mcp.tool
@guard
@cached(ttl=ttl_intraday_official)
def get_market_fund_flow() -> dict:
    """获取沪深市场整体资金流向历史数据。"""
    data = ak.market_fund_flow()
    return ok(
        data,
        count=len(data),
        source="akshare / 东方财富",
        disclaimer=DISCLAIMER_FUNDFLOW,
        note=DISCLAIMER_NOT_ADVICE,
    )


@mcp.tool
@guard
@cached(ttl=ttl_intraday_official)
def get_sector_fund_flow_rank(
    period: Literal["今日", "5日", "10日"] = "今日",
    sector_type: Literal["行业资金流", "概念资金流"] = "行业资金流",
    limit: int = 50,
) -> dict:
    """获取行业/概念板块资金流向排名。

    period: 今日、5日、10日。
    sector_type: 行业资金流、概念资金流。
    limit: 返回数量。
    """
    n = clamp_limit(limit)
    data = ak.sector_fund_flow_rank(
        period,
        sector_type,
        limit=n,
    )
    return ok(
        data,
        period=period,
        sector_type=sector_type,
        count=len(data),
        source="akshare / 东方财富",
        disclaimer=DISCLAIMER_FUNDFLOW,
        note=DISCLAIMER_NOT_ADVICE,
    )


@mcp.tool
@guard
@cached(ttl=ttl_intraday_official)
def get_main_fund_flow_rank(
    scope: Literal[
        "全部股票",
        "沪深A股",
        "沪市A股",
        "科创板",
        "深市A股",
        "创业板",
        "沪市B股",
        "深市B股",
    ] = "全部股票",
    limit: int = 50,
) -> dict:
    """获取个股主力净流入排名。

    数据源: 东方财富分页接口。
    排序: 按主力净流入金额从高到低。
    支持范围:
      全部股票、沪深A股、沪市A股、科创板、
      深市A股、创业板、沪市B股、深市B股。
    limit: 默认 50，最大 200。
    金额单位: 人民币元。

    口径警告: 主力资金为东方财富按单笔金额机械分桶估算，
    各软件口径不一，仅供参考，不代表真实机构意图。
    """
    n = max(1, min(clamp_limit(limit), 200))

    data = ak.main_fund_flow_rank(
        symbol=scope,
        limit=n,
    )

    return ok(
        data,
        scope=scope,
        count=len(data),
        source="东方财富分页接口",
        sort_by="main_net_inflow_desc",
        amount_unit="CNY",
        disclaimer=DISCLAIMER_FUNDFLOW,
        note=DISCLAIMER_NOT_ADVICE,
    )
