"""get_market_breadth —— 全市场涨跌家数、涨跌停数（乐咕乐股，通过 akshare）。

给自动化任务的环境分级提供确定的数字：
* 情绪极端：非ST跌停约30家以上；
* 主线退潮的广度部分：上涨家数占比 <= 15%。
之前这些数字靠网页搜索，午间和收盘取到的版本不同，导致各次运行的环境色不一致。
"""

from __future__ import annotations

import re
from typing import Any

from ..app import mcp
from ..cache import cached, in_trading_session, now_cn, ttl_session_aware_daily
from ..data_sources._base import EmptyDataError
from ..data_sources._retry import call_ak
from ._helpers import DISCLAIMER_NOT_ADVICE, guard, ok

LIMIT_DOWN_THRESHOLD = 30   # 非ST跌停家数阈值
UP_RATIO_THRESHOLD = 15.0   # 上涨家数占比阈值（%）

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _to_int(value: Any) -> int | None:
    match = _NUMBER.search(str(value).replace(",", ""))
    return None if match is None else int(float(match.group()))


def _pick(items: dict[str, Any], *names: str) -> int | None:
    """按名称精确匹配取整数值，兼容空格差异。"""
    normalized = {re.sub(r"\s+", "", str(k)): v for k, v in items.items()}
    for name in names:
        if name in normalized:
            return _to_int(normalized[name])
    return None


def parse_breadth(items: dict[str, Any]) -> dict:
    """由乐咕乐股的 item/value 映射计算广度指标。纯函数，便于离线测试。"""
    up = _pick(items, "上涨")
    down = _pick(items, "下跌")
    if up is None or down is None:
        raise EmptyDataError("乐咕乐股返回中没有上涨/下跌家数")

    flat = _pick(items, "平盘")
    limit_up = _pick(items, "涨停")
    limit_down = _pick(items, "跌停")
    real_limit_up = _pick(items, "真实涨停")
    real_limit_down = _pick(items, "真实跌停")
    st_limit_down = _pick(items, "st st*跌停", "stst*跌停", "ST跌停")
    suspended = _pick(items, "停牌")

    # 规则要求"非ST跌停"：优先用"真实跌停"，其次用 跌停 - ST跌停，最后才用 跌停 并注明
    if real_limit_down is not None:
        non_st_down, basis = real_limit_down, "真实跌停（已剔除ST等）"
    elif limit_down is not None and st_limit_down is not None:
        non_st_down, basis = max(limit_down - st_limit_down, 0), "跌停减ST跌停"
    else:
        non_st_down, basis = limit_down, "跌停（可能含ST，口径偏大）"

    total = up + down
    up_ratio = up / total * 100.0 if total else None

    stat_time = None
    for key, value in items.items():
        if "统计日期" in str(key):
            stat_time = str(value).strip()

    return {
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "up_ratio_pct": None if up_ratio is None else round(up_ratio, 1),
        "limit_up_count": limit_up,
        "real_limit_up_count": real_limit_up,
        "limit_down_count": limit_down,
        "real_limit_down_count": real_limit_down,
        "non_st_limit_down": non_st_down,
        "non_st_limit_down_basis": basis,
        "suspended_count": suspended,
        "stat_time": stat_time,
        "emotion_extreme_hit": bool(non_st_down is not None and non_st_down >= LIMIT_DOWN_THRESHOLD),
        "breadth_weak_hit": bool(up_ratio is not None and up_ratio <= UP_RATIO_THRESHOLD),
    }


@mcp.tool
@guard
@cached(ttl=ttl_session_aware_daily)
def get_market_breadth() -> dict:
    """获取全市场涨跌家数与涨跌停数，并给出环境分级需要的两个判定。

    返回 up_count/down_count/up_ratio_pct(上涨家数占比)、limit_up_count、limit_down_count、
    non_st_limit_down(非ST跌停家数及口径)、stat_time(统计时间)，以及
    emotion_extreme_hit(非ST跌停>=30家)、breadth_weak_hit(上涨家数占比<=15%)。
    "主线退潮"还需结合净流出最大的行业板块跌幅>1.5%，由板块资金流工具判断。
    来源乐咕乐股；盘中为实时统计，收盘后为当日最终数据。不做快照，失败直接报错。
    """
    frame = call_ak("stock_market_activity_legu")
    items = {str(row["item"]): row["value"] for _, row in frame.iterrows()}
    data = parse_breadth(items)
    return ok(
        data,
        source="akshare / 乐咕乐股",
        retrieved_at=now_cn().isoformat(timespec="seconds"),
        market_session="trading" if in_trading_session() else "closed",
        thresholds={"non_st_limit_down": LIMIT_DOWN_THRESHOLD, "up_ratio_pct": UP_RATIO_THRESHOLD},
        note=DISCLAIMER_NOT_ADVICE,
    )
