"""get_index_trend —— 上证指数、沪深300 的 MA20 趋势与量能，直接给出"指数趋势"是否命中。

对应自动化任务环境分级的第1维度：
  上证或沪深300 收盘 < MA20 且 MA20 向下；或 成交额 < 近20日均值75% 且指数下跌。
成交额取不到时用成交量代替（只看比值，与单位无关），结果里会注明口径。
"""

from __future__ import annotations

import math
from datetime import timedelta

import pandas as pd

from ..app import mcp
from ..cache import cached, now_cn, ttl_session_aware_daily
from ..data_sources._base import DataSourceError, EmptyDataError
from ..data_sources._retry import call_ak
from ..utils import dates
from ..utils.bars import required_last_bar_date
from ._helpers import DISCLAIMER_NOT_ADVICE, guard, ok

INDEXES = [("sh000001", "上证指数"), ("sh000300", "沪深300")]

VOLUME_RATIO_THRESHOLD = 0.75
MA20_FLAT_BAND_PCT = 0.05  # MA20 五日变化不超过 ±0.05% 视为走平


def _num(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(float(value), digits)


def compute_index_trend(df: pd.DataFrame) -> dict:
    """由指数日线(date/close/volume)计算趋势指标。纯函数，便于离线测试。"""
    if df is None or df.empty or not {"date", "close"}.issubset(df.columns):
        raise EmptyDataError("指数日线缺少 date/close 字段")

    data = df.copy()
    data["date"] = data["date"].astype(str).str[:10]
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    if "volume" in data.columns:
        data["volume"] = pd.to_numeric(data["volume"], errors="coerce")
    data = data.dropna(subset=["close"]).sort_values("date").drop_duplicates("date", keep="last")
    data = data.reset_index(drop=True)
    if len(data) < 26:
        raise EmptyDataError(f"指数日线仅 {len(data)} 根，不足以计算MA20及其方向")

    close = data["close"]
    last_close = float(close.iloc[-1])
    prev_close = float(close.iloc[-2])
    day_pct = (last_close / prev_close - 1.0) * 100.0 if prev_close else None

    ma20_series = close.rolling(20).mean()
    ma20 = float(ma20_series.iloc[-1])
    ma20_5d_ago = float(ma20_series.iloc[-6])
    ma20_change_pct = (ma20 / ma20_5d_ago - 1.0) * 100.0 if ma20_5d_ago else None
    if ma20_change_pct is None:
        direction = None
    elif ma20_change_pct > MA20_FLAT_BAND_PCT:
        direction = "up"
    elif ma20_change_pct < -MA20_FLAT_BAND_PCT:
        direction = "down"
    else:
        direction = "flat"

    below_ma20 = last_close < ma20
    ma_hit = bool(below_ma20 and direction == "down")

    volume_ratio = None
    volume_hit = False
    if "volume" in data.columns and data["volume"].notna().sum() >= 21:
        last_volume = _num(data["volume"].iloc[-1])
        base = _num(data["volume"].iloc[-21:-1].mean())
        if last_volume is not None and base:
            volume_ratio = last_volume / base
            volume_hit = bool(
                volume_ratio < VOLUME_RATIO_THRESHOLD and day_pct is not None and day_pct < 0
            )

    ma = {f"ma{n}": _round(close.rolling(n).mean().iloc[-1]) if len(close) >= n else None
          for n in (5, 10, 20, 60)}

    reasons = []
    if ma_hit:
        reasons.append("收盘价低于MA20且MA20近5日向下")
    if volume_hit:
        reasons.append(f"量能为近20日均值的{volume_ratio * 100:.0f}%（<75%）且当日下跌")

    return {
        "last_date": str(data["date"].iloc[-1]),
        "close": _round(last_close),
        "day_pct_change": _round(day_pct),
        "ma": ma,
        "ma20_change_5d_pct": _round(ma20_change_pct, 3),
        "ma20_direction": direction,
        "below_ma20": bool(below_ma20),
        "ma_condition_hit": ma_hit,
        "volume_ratio_vs_20d": _round(volume_ratio, 3),
        "volume_condition_hit": volume_hit,
        "hit": bool(ma_hit or volume_hit),
        "reasons": reasons,
        "bars": len(data),
    }


def _fetch(symbol: str, start: str, end: str) -> tuple[pd.DataFrame, str]:
    try:
        return call_ak("stock_zh_index_daily", symbol=symbol), "akshare / 新浪财经"
    except DataSourceError:
        df = call_ak("stock_zh_index_daily_tx", symbol=symbol, start_date=start, end_date=end)
        if "volume" not in df.columns and "amount" in df.columns:
            df = df.rename(columns={"amount": "volume"})  # 腾讯该列只用于求比值
        return df, "akshare / 腾讯证券（新浪不可用时的备用）"


@mcp.tool
@guard
@cached(ttl=ttl_session_aware_daily)
def get_index_trend() -> dict:
    """获取上证指数、沪深300 的 MA5/10/20/60、MA20方向、量能比，并给出"指数趋势"是否命中。

    命中规则（与环境分级第1维度一致）: 任一指数 收盘<MA20 且 MA20近5日向下；
    或 量能<近20日均值75% 且当日下跌。量能用成交量比值近似成交额。
    返回 index_trend_hit(总体是否命中)、每个指数的明细与命中原因、日线最新日期。
    盘中日线最新日期通常是上一交易日。不做快照。
    """
    now = now_cn()
    expected_last = dates.latest_trade_date(now.date())
    required_last = required_last_bar_date(now)
    start = (expected_last - timedelta(days=200)).strftime("%Y%m%d")
    end = expected_last.strftime("%Y%m%d")

    rows = []
    sources = {}
    for symbol, label in INDEXES:
        frame, source = _fetch(symbol, start, end)
        info = compute_index_trend(frame)
        info.update(
            symbol=symbol,
            name=label,
            source=source,
            lag=info["last_date"] < required_last.strftime("%Y-%m-%d"),
        )
        rows.append(info)
        sources[symbol] = source

    return ok(
        rows,
        index_trend_hit=any(r["hit"] for r in rows),
        hit_reasons=[f"{r['name']}: {'；'.join(r['reasons'])}" for r in rows if r["hit"]],
        expected_last_trade_date=required_last.strftime("%Y-%m-%d"),
        volume_basis="成交量比值近似成交额比值",
        source="; ".join(sorted(set(sources.values()))),
        note=DISCLAIMER_NOT_ADVICE,
    )