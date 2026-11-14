"""Offline tests: snapshot fallback in guard(), Redis path, THS fund-flow normalisation,
and the sector fund-flow tool's EM -> THS -> stale-snapshot chain. No network needed."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from ashare_mcp import snapshot
from ashare_mcp.config import settings
from ashare_mcp.data_sources import ths_flow
from ashare_mcp.data_sources._base import DataUnavailableError, EmptyDataError
from ashare_mcp.tools._helpers import guard, ok


@pytest.fixture(autouse=True)
def _clean_snapshot_state(monkeypatch):
    snapshot._memory.clear()
    snapshot._last_saved.clear()
    monkeypatch.setattr(snapshot, "_client", None)
    monkeypatch.setattr(snapshot, "_client_failed_at", 0.0)
    monkeypatch.setattr(settings, "redis_url", "")
    yield
    snapshot._memory.clear()
    snapshot._last_saved.clear()


# --------------------------------------------------------------------------- #
# guard + snapshot
# --------------------------------------------------------------------------- #
def _make_tool(name: str, behaviour: dict):
    @guard
    def tool(symbol: str = "600519") -> dict:
        exc = behaviour.get("raise")
        if exc is not None:
            raise exc
        return ok([{"symbol": symbol, "v": 1}], symbol=symbol)

    tool.__name__ = name
    tool.__wrapped__.__name__ = name
    return tool


def test_failure_serves_stale_snapshot_with_labels():
    behaviour: dict = {}

    @guard
    def get_demo_flow(symbol: str = "600519") -> dict:
        if behaviour.get("raise"):
            raise behaviour["raise"]
        return ok([{"symbol": symbol}], latest_data_date="2026-11-13", age_calendar_days=1,
                  freshness="fresh")

    first = get_demo_flow("600519")
    assert first["ok"] is True and "stale" not in first

    behaviour["raise"] = DataUnavailableError("eastmoney 502")
    second = get_demo_flow("600519")
    assert second["ok"] is True
    assert second["stale"] is True
    assert second["freshness"] == "stale_snapshot"
    assert second["snapshot_saved_at"]
    assert second["snapshot_age_hours"] is not None
    assert "age_calendar_days" not in second
    assert "eastmoney 502" in second["stale_reason"]
    assert second["data"] == [{"symbol": "600519"}]
    assert second["latest_data_date"] == "2026-11-13"


def test_snapshot_is_per_arguments():
    behaviour: dict = {}

    @guard
    def get_demo_by_symbol(symbol: str = "600519") -> dict:
        if behaviour.get("raise"):
            raise behaviour["raise"]
        return ok([{"symbol": symbol}])

    get_demo_by_symbol("600519")
    behaviour["raise"] = DataUnavailableError("down")
    other = get_demo_by_symbol("000001")
    assert other["ok"] is False and other["error"] == "data_source_unavailable"


def test_empty_data_never_falls_back_to_snapshot():
    behaviour: dict = {}

    @guard
    def get_demo_empty(symbol: str = "600519") -> dict:
        if behaviour.get("raise"):
            raise behaviour["raise"]
        return ok([{"symbol": symbol}])

    get_demo_empty("600519")
    behaviour["raise"] = EmptyDataError("no rows for that date")
    result = get_demo_empty("600519")
    assert result["ok"] is False and result["error"] == "no_data"


def test_realtime_tools_are_never_snapshotted():
    behaviour: dict = {}

    @guard
    def get_realtime_quote(symbol: str = "600519") -> dict:
        if behaviour.get("raise"):
            raise behaviour["raise"]
        return ok([{"price": 10.0}])

    get_realtime_quote("600519")
    behaviour["raise"] = DataUnavailableError("down")
    result = get_realtime_quote("600519")
    assert result["ok"] is False and result["error"] == "data_source_unavailable"


def test_error_results_are_not_saved():
    @guard
    def get_demo_error(symbol: str = "x") -> dict:
        return {"ok": False, "error": "stale_data", "detail": "old"}

    get_demo_error("x")
    assert not snapshot._memory


# --------------------------------------------------------------------------- #
# Redis path
# --------------------------------------------------------------------------- #
def test_redis_roundtrip_survives_memory_loss(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings, "redis_url", "rediss://example:6379")
    monkeypatch.setattr(snapshot, "_client", client)

    snapshot.save("get_demo", {"a": 1}, ok([{"x": 1}]))
    snapshot._memory.clear()  # simulate a Render restart
    loaded = snapshot.load("get_demo", {"a": 1})
    assert loaded is not None
    assert loaded["result"]["data"] == [{"x": 1}]
    keys = client.keys("ashare:snap:v1:*")
    assert len(keys) == 1
    assert 0 < client.ttl(keys[0]) <= snapshot.SNAPSHOT_TTL_SECONDS


def test_redis_failure_never_raises_and_falls_back_to_memory(monkeypatch):
    class BrokenRedis:
        def set(self, *a, **k):
            raise ConnectionError("boom")

        def get(self, *a, **k):
            raise ConnectionError("boom")

    monkeypatch.setattr(settings, "redis_url", "rediss://example:6379")
    monkeypatch.setattr(snapshot, "_client", BrokenRedis())

    snapshot.save("get_demo", {"a": 1}, ok([{"x": 1}]))
    loaded = snapshot.load("get_demo", {"a": 1})
    assert loaded is not None  # served from the in-memory copy


def test_oversized_payload_is_not_saved():
    big = ok([{"blob": "x" * 900_000}])
    snapshot.save("get_demo_big", {}, big)
    assert snapshot.load("get_demo_big", {}) is None


# --------------------------------------------------------------------------- #
# THS normalisation
# --------------------------------------------------------------------------- #
def test_to_yuan_variants():
    assert ths_flow.to_yuan(1.5) == pytest.approx(1.5e8)
    assert ths_flow.to_yuan("2.3亿") == pytest.approx(2.3e8)
    assert ths_flow.to_yuan("5000万") == pytest.approx(5e7)
    assert ths_flow.to_yuan("-1.2亿") == pytest.approx(-1.2e8)
    assert ths_flow.to_yuan("12.5") == pytest.approx(12.5e8)
    assert ths_flow.to_yuan("abc") is None
    assert ths_flow.to_yuan(None) is None
    assert ths_flow.to_yuan(math.nan) is None
    assert ths_flow.to_yuan("--") is None


def test_to_pct_variants():
    assert ths_flow.to_pct("3.21%") == pytest.approx(3.21)
    assert ths_flow.to_pct(-1.5) == pytest.approx(-1.5)
    assert ths_flow.to_pct("n/a") is None


def _realtime_df():
    return pd.DataFrame(
        [
            {"序号": 1, "行业": "银行", "行业指数": 1500.5, "行业-涨跌幅": 1.43, "流入资金": 30.2,
             "流出资金": 25.0, "净额": 5.2, "公司家数": 42, "领涨股": "江苏银行",
             "领涨股-涨跌幅": 2.24, "当前价": 13.23},
            {"序号": 2, "行业": "元件", "行业指数": 900.1, "行业-涨跌幅": -6.29, "流入资金": 10.0,
             "流出资金": 40.5, "净额": -30.5, "公司家数": 60, "领涨股": "某股",
             "领涨股-涨跌幅": 0.5, "当前价": 20.0},
            {"序号": 3, "行业": "影视院线", "行业指数": 700.0, "行业-涨跌幅": 4.37, "流入资金": 20.1,
             "流出资金": 6.2, "净额": 13.9, "公司家数": 15, "领涨股": "横店影视",
             "领涨股-涨跌幅": 10.03, "当前价": 19.31},
        ]
    )


def test_normalize_realtime_sorted_by_net_inflow():
    rows = ths_flow.normalize_rows(_realtime_df().to_dict(orient="records"), realtime=True)
    assert [r["name"] for r in rows] == ["影视院线", "银行", "元件"]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["net_inflow"] == pytest.approx(13.9e8)
    assert rows[0]["pct_change"] == pytest.approx(4.37)
    assert rows[0]["leader_name"] == "横店影视"
    assert all(r["basis"] == "ths" for r in rows)


def test_normalize_period_rows_use_stage_change():
    df = pd.DataFrame(
        [{"序号": 1, "行业": "银行", "公司家数": 42, "行业指数": 1500.5, "阶段涨跌幅": "5.2%",
          "流入资金": 100.0, "流出资金": 80.0, "净额": 20.0}]
    )
    rows = ths_flow.normalize_rows(df.to_dict(orient="records"), realtime=False)
    assert rows[0]["pct_change"] == pytest.approx(5.2)
    assert "leader_name" not in rows[0]


def test_sector_fund_flow_unsupported_args():
    with pytest.raises(DataUnavailableError):
        ths_flow.sector_fund_flow("30日", "行业资金流")
    with pytest.raises(DataUnavailableError):
        ths_flow.sector_fund_flow("今日", "地域资金流")


def test_sector_fund_flow_uses_matching_akshare_function(monkeypatch):
    calls = []

    def fake_call_ak(func, **kwargs):
        calls.append((func, kwargs))
        return _realtime_df()

    monkeypatch.setattr(ths_flow, "call_ak", fake_call_ak)
    rows = ths_flow.sector_fund_flow("今日", "概念资金流", limit=2)
    assert calls == [("stock_fund_flow_concept", {"symbol": "即时"})]
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# Tool-level chain: EM -> THS -> stale snapshot
# --------------------------------------------------------------------------- #
def test_sector_tool_falls_back_to_ths_then_stale(monkeypatch):
    from ashare_mcp.tools import fundflow

    def em_down(*a, **k):
        raise DataUnavailableError("eastmoney 502")

    monkeypatch.setattr(fundflow.ak, "sector_fund_flow_rank", em_down)
    monkeypatch.setattr(
        fundflow.ths_flow,
        "sector_fund_flow",
        lambda period, sector_type, limit=None: ths_flow.normalize_rows(
            _realtime_df().to_dict(orient="records"), realtime=True
        ),
    )

    tool = fundflow.get_sector_fund_flow_rank.__wrapped__.__wrapped__  # unwrap cached+guard
    plain = guard(tool)

    # EM down, THS up -> fallback response
    result = plain("今日", "行业资金流", 50)
    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert "同花顺" in result["source"]
    assert result["data"][0]["name"] == "影视院线"
    assert "stale" not in result

    # Both down -> the last good (fallback) result is served as stale
    def ths_down(*a, **k):
        raise DataUnavailableError("ths blocked")

    monkeypatch.setattr(fundflow.ths_flow, "sector_fund_flow", ths_down)
    stale = plain("今日", "行业资金流", 50)
    assert stale["ok"] is True and stale["stale"] is True
    assert stale["data"][0]["name"] == "影视院线"

    # A never-seen argument combination has no snapshot -> graceful error
    other = plain("10日", "概念资金流", 50)
    assert other["ok"] is False and other["error"] == "data_source_unavailable"
    assert "东方财富失败" in other["detail"] and "同花顺备用通道失败" in other["detail"]
