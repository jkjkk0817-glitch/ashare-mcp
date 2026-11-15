"""Offline tests for the intraday pieces: dual-source quote verification, index quotes,
Beijing-time session-aware cache TTLs, and the stock pack's partial-bar flag."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ashare_mcp import cache, snapshot
from ashare_mcp.config import settings
from ashare_mcp.data_sources import quote_sources as qs
from ashare_mcp.tools import quote_verify, stock_pack
from ashare_mcp.tools._helpers import guard

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
# Sample raw responses
# --------------------------------------------------------------------------- #
def _tencent_line(prefixed, name, code, price, prev, pct, hhmmss="20261113150003",
                  amount_wan="196600"):
    fields = [""] * 60
    fields[1], fields[2], fields[3], fields[4] = name, code, str(price), str(prev)
    fields[5], fields[6] = str(price - 0.1), "123456"
    fields[30], fields[31], fields[32] = hhmmss, str(round(price - prev, 2)), str(pct)
    fields[33], fields[34], fields[37], fields[38] = str(price + 0.1), str(price - 0.2), amount_wan, "0.81"
    fields[57] = amount_wan
    return f'v_{prefixed}="{"~".join(fields)}";\n'


def _sina_line(prefixed, name, price, prev, date_str="2026-11-13", time_str="15:00:00",
               amount="1966000000"):
    fields = [""] * 33
    fields[0], fields[1], fields[2], fields[3] = name, str(price - 0.1), str(prev), str(price)
    fields[4], fields[5], fields[8], fields[9] = str(price + 0.1), str(price - 0.2), "12345600", amount
    fields[30], fields[31] = date_str, time_str
    return f'var hq_str_{prefixed}="{",".join(fields)}";\n'


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_tencent_converts_units_and_time():
    raw = _tencent_line("sh600919", "江苏银行", "600919", 13.23, 12.94, 2.24)
    row = qs.parse_tencent(raw)["sh600919"]
    assert row["name"] == "江苏银行" and row["price"] == pytest.approx(13.23)
    assert row["volume"] == pytest.approx(12345600)  # 手 -> 股
    assert row["amount"] == pytest.approx(196600 * 10000)  # 万 -> 元
    assert row["quote_time"] == datetime(2026, 11, 13, 15, 0, 3, tzinfo=SH)


def test_parse_sina_computes_pct_and_time():
    raw = _sina_line("sh600919", "江苏银行", 13.23, 12.94)
    row = qs.parse_sina(raw)["sh600919"]
    assert row["pct_change"] == pytest.approx((13.23 - 12.94) / 12.94 * 100)
    assert row["quote_time"] == datetime(2026, 11, 13, 15, 0, 0, tzinfo=SH)
    assert row["amount"] == pytest.approx(1966000000)


def test_parse_skips_empty_and_invalid_lines():
    assert qs.parse_sina('var hq_str_sh999999="";\n') == {}
    assert qs.parse_tencent('v_pv_none_match="1";\n') == {}
    assert qs.parse_tencent("") == {} and qs.parse_sina("") == {}


# --------------------------------------------------------------------------- #
# verify_pair
# --------------------------------------------------------------------------- #
def _pair(t_price=13.23, s_price=13.23, t_time="20261113150003", s_time=("2026-11-13", "15:00:00"),
          t_name="江苏银行", s_name="江苏银行"):
    t = qs.parse_tencent(_tencent_line("sh600919", t_name, "600919", t_price, 12.94, 2.24, t_time))["sh600919"]
    s = qs.parse_sina(_sina_line("sh600919", s_name, s_price, 12.94, *s_time))["sh600919"]
    return t, s


def test_verify_passes_when_all_conditions_hold():
    t, s = _pair()
    result = qs.verify_pair(t, s)
    assert result["verified"] is True and result["reasons"] == []
    assert result["time_diff_seconds"] == 3.0
    assert result["price_diff_pct"] == 0.0


def test_verify_fails_on_time_gap():
    t, s = _pair(s_time=("2026-11-13", "14:55:00"))
    result = qs.verify_pair(t, s)
    assert result["verified"] is False
    assert any("相差" in r for r in result["reasons"])


def test_verify_post_close_ignores_time_gap():
    # 收盘后腾讯时间戳可以晚到16:14，新浪固定15:00:00：不应因时间差判失败
    t, s = _pair(t_time="20261113161400")
    result = qs.verify_pair(t, s)
    assert result["verified"] is True, result["reasons"]
    assert result["time_rule"] == "post_close"
    assert result["time_diff_seconds"] == pytest.approx(4440.0)


def test_verify_post_close_still_checks_price_and_date():
    t, s = _pair(t_time="20261113161400", s_price=13.30)
    assert qs.verify_pair(t, s)["verified"] is False
    t, s = _pair(t_time="20261113161400", s_time=("2026-11-12", "15:00:00"))
    result = qs.verify_pair(t, s)
    assert result["verified"] is False and any("日期" in r for r in result["reasons"])


def test_verify_in_session_time_gap_still_fails():
    t, s = _pair(t_time="20261113103000", s_time=("2026-11-13", "10:20:00"))
    result = qs.verify_pair(t, s)
    assert result["verified"] is False and result["time_rule"] is None
    assert any("相差" in r for r in result["reasons"])


def test_verify_fails_on_price_deviation():
    t, s = _pair(s_price=13.30)  # 0.53%
    result = qs.verify_pair(t, s)
    assert result["verified"] is False
    assert any("价格偏差" in r for r in result["reasons"])
    # 0.2% 偏差仍通过
    t2, s2 = _pair(t_price=13.00, s_price=13.026)
    assert qs.verify_pair(t2, s2)["verified"] is True


def test_verify_fails_on_date_and_name_mismatch():
    t, s = _pair(s_time=("2026-11-12", "15:00:00"))
    assert any("日期" in r for r in qs.verify_pair(t, s)["reasons"])
    t, s = _pair(s_name="南京银行")
    assert any("名称" in r for r in qs.verify_pair(t, s)["reasons"])


def test_verify_single_or_no_source():
    t, _ = _pair()
    assert qs.verify_pair(t, None)["verified"] is False
    assert qs.verify_pair(None, None)["reasons"] == ["两个来源均无数据"]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def _call(tool, *args):
    return guard(tool.__wrapped__.__wrapped__)(*args)


def _patch_fetch(monkeypatch, tencent_raw, sina_raw):
    monkeypatch.setattr(qs, "fetch_tencent", lambda p: qs.parse_tencent(tencent_raw))
    monkeypatch.setattr(qs, "fetch_sina", lambda p: qs.parse_sina(sina_raw))


def test_get_verified_quote_end_to_end(monkeypatch):
    tencent = _tencent_line("sh600919", "江苏银行", "600919", 13.23, 12.94, 2.24) + _tencent_line(
        "sh601009", "南京银行", "601009", 12.81, 12.57, 1.91)
    sina = _sina_line("sh600919", "江苏银行", 13.23, 12.94) + _sina_line(
        "sh601009", "南京银行", 12.81, 12.57)
    _patch_fetch(monkeypatch, tencent, sina)
    result = _call(quote_verify.get_verified_quote, ["600919", "601009"])
    assert result["ok"] is True and result["count"] == 2 and result["verified_count"] == 2
    row = result["data"][0]
    assert row["code"] == "600919" and row["verified"] is True
    assert row["quote_time"].startswith("2026-11-13T15:00:03")
    assert row["limit_up_like"] is False
    assert row["sources"]["tencent"]["price"] == row["sources"]["sina"]["price"]


def test_get_verified_quote_flags_limit_up_and_st(monkeypatch):
    tencent = _tencent_line("sh603103", "横店影视", "603103", 19.31, 17.55, 10.03)
    sina = _sina_line("sh603103", "横店影视", 19.31, 17.55)
    _patch_fetch(monkeypatch, tencent, sina)
    row = _call(quote_verify.get_verified_quote, ["603103"])["data"][0]
    assert row["limit_up_like"] is True


def test_get_verified_quote_one_source_down_is_unverified_not_error(monkeypatch):
    tencent = _tencent_line("sh600919", "江苏银行", "600919", 13.23, 12.94, 2.24)

    def sina_down(p):
        raise qs.requests.ConnectionError("sina blocked")

    monkeypatch.setattr(qs, "fetch_tencent", lambda p: qs.parse_tencent(tencent))
    monkeypatch.setattr(qs, "fetch_sina", sina_down)
    result = _call(quote_verify.get_verified_quote, ["600919"])
    assert result["ok"] is True and result["verified_count"] == 0
    assert result["data"][0]["verified"] is False
    assert "sina" in result["source_errors"]


def test_get_verified_quote_both_down_errors_and_never_serves_stale(monkeypatch):
    tencent = _tencent_line("sh600919", "江苏银行", "600919", 13.23, 12.94, 2.24)
    sina = _sina_line("sh600919", "江苏银行", 13.23, 12.94)
    _patch_fetch(monkeypatch, tencent, sina)
    assert _call(quote_verify.get_verified_quote, ["600919"])["ok"] is True

    def down(p):
        raise qs.requests.ConnectionError("down")

    monkeypatch.setattr(qs, "fetch_tencent", down)
    monkeypatch.setattr(qs, "fetch_sina", down)
    result = _call(quote_verify.get_verified_quote, ["600919"])
    assert result["ok"] is False and result["error"] == "data_source_unavailable"
    assert "stale" not in result


def test_get_verified_quote_rejects_empty_list():
    assert _call(quote_verify.get_verified_quote, [])["error"] == "bad_request"


def test_get_index_quotes_with_amount_cross_check(monkeypatch):
    tencent = "".join(
        _tencent_line(p, label, p[2:], 3634.35 + i, 3656.0, -0.59, amount_wan="80000000")
        for i, (p, label) in enumerate(quote_verify.INDEX_MAP)
    )
    sina = "".join(
        _sina_line(p, label, 3634.35 + i, 3656.0, amount="800000000000")
        for i, (p, label) in enumerate(quote_verify.INDEX_MAP)
    )
    _patch_fetch(monkeypatch, tencent, sina)
    result = _call(quote_verify.get_index_quotes)
    assert result["ok"] is True and result["count"] == 5 and result["verified_count"] == 5
    assert result["two_market_amount_approx"] == pytest.approx(2 * 800000000000)
    assert [r["label"] for r in result["data"]][:2] == ["上证指数", "深证成指"]


def test_get_index_quotes_drops_amount_when_sources_disagree(monkeypatch):
    tencent = "".join(
        _tencent_line(p, label, p[2:], 3600.0, 3600.0, 0.0, amount_wan="80000000")
        for p, label in quote_verify.INDEX_MAP
    )
    sina = "".join(
        _sina_line(p, label, 3600.0, 3600.0, amount="100000000000")
        for p, label in quote_verify.INDEX_MAP
    )
    _patch_fetch(monkeypatch, tencent, sina)
    assert _call(quote_verify.get_index_quotes)["two_market_amount_approx"] is None


def test_realtime_tools_are_not_snapshotted():
    assert "get_verified_quote" in snapshot.NO_SNAPSHOT_TOOLS
    assert "get_index_quotes" in snapshot.NO_SNAPSHOT_TOOLS


# --------------------------------------------------------------------------- #
# Session-aware cache TTLs (Beijing time)
# --------------------------------------------------------------------------- #
def _at(monkeypatch, y, m, d, hh, mm):
    fixed = datetime(y, m, d, hh, mm, tzinfo=SH)
    monkeypatch.setattr(cache, "now_cn", lambda: fixed)
    monkeypatch.setattr(cache, "_is_trade_day_cn", lambda day: day.weekday() < 5)
    return fixed


@pytest.mark.parametrize(
    "hh,mm,expected",
    [(9, 10, False), (9, 15, True), (10, 30, True), (11, 35, True), (11, 36, False),
     (12, 30, False), (13, 0, True), (14, 59, True), (15, 5, True), (15, 6, False)],
)
def test_in_trading_session_windows(monkeypatch, hh, mm, expected):
    fixed = _at(monkeypatch, 2026, 11, 16, hh, mm)  # 周一
    assert cache.in_trading_session(fixed) is expected


def test_no_session_on_weekend_or_holiday(monkeypatch):
    fixed = _at(monkeypatch, 2026, 11, 14, 10, 30)  # 周六
    assert cache.in_trading_session(fixed) is False
    monkeypatch.setattr(cache, "_is_trade_day_cn", lambda day: False)
    assert cache.in_trading_session(datetime(2026, 11, 16, 10, 30, tzinfo=SH)) is False


def test_intraday_ttls_are_short_during_session(monkeypatch):
    _at(monkeypatch, 2026, 11, 16, 10, 30)
    assert cache.ttl_intraday_official() == cache.TTL_INTRADAY_FLOW == 180
    assert cache.ttl_session_aware_daily() == cache.TTL_INTRADAY_KLINE == 60
    today = "2026-11-16"
    assert cache.ttl_daily_kline({"end_date": today}) == 60
    assert cache.ttl_daily_kline({"end_date": "2026-11-13"}) is None  # 历史K线永久


def test_off_hours_ttl_uses_beijing_clock(monkeypatch):
    _at(monkeypatch, 2026, 11, 16, 16, 0)  # 收盘后，18点前
    assert cache.ttl_intraday_official() == 1800
    _at(monkeypatch, 2026, 11, 16, 20, 0)  # 晚上 -> 到次日 09:00
    assert cache.ttl_intraday_official() == 13 * 3600
    _at(monkeypatch, 2026, 11, 16, 3, 0)  # 凌晨，在 18 点前的规则下按 30 分钟
    assert cache.ttl_intraday_official() == 1800


# --------------------------------------------------------------------------- #
# Stock pack partial-bar flag
# --------------------------------------------------------------------------- #
def _bars(n, end):
    from datetime import timedelta
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    return [
        {"date": day.strftime("%Y-%m-%d"), "open": 10 + i * 0.05, "high": 10.2 + i * 0.05,
         "low": 9.9 + i * 0.05, "close": 10.1 + i * 0.05, "volume": 1000, "amount": 1e7,
         "pct_change": 0.4, "turnover_rate": 1.0}
        for i, day in enumerate(days)
    ]


def _pack_with_session(monkeypatch, in_session, today=date(2026, 11, 16)):
    monkeypatch.setattr(stock_pack.dates, "latest_trade_date", lambda ref=None: today)
    monkeypatch.setattr(stock_pack, "now_cn", lambda: datetime(2026, 11, 16, 10, 30, tzinfo=SH))
    monkeypatch.setattr(stock_pack, "in_trading_session", lambda: in_session)
    monkeypatch.setattr(stock_pack.ak, "daily_hist", lambda *a, **k: _bars(130, today))
    return guard(stock_pack.get_stock_pack.__wrapped__.__wrapped__)("600919", 120)


def test_stock_pack_marks_partial_bar_during_session(monkeypatch):
    result = _pack_with_session(monkeypatch, in_session=True)
    assert result["market_session"] == "trading"
    assert result["last_bar_partial"] is True
    assert "未收盘" in result["intraday_note"]


def test_stock_pack_not_partial_after_close(monkeypatch):
    result = _pack_with_session(monkeypatch, in_session=False)
    assert result["market_session"] == "closed"
    assert result["last_bar_partial"] is False and result["intraday_note"] is None
