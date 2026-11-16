"""Offline tests for get_index_trend: MA20 trend / volume rules and the fetch fallback."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from ashare_mcp import snapshot
from ashare_mcp.config import settings
from ashare_mcp.data_sources._base import DataUnavailableError, EmptyDataError
from ashare_mcp.tools import index_trend
from ashare_mcp.tools._helpers import guard


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    snapshot._memory.clear()
    snapshot._last_saved.clear()
    monkeypatch.setattr(snapshot, "_client", None)
    monkeypatch.setattr(settings, "redis_url", "")
    yield
    snapshot._memory.clear()
    snapshot._last_saved.clear()


def _frame(closes, volumes=None, end=date(2026, 11, 13)):
    days, d = [], end
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return pd.DataFrame(
        {"date": days, "close": closes, "volume": volumes or [1000.0] * len(closes)}
    )


def _down(n=70, start=3800.0, step=-3.0):
    return [start + step * i for i in range(n)]


def _up(n=70, start=3000.0, step=3.0):
    return [start + step * i for i in range(n)]


def test_downtrend_below_ma20_hits():
    info = index_trend.compute_index_trend(_frame(_down()))
    assert info["below_ma20"] is True
    assert info["ma20_direction"] == "down"
    assert info["ma_condition_hit"] is True and info["hit"] is True
    assert "MA20" in info["reasons"][0]
    assert info["last_date"] == "2026-11-13"


def test_uptrend_does_not_hit():
    info = index_trend.compute_index_trend(_frame(_up()))
    assert info["below_ma20"] is False
    assert info["ma20_direction"] == "up"
    assert info["hit"] is False and info["reasons"] == []


def test_below_ma20_but_ma20_rising_does_not_hit():
    closes = _up(60) + [3000.0 + 3.0 * 59 - 40.0] * 2  # 上行后回落，仍低于MA20但MA20仍在升
    info = index_trend.compute_index_trend(_frame(closes))
    assert info["ma20_direction"] == "up"
    assert info["ma_condition_hit"] is False


def test_ma20_values_match_pandas():
    closes = _down()
    info = index_trend.compute_index_trend(_frame(closes))
    series = pd.Series(closes)
    assert info["ma"]["ma20"] == pytest.approx(series.tail(20).mean(), abs=0.01)
    assert info["ma"]["ma60"] == pytest.approx(series.tail(60).mean(), abs=0.01)
    ma20 = series.rolling(20).mean()
    assert info["ma20_change_5d_pct"] == pytest.approx(
        (ma20.iloc[-1] / ma20.iloc[-6] - 1) * 100, abs=0.001
    )


def test_volume_condition_needs_both_low_volume_and_down_day():
    closes = _up()
    low_vol = [1000.0] * 69 + [600.0]
    closes_down_day = closes[:-1] + [closes[-2] - 5.0]
    hit = index_trend.compute_index_trend(_frame(closes_down_day, low_vol))
    assert hit["volume_ratio_vs_20d"] == pytest.approx(0.6, abs=0.001)
    assert hit["volume_condition_hit"] is True and hit["hit"] is True

    up_day = index_trend.compute_index_trend(_frame(closes, low_vol))
    assert up_day["volume_condition_hit"] is False  # 缩量但上涨，不算
    normal = index_trend.compute_index_trend(_frame(closes_down_day, [1000.0] * 70))
    assert normal["volume_condition_hit"] is False  # 下跌但量能正常，不算


def test_missing_volume_is_tolerated():
    frame = _frame(_down()).drop(columns=["volume"])
    info = index_trend.compute_index_trend(frame)
    assert info["volume_ratio_vs_20d"] is None and info["volume_condition_hit"] is False
    assert info["ma_condition_hit"] is True


def test_too_short_history_raises():
    with pytest.raises(EmptyDataError):
        index_trend.compute_index_trend(_frame(_down(20)))
    with pytest.raises(EmptyDataError):
        index_trend.compute_index_trend(pd.DataFrame())


# --------------------------------------------------------------------------- #
# Tool level
# --------------------------------------------------------------------------- #
def _tool():
    return guard(index_trend.get_index_trend.__wrapped__.__wrapped__)


def _patch_dates(monkeypatch):
    monkeypatch.setattr(index_trend.dates, "latest_trade_date", lambda ref=None: date(2026, 11, 13))


def test_tool_uses_sina_and_reports_overall_hit(monkeypatch):
    _patch_dates(monkeypatch)
    calls = []

    def fake_call_ak(name, **kwargs):
        calls.append(name)
        return _frame(_down())

    monkeypatch.setattr(index_trend, "call_ak", fake_call_ak)
    result = _tool()()
    assert result["ok"] is True and result["index_trend_hit"] is True
    assert calls == ["stock_zh_index_daily", "stock_zh_index_daily"]
    assert [r["name"] for r in result["data"]] == ["上证指数", "沪深300"]
    assert result["hit_reasons"] and all(r["lag"] is False for r in result["data"])


def test_tool_no_hit_in_uptrend(monkeypatch):
    _patch_dates(monkeypatch)
    monkeypatch.setattr(index_trend, "call_ak", lambda name, **k: _frame(_up()))
    result = _tool()()
    assert result["index_trend_hit"] is False and result["hit_reasons"] == []


def test_tool_falls_back_to_tencent_and_renames_amount(monkeypatch):
    _patch_dates(monkeypatch)

    def fake_call_ak(name, **kwargs):
        if name == "stock_zh_index_daily":
            raise DataUnavailableError("sina blocked")
        frame = _frame(_down()).rename(columns={"volume": "amount"})
        return frame

    monkeypatch.setattr(index_trend, "call_ak", fake_call_ak)
    result = _tool()()
    assert result["ok"] is True
    assert "腾讯" in result["data"][0]["source"]
    assert result["data"][0]["volume_ratio_vs_20d"] is not None


def test_tool_flags_lagging_daily_bar(monkeypatch):
    _patch_dates(monkeypatch)
    monkeypatch.setattr(
        index_trend, "call_ak", lambda name, **k: _frame(_up(), end=date(2026, 11, 12))
    )
    result = _tool()()
    assert all(r["lag"] is True for r in result["data"])


def test_tool_both_sources_down_is_an_error(monkeypatch):
    _patch_dates(monkeypatch)

    def down(name, **kwargs):
        raise DataUnavailableError("down")

    monkeypatch.setattr(index_trend, "call_ak", down)
    result = _tool()()
    assert result["ok"] is False and result["error"] == "data_source_unavailable"