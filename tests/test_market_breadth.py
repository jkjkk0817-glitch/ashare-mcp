"""Offline tests for get_market_breadth parsing and flags."""

from __future__ import annotations

import pandas as pd
import pytest

from ashare_mcp import snapshot
from ashare_mcp.config import settings
from ashare_mcp.data_sources._base import DataUnavailableError, EmptyDataError
from ashare_mcp.tools import market_breadth
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


def _items(up=1118, down=4172, limit_down=68, real_limit_down=64, st_limit_down=4):
    return {
        "上涨": str(up), "涨停": "33", "真实涨停": "30", "st st*涨停": "3",
        "下跌": str(down), "跌停": str(limit_down), "真实跌停": str(real_limit_down),
        "st st*跌停": str(st_limit_down), "平盘": "120", "停牌": "9",
        "活跃度": "20.5%", "统计日期": "2026-11-16 15:00:00",
    }


def test_parse_breadth_extreme_day():
    data = market_breadth.parse_breadth(_items())
    assert data["up_count"] == 1118 and data["down_count"] == 4172
    assert data["up_ratio_pct"] == pytest.approx(1118 / (1118 + 4172) * 100, abs=0.05)
    assert data["non_st_limit_down"] == 64 and "真实跌停" in data["non_st_limit_down_basis"]
    assert data["emotion_extreme_hit"] is True
    assert data["breadth_weak_hit"] is False  # 21.1% > 15%
    assert data["stat_time"] == "2026-11-16 15:00:00"


def test_breadth_weak_hit_only_when_ratio_at_or_below_15():
    weak = market_breadth.parse_breadth(_items(up=700, down=4300))
    assert weak["up_ratio_pct"] <= 15 and weak["breadth_weak_hit"] is True
    edge = market_breadth.parse_breadth(_items(up=15, down=85))
    assert edge["up_ratio_pct"] == pytest.approx(15.0) and edge["breadth_weak_hit"] is True
    ok = market_breadth.parse_breadth(_items(up=16, down=84))
    assert ok["breadth_weak_hit"] is False


def test_emotion_threshold_uses_non_st_count():
    below = market_breadth.parse_breadth(_items(limit_down=45, real_limit_down=29, st_limit_down=16))
    assert below["non_st_limit_down"] == 29 and below["emotion_extreme_hit"] is False
    at = market_breadth.parse_breadth(_items(limit_down=45, real_limit_down=30, st_limit_down=15))
    assert at["emotion_extreme_hit"] is True


def test_non_st_falls_back_to_total_minus_st_then_total():
    items = _items()
    items.pop("真实跌停")
    sub = market_breadth.parse_breadth(items)
    assert sub["non_st_limit_down"] == 64 and "减ST" in sub["non_st_limit_down_basis"]
    items.pop("st st*跌停")
    total = market_breadth.parse_breadth(items)
    assert total["non_st_limit_down"] == 68 and "可能含ST" in total["non_st_limit_down_basis"]


def test_missing_up_down_raises():
    with pytest.raises(EmptyDataError):
        market_breadth.parse_breadth({"涨停": "30"})


def _tool():
    return guard(market_breadth.get_market_breadth.__wrapped__.__wrapped__)


def test_tool_end_to_end(monkeypatch):
    frame = pd.DataFrame({"item": list(_items()), "value": list(_items().values())})
    monkeypatch.setattr(market_breadth, "call_ak", lambda name, **k: frame)
    result = _tool()()
    assert result["ok"] is True and result["data"]["emotion_extreme_hit"] is True
    assert result["thresholds"]["non_st_limit_down"] == 30
    assert result["source"].endswith("乐咕乐股")


def test_tool_error_and_never_snapshotted(monkeypatch):
    frame = pd.DataFrame({"item": list(_items()), "value": list(_items().values())})
    monkeypatch.setattr(market_breadth, "call_ak", lambda name, **k: frame)
    assert _tool()()["ok"] is True

    def down(name, **k):
        raise DataUnavailableError("legu down")

    monkeypatch.setattr(market_breadth, "call_ak", down)
    result = _tool()()
    assert result["ok"] is False and result["error"] == "data_source_unavailable"
    assert "stale" not in result
    assert "get_market_breadth" in snapshot.NO_SNAPSHOT_TOOLS
