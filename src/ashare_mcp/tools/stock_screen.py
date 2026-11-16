"""get_stock_pack_batch —— 一次调用对一批候选做日线筛选并按盈亏比排序。

解决两个问题：
1. 自动化任务调用次数有限，逐只调用 get_stock_pack 往往只验证了一两只就下结论；
2. 小资金只买得起低价股，批量筛选时直接按价格上限过滤。

每只股票的计算与 get_stock_pack 完全相同（共用 build_pack），这里只返回紧凑摘要
和 reference_plan，并把 observation_ok=true 的排在前面、盈亏比从高到低。
"""

from __future__ import annotations

import time

from loguru import logger

from ..app import mcp
from ..cache import cached, ttl_session_aware_daily
from ..data_sources._base import DataSourceError, DataUnavailableError
from ..utils import codes
from ._helpers import DISCLAIMER_NOT_ADVICE, err, guard, ok
from .stock_pack import build_pack

MAX_SYMBOLS = 12
TIME_BUDGET_SECONDS = 50.0
DEFAULT_MAX_PRICE = 25.0


def _row(symbol: str, pack: dict, meta: dict, max_price: float | None) -> dict:
    last = pack["last_bar"]
    plan = pack["reference_plan"]
    close = last["close"]
    row = {
        "symbol": symbol,
        "close": close,
        "pct_change": last["pct_change"],
        "last_date": last["date"],
        "one_lot_cost": None if close is None else round(close * 100, 2),
        "status": "analyzed",
        "limit_up_like": last["limit_up_like"],
        "ma20_direction": pack["ma20_direction"],
        "range_position_20d_pct": pack["range_position_20d_pct"],
        "chase_triggered": pack["chase_triggered"],
        "supports": pack["supports"],
        "resistances": pack["resistances"],
        "kline_lag": meta["kline_lag"],
        "source": meta["source"],
        "observation_ok": plan.get("observation_ok", False),
        "gate_failed": plan.get("gate_failed", []),
        "rr": plan.get("rr"),
        "stop_distance_pct": plan.get("stop_distance_pct"),
        "buy_zone": plan.get("buy_zone"),
        "stop": plan.get("stop"),
        "take_profit_1": plan.get("take_profit_1"),
        "take_profit_2": plan.get("take_profit_2"),
        "position_cap_by_risk_pct": plan.get("position_cap_by_risk_pct"),
    }
    if max_price is not None and close is not None and close > max_price:
        # 价格超过上限：保留一行说明，不给价位计划，也不算合格
        row.update(
            status="price_above_max",
            observation_ok=False,
            gate_failed=[f"收盘价{close}元高于上限{max_price:g}元"],
            buy_zone=None,
            stop=None,
            take_profit_1=None,
            take_profit_2=None,
            rr=None,
        )
    return row


def _sort_key(row: dict) -> tuple:
    analyzed = row["status"] == "analyzed"
    rr = row.get("rr")
    return (
        0 if row.get("observation_ok") else (1 if analyzed else 2),
        0 if rr is not None else 1,
        -(rr or 0.0),
    )


@mcp.tool
@guard
@cached(ttl=ttl_session_aware_daily)
def get_stock_pack_batch(
    symbols: list[str],
    max_price: float = DEFAULT_MAX_PRICE,
    bars: int = 120,
) -> dict:
    """对一批候选（最多12只）做日线筛选，按"通过观察门槛、计划盈亏比从高到低"排序。

    每只返回: close、pct_change、one_lot_cost(1手所需资金)、observation_ok、gate_failed、
    rr(计划盈亏比)、buy_zone/stop/take_profit_1/2(取自 reference_plan)、supports/resistances、
    ma20_direction、range_position_20d_pct、chase_triggered、limit_up_like、kline_lag。
    收盘价高于 max_price(默认25元，传0表示不限)的股票只返回一行说明，不给价位计划。
    总耗时超过约50秒会停止，未处理的代码列在 not_processed 里，可再调用一次。
    单只失败不影响其他；全部失败才报错。口径与 get_stock_pack 完全一致。
    参数: symbols: 股票代码列表; max_price: 价格上限(元); bars: 日K根数，默认120。
    """
    if not symbols:
        return err("bad_request", "symbols 不能为空", "示例: ['600919','601009']")

    wanted: list[str] = []
    for item in symbols:
        code = codes.normalize(item)
        if code not in wanted:
            wanted.append(code)
    wanted = wanted[:MAX_SYMBOLS]
    cap = float(max_price) if max_price and float(max_price) > 0 else None

    started = time.monotonic()
    rows: list[dict] = []
    errors: list[dict] = []
    not_processed: list[str] = []

    for index, code in enumerate(wanted):
        if time.monotonic() - started > TIME_BUDGET_SECONDS:
            not_processed = wanted[index:]
            break
        try:
            pack, meta = build_pack(code, bars)
        except (DataSourceError, ValueError, KeyError) as exc:
            logger.warning(f"get_stock_pack_batch {code} failed: {exc}")
            errors.append({"symbol": code, "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        rows.append(_row(code, pack, meta, cap))

    if not rows:
        raise DataUnavailableError(
            "批量筛选没有任何一只成功取得日K: "
            + "；".join(f"{e['symbol']}={e['error']}" for e in errors[:3])
        )

    rows.sort(key=_sort_key)
    qualified = [r for r in rows if r["observation_ok"]]
    return ok(
        rows,
        requested=len(wanted),
        analyzed=sum(1 for r in rows if r["status"] == "analyzed"),
        qualified=len(qualified),
        qualified_symbols=[r["symbol"] for r in qualified],
        skipped_price_above_max=sum(1 for r in rows if r["status"] == "price_above_max"),
        max_price=cap,
        errors=errors or None,
        not_processed=not_processed or None,
        elapsed_seconds=round(time.monotonic() - started, 1),
        gate_note="observation_ok要求: 计划盈亏比>=1.5、止损距离<=6%、未触发追高、未涨停",
        note=DISCLAIMER_NOT_ADVICE,
    )
