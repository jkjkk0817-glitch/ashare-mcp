from __future__ import annotations

from datetime import date

import pytest

from ashare_mcp.utils.freshness import (
    MARKET_FLOW_MAX_AGE_DAYS,
    data_age_days,
    latest_record_date,
    parse_record_date,
)


def test_parse_record_date_supported_formats():
    assert parse_record_date("2026-11-12") == date(2026, 11, 12)
    assert parse_record_date("20261112") == date(2026, 11, 12)
    assert parse_record_date("2026/11/12") == date(2026, 11, 12)
    assert parse_record_date("not-a-date") is None


def test_latest_record_date_uses_latest_valid_row():
    rows = [
        {"date": "2026-11-10"},
        {"date": "2026-11-12"},
        {"date": "bad"},
        {"trade_date": "20261111"},
    ]
    assert latest_record_date(rows) == date(2026, 11, 12)


def test_market_flow_age_threshold():
    latest = date(2026, 7, 1)
    assert data_age_days(latest, today=date(2026, 11, 12)) > MARKET_FLOW_MAX_AGE_DAYS
    assert data_age_days(date(2026, 11, 10), today=date(2026, 11, 12)) == 2


def test_tushare_market_flow_maps_documented_fields(monkeypatch):
    import requests

    from ashare_mcp.data_sources import tushare_src
    from ashare_mcp.config import settings

    monkeypatch.setattr(settings, "tushare_token", "test-token")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 0,
                "data": {
                    "fields": [
                        "trade_date", "close_sh", "pct_change_sh", "close_sz",
                        "pct_change_sz", "net_amount", "net_amount_rate",
                    ],
                    "items": [["20261112", 4000.0, 0.5, 13000.0, 0.8, 123456.0, 1.2]],
                },
            }

    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    rows = tushare_src.market_fund_flow()

    assert captured["url"] == "https://api.tushare.pro"
    assert captured["json"]["api_name"] == "moneyflow_mkt_dc"
    assert "test-token" == captured["json"]["token"]
    assert rows[0]["date"] == "2026-11-12"
    assert rows[0]["main_net_inflow"] == 123456.0
    assert rows[0]["main_net_inflow_pct"] == 1.2


def test_tushare_market_flow_requires_existing_token(monkeypatch):
    from ashare_mcp.data_sources import tushare_src
    from ashare_mcp.config import settings
    from ashare_mcp.data_sources._base import DataUnavailableError

    monkeypatch.setattr(settings, "tushare_token", "")
    with pytest.raises(DataUnavailableError, match="TUSHARE_TOKEN not configured"):
        tushare_src.market_fund_flow()


def test_stale_primary_uses_tushare_fallback(monkeypatch):
    from ashare_mcp.data_sources import akshare_src, tushare_src

    monkeypatch.setattr(akshare_src, "call_ak", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        akshare_src,
        "df_to_records",
        lambda _df: [{"date": "2026-07-01", "main_net_inflow": 1}],
    )
    monkeypatch.setattr(
        tushare_src,
        "market_fund_flow",
        lambda: [{"date": date.today().isoformat(), "main_net_inflow": 2}],
    )

    rows, source = akshare_src.market_fund_flow_with_source()
    assert rows[0]["main_net_inflow"] == 2
    assert source.startswith("Tushare Pro")
