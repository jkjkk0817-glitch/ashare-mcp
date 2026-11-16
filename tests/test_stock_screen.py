"""Offline tests for get_stock_pack_batch, the required-bar date helper and pre-open quote rules."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ashare_mcp import snapshot
from ashare_mcp.config import settings
from ashare_mcp.data_sources import quote_sources as qs
from ashare_mcp.data_sources._base import DataUnavailableError
from ashare_mcp.tools import quote_verify, stock_screen
from ashare_mcp.tools._helpers import guard
from ashare_mcp.utils import bars

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    snapshot._memory.clear()
    snapshot._last_saved.clear()
    monkeypatch.setattr(snapshot, "_client", None)
    monkeypatch.setattr(settings, "redis_url", "")
    yield
    snapshot._memory.clear()
    snapshot._last_saved.clear()


# --------------------------------------------------------------------------- #
# required_last_bar_date
# --------------------------------------------------------------------------- #
def _weekday_calendar(monkeypatch):
    def latest(ref):
        d = ref
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    monkeypatch.setattr(bars.dates, "latest_trade_date", latest)


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 11, 16, 9, 0, tzinfo=SH), date(2026, 11, 13)),    # 周一盘前 -> 上周五
        (datetime(2026, 11, 16, 10, 30, tzinfo=SH), date(2026, 11, 13)),  # 周一盘中 -> 上周五
        (datetime(2026, 11, 16, 15, 0, tzinfo=SH), date(2026, 11, 16)),   # 周一收盘后 -> 当天
        (datetime(2026, 11, 16, 20, 0, tzinfo=SH), date(2026, 11, 16)),
        (datetime(2026, 11, 14, 11, 0, tzinfo=SH), date(2026, 11, 13)),   # 周六 -> 周五
        (datetime(2026, 11, 17, 8, 30, tzinfo=SH), date(2026, 11, 16)),   # 周二盘前 -> 周一
    ],
)
def test_required_last_bar_date(monkeypatch, now, expected):
    _weekday_calendar(monkeypatch)
    assert bars.required_last_bar_date(now) == expected


# --------------------------------------------------------------------------- #
# Pre-open quote handling
# --------------------------------------------------------------------------- #
def _tencent(name="江苏银行", price=13.23, hhmmss="20261113161400"):
    fields = [""] * 60
    fields[1], fields[2], fields[3], fields[4] = name, "600919", str(price), str(price)
    fields[30], fields[32], fields[37] = hhmmss, "0.00", "0"
    return f'v_sh600919="{"~".join(fields)}";'


def _sina(name="江苏银行", price=13.23, prev=13.23, date_str="2026-11-13", time_str="15:00:00"):
    fields = [""] * 33
    fields[0], fields[1], fields[2], fields[3] = name, "0", str(prev), str(price)
    fields[30], fields[31] = date_str, time_str
    return f'var hq_str_sh600919="{",".join(fields)}";'


def test_sina_zero_price_pre_open_uses_previous_close():
    row = qs.parse_sina(_sina(price=0.0, prev=13.23, date_str="2026-11-16", time_str="09:00:03"))["sh600919"]
    assert row["price"] == pytest.approx(13.23)
    assert row["price_is_prev_close"] is True


def test_sina_zero_price_without_prev_close_is_dropped():
    assert qs.parse_sina(_sina(price=0.0, prev=0.0)) == {}


def test_verify_pre_open_date_mismatch_passes_with_equal_price():
    # 腾讯已切到今天的盘前快照(09:00)，新浪还停在上一交易日收盘(15:00)
    t = qs.parse_tencent(_tencent(hhmmss="20261116090003"))["sh600919"]
    s = qs.parse_sina(_sina())["sh600919"]
    result = qs.verify_pair(t, s)
    assert result["verified"] is True, result["reasons"]
    assert result["time_rule"] == "pre_open"


def test_verify_pre_open_both_today_passes():
    t = qs.parse_tencent(_tencent(hhmmss="20261116090003"))["sh600919"]
    s = qs.parse_sina(_sina(price=0.0, prev=13.23, date_str="2026-11-16", time_str="09:00:10"))["sh600919"]
    result = qs.verify_pair(t, s)
    assert result["verified"] is True and result["time_rule"] == "pre_open"


def test_verify_pre_open_still_rejects_price_mismatch_and_far_dates():
    t = qs.parse_tencent(_tencent(price=13.50, hhmmss="20261116090003"))["sh600919"]
    s = qs.parse_sina(_sina())["sh600919"]
    assert qs.verify_pair(t, s)["verified"] is False  # 价格偏差超标
    t = qs.parse_tencent(_tencent(hhmmss="20261116090003"))["sh600919"]
    s = qs.parse_sina(_sina(date_str="2026-10-20"))["sh600919"]
    result = qs.verify_pair(t, s)
    assert result["verified"] is False and any("日期" in r for r in result["reasons"])


def test_verify_mid_session_date_mismatch_still_fails():
    # 盘中(10:30)一边停在昨天，不属于盘前放行场景
    t = qs.parse_tencent(_tencent(hhmmss="20261116103000"))["sh600919"]
    s = qs.parse_sina(_sina())["sh600919"]
    result = qs.verify_pair(t, s)
    assert result["verified"] is False and any("日期" in r for r in result["reasons"])


def test_missing_source_is_named_in_reasons(monkeypatch):
    monkeypatch.setattr(qs, "fetch_tencent", lambda p: qs.parse_tencent(_tencent()))
    monkeypatch.setattr(qs, "fetch_sina", lambda p: {})
    result = guard(quote_verify.get_verified_quote.__wrapped__.__wrapped__)(["600919"])
    row = result["data"][0]
    assert row["verified"] is False and row["missing_sources"] == ["新浪"]
    assert any("新浪未返回" in r for r in row["verify_reasons"])


# --------------------------------------------------------------------------- #
# get_stock_pack_batch
# --------------------------------------------------------------------------- #
def _fake_pack(close, rr, ok_flag=True, failed=None, lag=False, session="closed", partial=False):
    plan = {
        "available": True, "observation_ok": ok_flag, "gate_failed": failed or [],
        "rr": rr, "stop_distance_pct": 3.0, "buy_zone": {"low": close * 0.97, "high": close * 0.99},
        "stop": close * 0.94, "take_profit_1": {"low": close * 1.05, "high": close * 1.06},
        "take_profit_2": None, "position_cap_by_risk_pct": 33.3,
    }
    pack = {
        "last_bar": {"date": "2026-11-13", "close": close, "pct_change": 1.0, "limit_up_like": False},
        "reference_plan": plan, "ma20_direction": "up", "range_position_20d_pct": 70.0,
        "chase_triggered": [], "supports": [{"price": close * 0.98, "basis": "MA5"}],
        "resistances": [{"price": close * 1.06, "basis": "近20日最高"}],
    }
    meta = {"kline_lag": lag, "source": "akshare", "market_session": session, "last_bar_partial": partial}
    return pack, meta


def _tool():
    return guard(stock_screen.get_stock_pack_batch.__wrapped__.__wrapped__)


def test_batch_sorts_qualified_by_rr_and_filters_price(monkeypatch):
    table = {
        "600001": _fake_pack(10.0, 1.8),
        "600002": _fake_pack(12.0, 2.6),
        "600003": _fake_pack(9.0, 0.4, ok_flag=False, failed=["计划盈亏比0.4低于1.5"]),
        "600519": _fake_pack(1435.0, 3.0),  # 超过价格上限
    }
    monkeypatch.setattr(stock_screen, "build_pack", lambda code, bars=120: table[code])
    result = _tool()(["600001", "600002", "600003", "600519"])
    assert result["ok"] is True
    order = [r["symbol"] for r in result["data"]]
    assert order == ["600002", "600001", "600003", "600519"]
    assert result["qualified_symbols"] == ["600002", "600001"]
    assert result["skipped_price_above_max"] == 1 and result["analyzed"] == 3
    top = result["data"][0]
    assert top["one_lot_cost"] == pytest.approx(1200.0)
    high = result["data"][-1]
    assert high["status"] == "price_above_max" and high["buy_zone"] is None
    assert high["observation_ok"] is False and "高于上限" in high["gate_failed"][0]


def test_batch_max_price_zero_means_no_limit(monkeypatch):
    monkeypatch.setattr(stock_screen, "build_pack", lambda code, bars=120: _fake_pack(80.0, 2.0))
    result = _tool()(["600001"], 0)
    assert result["data"][0]["status"] == "analyzed" and result["max_price"] is None


def test_batch_single_failure_does_not_break_others(monkeypatch):
    def fake(code, bars=120):
        if code == "600002":
            raise DataUnavailableError("kline down")
        return _fake_pack(10.0, 2.0)

    monkeypatch.setattr(stock_screen, "build_pack", fake)
    result = _tool()(["600001", "600002"])
    assert result["ok"] is True and result["analyzed"] == 1
    assert result["errors"][0]["symbol"] == "600002"


def test_batch_all_fail_is_an_error(monkeypatch):
    def down(code, bars=120):
        raise DataUnavailableError("down")

    monkeypatch.setattr(stock_screen, "build_pack", down)
    result = _tool()(["600001", "600002"])
    assert result["ok"] is False and result["error"] == "data_source_unavailable"


def test_batch_respects_time_budget_and_symbol_cap(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(stock_screen.time, "monotonic", lambda: clock["t"])

    def slow(code, bars=120):
        clock["t"] += 30.0
        return _fake_pack(10.0, 2.0)

    monkeypatch.setattr(stock_screen, "build_pack", slow)
    result = _tool()(["600001", "600002", "600003", "600004"])
    # 每只耗时30秒：处理完两只后已用60秒，超过50秒预算，第三只起不再处理
    assert result["analyzed"] == 2
    assert result["not_processed"] == ["600003", "600004"]

    many = [f"6000{n:02d}" for n in range(30)]
    monkeypatch.setattr(stock_screen, "build_pack", lambda code, bars=120: _fake_pack(10.0, 2.0))
    clock["t"] = 0.0
    monkeypatch.setattr(stock_screen.time, "monotonic", lambda: 0.0)
    assert _tool()(many)["requested"] == stock_screen.MAX_SYMBOLS


def test_batch_dedupes_and_rejects_empty(monkeypatch):
    monkeypatch.setattr(stock_screen, "build_pack", lambda code, bars=120: _fake_pack(10.0, 2.0))
    assert _tool()(["600001", "600001", "sh600001"])["requested"] == 1
    assert _tool()([])["error"] == "bad_request"


def test_batch_flags_stale_bar_during_session(monkeypatch):
    monkeypatch.setattr(
        stock_screen, "build_pack",
        lambda code, bars=120: _fake_pack(10.0, 2.0, session="trading", partial=False),
    )
    result = _tool()(["600001"])
    assert result["data"][0]["intraday_bar_stale"] is True
    assert "最新价复核" in result["intraday_note"]


def test_batch_no_stale_flag_when_bar_is_partial_or_market_closed(monkeypatch):
    monkeypatch.setattr(
        stock_screen, "build_pack",
        lambda code, bars=120: _fake_pack(10.0, 2.0, session="trading", partial=True),
    )
    result = _tool()(["600001"])
    assert result["data"][0]["intraday_bar_stale"] is False and result["intraday_note"] is None
    monkeypatch.setattr(stock_screen, "build_pack", lambda code, bars=120: _fake_pack(10.0, 2.0))
    assert _tool()(["600001"])["data"][0]["intraday_bar_stale"] is False
