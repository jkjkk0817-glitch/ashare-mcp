"""Offline tests for get_stock_pack: pure computation and the tool-level flow."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from ashare_mcp import snapshot
from ashare_mcp.config import settings
from ashare_mcp.data_sources._base import DataUnavailableError, EmptyDataError
from ashare_mcp.tools import stock_pack
from ashare_mcp.tools._helpers import guard


@pytest.fixture(autouse=True)
def _clean_snapshot_state(monkeypatch):
    snapshot._memory.clear()
    snapshot._last_saved.clear()
    monkeypatch.setattr(snapshot, "_client", None)
    monkeypatch.setattr(settings, "redis_url", "")
    yield
    snapshot._memory.clear()
    snapshot._last_saved.clear()


def _bars(n: int, start_price: float = 10.0, step: float = 0.05, end: date = date(2026, 11, 13)):
    """n 根工作日日K，价格线性上行，便于手算校验。"""
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    rows = []
    prev = None
    for i, day in enumerate(days):
        close = round(start_price + step * i, 4)
        pct = None if prev is None else round((close / prev - 1) * 100, 4)
        rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "open": close - 0.02,
                "high": close + 0.10,
                "low": close - 0.10,
                "close": close,
                "volume": 1000 + i,
                "amount": 1e7 + i,
                "pct_change": pct,
                "turnover_rate": 1.0,
            }
        )
        prev = close
    return rows


def test_compute_pack_matches_pandas():
    rows = _bars(130)
    pack = stock_pack.compute_pack(rows)
    close = pd.Series([r["close"] for r in rows])

    assert pack["last_bar"]["date"] == "2026-11-13"
    assert pack["last_bar"]["close"] == pytest.approx(close.iloc[-1], abs=0.01)
    assert pack["ma"]["ma5"] == pytest.approx(close.tail(5).mean(), abs=0.01)
    assert pack["ma"]["ma20"] == pytest.approx(close.tail(20).mean(), abs=0.01)
    assert pack["ma"]["ma60"] == pytest.approx(close.tail(60).mean(), abs=0.01)
    assert pack["returns_pct"]["ret_5d"] == pytest.approx(
        (close.iloc[-1] / close.iloc[-6] - 1) * 100, abs=0.01
    )
    assert pack["range"]["high_20d"] == pytest.approx(close.tail(20).max() + 0.10, abs=0.01)
    assert pack["range"]["low_60d"] == pytest.approx(close.tail(60).min() - 0.10, abs=0.01)
    assert pack["ma20_direction"] == "up"
    assert pack["bars"]["count"] == 130 and pack["bars"]["ma60_available"] is True


def test_supports_below_and_resistances_above_close():
    pack = stock_pack.compute_pack(_bars(130))
    close = pack["last_bar"]["close"]
    assert pack["supports"], "上行趋势中均线应在收盘价下方"
    assert all(s["price"] < close for s in pack["supports"])
    prices = [s["price"] for s in pack["supports"]]
    assert prices == sorted(prices, reverse=True)
    assert all(r["price"] > close for r in pack["resistances"])
    assert all(s["basis"] for s in pack["supports"] + pack["resistances"])


def test_short_history_leaves_long_windows_empty():
    pack = stock_pack.compute_pack(_bars(30))
    assert pack["ma"]["ma60"] is None
    assert pack["range"]["high_60d"] is None
    assert pack["returns_pct"]["ret_60d"] is None
    assert pack["bars"]["ma60_available"] is False
    assert pack["ma"]["ma20"] is not None


def test_too_few_bars_raises_empty():
    with pytest.raises(EmptyDataError):
        stock_pack.compute_pack(_bars(3))
    with pytest.raises(EmptyDataError):
        stock_pack.compute_pack([])


def test_limit_up_and_chase_checks_trigger():
    rows = _bars(70, start_price=10.0, step=0.0)  # 平盘
    # 最后三根连续涨停式大阳线
    price = 10.0
    for i in range(-3, 0):
        new = round(price * 1.10, 2)
        rows[i].update(
            {"open": price, "high": new, "low": price, "close": new, "pct_change": 10.0}
        )
        price = new
    pack = stock_pack.compute_pack(rows)
    assert pack["last_bar"]["limit_up_like"] is True
    assert pack["chase_checks"]["big_bull_streak"]["value"] == 3
    assert pack["chase_checks"]["big_bull_streak"]["triggered"] is True
    assert pack["chase_checks"]["ret_5d"]["triggered"] is True  # 约+33%
    assert "ret_5d" in pack["chase_triggered"]
    assert pack["resistances"] == [] or all(
        r["price"] > pack["last_bar"]["close"] for r in pack["resistances"]
    )


def test_calm_stock_triggers_nothing():
    pack = stock_pack.compute_pack(_bars(130))
    assert pack["chase_triggered"] == []
    assert pack["last_bar"]["limit_up_like"] is False


def test_unsorted_and_duplicate_rows_are_normalised():
    rows = _bars(40)
    shuffled = list(reversed(rows)) + [rows[-1]]
    pack = stock_pack.compute_pack(shuffled)
    assert pack["bars"]["count"] == 40
    assert pack["bars"]["last_date"] == rows[-1]["date"]


# --------------------------------------------------------------------------- #
# Tool-level flow
# --------------------------------------------------------------------------- #
def _patch_dates(monkeypatch, last=date(2026, 11, 13)):
    monkeypatch.setattr(stock_pack.dates, "latest_trade_date", lambda ref=None: last)


def test_tool_uses_akshare_and_reports_no_lag(monkeypatch):
    _patch_dates(monkeypatch)
    monkeypatch.setattr(stock_pack.ak, "daily_hist", lambda *a, **k: _bars(130))
    result = guard(stock_pack.get_stock_pack.__wrapped__.__wrapped__)("600919", 120)
    assert result["ok"] is True
    assert result["source"] == "akshare"
    assert result["kline_lag"] is False and result["kline_lag_note"] is None
    assert result["symbol"] == "600919"
    assert result["data"]["bars"]["count"] == 120  # 只取最近 bars 根


def test_tool_falls_back_to_baostock_and_flags_lag(monkeypatch):
    _patch_dates(monkeypatch)

    def ak_down(*a, **k):
        raise DataUnavailableError("eastmoney down")

    monkeypatch.setattr(stock_pack.ak, "daily_hist", ak_down)
    lagging = _bars(100, end=date(2026, 11, 12))
    monkeypatch.setattr(stock_pack.bs, "daily_kline", lambda *a, **k: lagging)
    result = guard(stock_pack.get_stock_pack.__wrapped__.__wrapped__)("601009", 120)
    assert result["ok"] is True
    assert result["source"] == "baostock (fallback)"
    assert result["kline_lag"] is True
    assert "落后" in result["kline_lag_note"]


def test_tool_reports_unavailable_when_both_sources_fail(monkeypatch):
    _patch_dates(monkeypatch)

    def down(*a, **k):
        raise DataUnavailableError("down")

    monkeypatch.setattr(stock_pack.ak, "daily_hist", down)
    monkeypatch.setattr(stock_pack.bs, "daily_kline", down)
    result = guard(stock_pack.get_stock_pack.__wrapped__.__wrapped__)("600919", 120)
    assert result["ok"] is False and result["error"] == "data_source_unavailable"


# --------------------------------------------------------------------------- #
# reference_plan: 固定规则，同样的数据永远得到同样的结果
# --------------------------------------------------------------------------- #
def test_reference_plan_matches_hand_calculation():
    supports = [
        {"price": 6.33, "basis": "MA5"},
        {"price": 6.24, "basis": "MA10"},
        {"price": 6.08, "basis": "MA20"},
    ]
    resistances = [{"price": 6.60, "basis": "近20日最高/近60日最高"}]
    plan = stock_pack._reference_plan(6.53, supports, resistances)

    assert plan["available"] is True
    assert plan["buy_zone"]["low"] == pytest.approx(6.30, abs=0.01)
    assert plan["buy_zone"]["high"] == pytest.approx(6.39, abs=0.01)
    assert plan["stop"] == pytest.approx(6.05, abs=0.01)
    assert plan["take_profit_1"]["low"] == pytest.approx(6.53, abs=0.01)
    assert plan["take_profit_1"]["high"] == pytest.approx(6.60, abs=0.01)
    assert plan["take_profit_2"] is None
    expected_rr = (6.53 - 6.39) / (6.39 - 6.05)
    assert plan["rr"] == pytest.approx(expected_rr, abs=0.02)
    assert plan["rr"] < 1  # 紧贴阶段高点，盈亏比差，必须如实暴露
    assert plan["stop_too_far"] is False
    assert plan["position_cap_by_risk_pct"] == pytest.approx(
        2.0 / max(plan["stop_distance_pct"] + 3.0, 5.0) * 100, abs=0.1
    )


def test_reference_plan_is_deterministic():
    pack_a = stock_pack.compute_pack(_bars(130))
    pack_b = stock_pack.compute_pack(_bars(130))
    assert pack_a["reference_plan"] == pack_b["reference_plan"]


def test_reference_plan_ordering_invariants_on_real_shape():
    plan = stock_pack.compute_pack(_bars(130))["reference_plan"]
    assert plan["available"] is True
    assert plan["stop"] < plan["buy_zone"]["low"] <= plan["buy_zone"]["high"]
    tp1 = plan["take_profit_1"]
    assert tp1 is None or tp1["low"] > plan["buy_zone"]["high"]


def test_reference_plan_single_support_uses_three_percent_stop():
    plan = stock_pack._reference_plan(
        10.0, [{"price": 9.8, "basis": "MA20"}], [{"price": 10.8, "basis": "近20日最高"}]
    )
    assert plan["stop"] == pytest.approx(9.8 * 0.97, abs=0.01)
    assert "仅一个支撑" in plan["stop_basis"]


def test_reference_plan_without_resistance_has_no_take_profit_or_rr():
    plan = stock_pack._reference_plan(10.0, [{"price": 9.5, "basis": "MA5"}], [])
    assert plan["take_profit_1"] is None and plan["rr"] is None
    assert any("止盈位缺失" in n for n in plan["notes"])


def test_reference_plan_flags_far_stop():
    plan = stock_pack._reference_plan(
        10.0,
        [{"price": 9.9, "basis": "MA5"}, {"price": 9.2, "basis": "MA10"}, {"price": 8.5, "basis": "MA20"}],
        [{"price": 11.0, "basis": "近20日最高"}],
    )
    assert plan["stop_too_far"] is True
    assert any("6%" in n for n in plan["notes"])


def test_reference_plan_unavailable_without_supports():
    plan = stock_pack._reference_plan(10.0, [], [{"price": 11.0, "basis": "x"}])
    assert plan["available"] is False and "reason" in plan


def test_reference_plan_skips_resistance_too_close_to_buy_zone():
    plan = stock_pack._reference_plan(
        10.0,
        [{"price": 9.95, "basis": "MA5"}, {"price": 9.7, "basis": "MA10"}],
        [{"price": 10.05, "basis": "近20日最高"}, {"price": 10.8, "basis": "近60日最高"}],
    )
    assert plan["take_profit_1"]["high"] == pytest.approx(10.8, abs=0.01)
    assert plan["take_profit_1"]["low"] > plan["buy_zone"]["high"]
    assert any("已跳过" in n for n in plan["notes"])


def test_reference_plan_only_too_close_resistance_means_no_take_profit():
    plan = stock_pack._reference_plan(
        10.0, [{"price": 9.9, "basis": "MA5"}], [{"price": 10.02, "basis": "近20日最高"}]
    )
    assert plan["take_profit_1"] is None and plan["rr"] is None
