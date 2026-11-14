"""get_stock_pack —— 一次调用返回一只股票的"日线技术数据包"。

目的：让自动化任务对每只候选只调用一次，就拿到收盘价、涨跌幅、均线、阶段涨幅、
支撑压力位候选和追高检验数据，减少调用次数和失败点。

只使用日K（主源 akshare，失败降级 baostock，口径为前复权）。
不含实时行情、资金流、公告；这些仍用对应工具单独获取。
所有位置均为机械计算的"技术位候选"，注明依据，不构成买卖建议。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ..app import mcp
from ..cache import cached, ttl_intraday_official
from ..data_sources import akshare_src as ak
from ..data_sources import baostock_src as bs
from ..data_sources._base import DataSourceError, EmptyDataError
from ..utils import codes, dates
from ._helpers import DISCLAIMER_NOT_ADVICE, clamp_limit, guard, ok

_SHANGHAI = ZoneInfo("Asia/Shanghai")

# 追高预警阈值（与自动化任务提示词一致）
CHASE_RET_5D = 15.0
CHASE_RET_10D = 25.0
CHASE_DEV_MA20 = 15.0
CHASE_BIG_BULL_PCT = 5.0
CHASE_BIG_BULL_STREAK = 3
LIMIT_UP_PCT = 9.9  # 沪深主板涨停约10%


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def _pct_change_between(series: pd.Series, bars_ago: int) -> float | None:
    if len(series) <= bars_ago:
        return None
    base = _num(series.iloc[-1 - bars_ago])
    last = _num(series.iloc[-1])
    if not base or last is None:
        return None
    return (last / base - 1.0) * 100.0


def _ma(series: pd.Series, window: int) -> float | None:
    if len(series) < window:
        return None
    return _num(series.rolling(window).mean().iloc[-1])


def _window_extreme(series: pd.Series, window: int, kind: str) -> float | None:
    if len(series) < window:
        return None
    tail = series.iloc[-window:]
    return _num(tail.max() if kind == "high" else tail.min())


def _direction(pct: float | None, flat_band: float = 0.3) -> str | None:
    if pct is None:
        return None
    if pct > flat_band:
        return "up"
    if pct < -flat_band:
        return "down"
    return "flat"


def _big_bull_streak(pct_series: pd.Series) -> int:
    streak = 0
    for value in reversed(list(pct_series)):
        number = _num(value)
        if number is not None and number >= CHASE_BIG_BULL_PCT:
            streak += 1
        else:
            break
    return streak


def _levels(close: float, ma: dict, highs: dict, lows: dict) -> tuple[list, list]:
    support_candidates = [
        (ma.get("ma5"), "MA5"),
        (ma.get("ma10"), "MA10"),
        (ma.get("ma20"), "MA20"),
        (ma.get("ma60"), "MA60"),
        (lows.get("low_20d"), "近20日最低"),
        (lows.get("low_60d"), "近60日最低"),
    ]
    resistance_candidates = [
        (highs.get("high_20d"), "近20日最高"),
        (highs.get("high_60d"), "近60日最高"),
        (ma.get("ma5"), "MA5"),
        (ma.get("ma10"), "MA10"),
        (ma.get("ma20"), "MA20"),
        (ma.get("ma60"), "MA60"),
    ]
    supports = sorted(
        ((p, b) for p, b in support_candidates if p is not None and p < close),
        key=lambda x: -x[0],
    )
    resistances = sorted(
        ((p, b) for p, b in resistance_candidates if p is not None and p > close),
        key=lambda x: x[0],
    )

    def dedupe(items: list[tuple[float, str]]) -> list[dict]:
        out: list[dict] = []
        for price, basis in items:
            rounded = round(price, 2)
            if out and out[-1]["price"] == rounded:
                out[-1]["basis"] += f"/{basis}"
                continue
            out.append({"price": rounded, "basis": basis})
        return out[:3]

    return dedupe(supports), dedupe(resistances)


def compute_pack(records: list[dict]) -> dict:
    """由日K记录计算数据包。纯函数，便于离线测试。"""
    df = pd.DataFrame(records)
    needed = {"date", "close", "high", "low"}
    if df.empty or not needed.issubset(df.columns):
        raise EmptyDataError("日K缺少 date/close/high/low 字段，无法计算数据包")

    for col in ("open", "high", "low", "close", "volume", "amount", "pct_change", "turnover_rate"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = df["date"].astype(str).str[:10]
    df = df.dropna(subset=["close"]).sort_values("date").drop_duplicates("date", keep="last")
    df = df.reset_index(drop=True)
    if len(df) < 5:
        raise EmptyDataError(f"日K仅 {len(df)} 根，不足以计算数据包")

    close = df["close"]
    last = df.iloc[-1]
    last_close = float(last["close"])

    if "pct_change" in df.columns and _num(last.get("pct_change")) is not None:
        last_pct = float(last["pct_change"])
    else:
        prev = _num(close.iloc[-2])
        last_pct = (last_close / prev - 1.0) * 100.0 if prev else None

    ma = {f"ma{n}": _ma(close, n) for n in (5, 10, 20, 60)}
    ma20_series = close.rolling(20).mean()
    ma20_slope = (
        _pct_change_between(ma20_series.dropna(), 5) if ma20_series.notna().sum() > 5 else None
    )

    returns = {
        "ret_5d": _pct_change_between(close, 5),
        "ret_10d": _pct_change_between(close, 10),
        "ret_20d": _pct_change_between(close, 20),
        "ret_60d": _pct_change_between(close, 60),
    }
    deviation_ma20 = (
        (last_close / ma["ma20"] - 1.0) * 100.0 if ma["ma20"] else None
    )

    highs = {
        "high_20d": _window_extreme(df["high"], 20, "high"),
        "high_60d": _window_extreme(df["high"], 60, "high"),
    }
    lows = {
        "low_20d": _window_extreme(df["low"], 20, "low"),
        "low_60d": _window_extreme(df["low"], 60, "low"),
    }
    range_pos_20d = None
    if highs["high_20d"] is not None and lows["low_20d"] is not None:
        span = highs["high_20d"] - lows["low_20d"]
        if span > 0:
            range_pos_20d = (last_close - lows["low_20d"]) / span * 100.0

    volume_ratio_5_20 = None
    if "volume" in df.columns and len(df) >= 20:
        v5 = _num(df["volume"].iloc[-5:].mean())
        v20 = _num(df["volume"].iloc[-20:].mean())
        if v5 is not None and v20:
            volume_ratio_5_20 = v5 / v20

    streak = _big_bull_streak(df["pct_change"]) if "pct_change" in df.columns else 0
    upper_shadow_pct = None
    if "open" in df.columns:
        body_top = max(_num(last.get("open")) or last_close, last_close)
        upper_shadow_pct = (float(last["high"]) - body_top) / last_close * 100.0

    supports, resistances = _levels(last_close, ma, highs, lows)

    chase_checks = {
        "ret_5d": {"value": _round(returns["ret_5d"]), "threshold": CHASE_RET_5D,
                   "triggered": returns["ret_5d"] is not None and returns["ret_5d"] > CHASE_RET_5D},
        "ret_10d": {"value": _round(returns["ret_10d"]), "threshold": CHASE_RET_10D,
                    "triggered": returns["ret_10d"] is not None
                    and returns["ret_10d"] > CHASE_RET_10D},
        "deviation_ma20": {"value": _round(deviation_ma20), "threshold": CHASE_DEV_MA20,
                           "triggered": deviation_ma20 is not None
                           and deviation_ma20 > CHASE_DEV_MA20},
        "big_bull_streak": {"value": streak, "threshold": CHASE_BIG_BULL_STREAK,
                            "definition": f"连续单日涨幅>={CHASE_BIG_BULL_PCT:g}%的K线根数",
                            "triggered": streak >= CHASE_BIG_BULL_STREAK},
    }
    chase_triggered = [k for k, v in chase_checks.items() if v["triggered"]]

    limit_up_like = last_pct is not None and last_pct >= LIMIT_UP_PCT

    return {
        "last_bar": {
            "date": str(last["date"]),
            "close": _round(last_close),
            "open": _round(_num(last.get("open"))) if "open" in df.columns else None,
            "high": _round(_num(last["high"])),
            "low": _round(_num(last["low"])),
            "pct_change": _round(last_pct),
            "volume": _num(last.get("volume")) if "volume" in df.columns else None,
            "amount": _num(last.get("amount")) if "amount" in df.columns else None,
            "turnover_rate": _round(_num(last.get("turnover_rate")))
            if "turnover_rate" in df.columns else None,
            "limit_up_like": bool(limit_up_like),
            "upper_shadow_pct": _round(upper_shadow_pct),
        },
        "ma": {k: _round(v) for k, v in ma.items()},
        "ma20_slope_5d_pct": _round(ma20_slope, 3),
        "ma20_direction": _direction(ma20_slope),
        "returns_pct": {k: _round(v) for k, v in returns.items()},
        "deviation_ma20_pct": _round(deviation_ma20),
        "range": {k: _round(v) for k, v in {**highs, **lows}.items()},
        "range_position_20d_pct": _round(range_pos_20d, 1),
        "volume_ratio_5_20": _round(volume_ratio_5_20, 2),
        "supports": supports,
        "resistances": resistances,
        "chase_checks": chase_checks,
        "chase_triggered": chase_triggered,
        "bars": {
            "count": len(df),
            "first_date": str(df.iloc[0]["date"]),
            "last_date": str(last["date"]),
            "ma60_available": len(df) >= 60,
        },
    }


def _fetch_daily(symbol: str, start: str, end: str) -> tuple[list[dict], str]:
    try:
        return ak.daily_hist(symbol, start, end, "qfq"), "akshare"
    except DataSourceError:
        return bs.daily_kline(symbol, start, end, "qfq"), "baostock (fallback)"


@mcp.tool
@guard
@cached(ttl=ttl_intraday_official)
def get_stock_pack(symbol: str, bars: int = 120) -> dict:
    """获取一只股票的日线技术数据包（每只候选只需调用一次）。

    返回 data: last_bar(最近一根日K: 日期/收盘/涨跌幅/成交额/换手率/是否涨停)、
    ma(MA5/10/20/60)、ma20_direction、returns_pct(近5/10/20/60日涨幅)、
    deviation_ma20_pct、range(近20/60日高低点)、supports/resistances(技术位候选，
    注明依据: 均线/区间高低点)、chase_checks(追高预警逐项结果)、bars(K线根数与日期)。
    口径: 前复权日K，主源 akshare，失败降级 baostock。
    注意: 价格为最近一个交易日收盘，不是实时价；不含资金流、公告。
    参数:
      symbol: 股票代码(多种写法均可)。
      bars: 取最近多少根日K，默认120，范围60-250。
    """
    sym = codes.normalize(symbol)
    n_bars = max(60, min(clamp_limit(bars, default=120, maximum=250), 250))

    today_cn = datetime.now(_SHANGHAI).date()
    expected_last = dates.latest_trade_date(today_cn)
    start = (expected_last - timedelta(days=int(n_bars * 1.6) + 10)).strftime("%Y-%m-%d")
    end = expected_last.strftime("%Y-%m-%d")

    records, source = _fetch_daily(sym, start, end)
    # 只保留最近 n_bars 根参与计算，保证窗口口径一致
    recent = sorted(records, key=lambda r: str(r.get("date")))[-n_bars:]
    pack = compute_pack(recent)

    last_date = pack["bars"]["last_date"]
    lag = last_date < end
    return ok(
        pack,
        symbol=sym,
        source=source,
        adjust="qfq",
        price_basis="最近一个交易日日K收盘，非实时价",
        expected_last_trade_date=end,
        kline_lag=bool(lag),
        kline_lag_note=(
            f"日K最新日期{last_date}落后于最近交易日{end}，不得称为最新完整日K。"
            if lag else None
        ),
        note=DISCLAIMER_NOT_ADVICE,
    )
