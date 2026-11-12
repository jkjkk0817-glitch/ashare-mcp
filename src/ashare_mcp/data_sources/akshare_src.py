
"""Thin akshare wrappers. Each returns JSON-friendly dict / list[dict] (no DataFrames).

Function-name candidate lists make these resilient to akshare's interface renames.
Code/date normalization happens here so the tool layer can pass user-friendly inputs.
"""

from __future__ import annotations

import requests
from datetime import datetime, timezone
from loguru import logger

from ._base import (
    DataUnavailableError,
    EmptyDataError,
    df_kv_to_dict,
    df_to_records,
)
from ._retry import call_ak
from ..utils import codes, dates
from ..utils.freshness import latest_record_date, data_age_days, MARKET_FLOW_MAX_AGE_DAYS


# ---- per-call rename overrides ----
_BID_ASK_RENAME = {
    "买一": "bid_1", "买二": "bid_2", "买三": "bid_3",
    "买四": "bid_4", "买五": "bid_5",
    "卖一": "ask_1", "卖二": "ask_2", "卖三": "ask_3",
    "卖四": "ask_4", "卖五": "ask_5",
    "买1": "bid_1", "买2": "bid_2", "买3": "bid_3",
    "买4": "bid_4", "买5": "bid_5",
    "卖1": "ask_1", "卖2": "ask_2", "卖3": "ask_3",
    "卖4": "ask_4", "卖5": "ask_5",
    "buy_1": "bid_1", "buy_2": "bid_2", "buy_3": "bid_3",
    "buy_4": "bid_4", "buy_5": "bid_5",
    "sell_1": "ask_1", "sell_2": "ask_2", "sell_3": "ask_3",
    "sell_4": "ask_4", "sell_5": "ask_5",
    "最新": "price",
    "最新价": "price",
    "涨跌": "change",
    "涨跌额": "change",
    "涨幅": "pct_change",
    "涨跌幅": "pct_change",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "昨收": "prev_close",
    "成交量": "volume",
    "成交额": "amount",
    "换手率": "turnover_rate",
    "量比": "volume_ratio",
    "市盈率": "pe_ttm",
    "市净率": "pb",
    "总市值": "total_market_cap",
    "流通市值": "float_market_cap",
}


# --------------------------------------------------------------------------- #
# A. Market
# --------------------------------------------------------------------------- #

def _as_float(value):
    try:
        if value is None or str(value).strip() in {"", "-"}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _alternate_quote(symbol: str) -> dict:
    """Try Tencent first, then Sina. Never return a partial quote without a price."""
    code = codes.normalize(symbol)
    prefixed = codes.with_prefix(code)
    errors = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://finance.sina.com.cn/",
        "Accept": "*/*",
    }

    # Tencent text API: volume is in lots; amount is in 10,000 CNY.
    try:
        response = requests.get(
            f"https://qt.gtimg.cn/q={prefixed}",
            headers=headers,
            timeout=(4, 8),
        )
        response.raise_for_status()
        response.encoding = "gbk"
        raw = response.text
        payload = raw.split('="', 1)[1].split('"', 1)[0] if '="' in raw else ""
        fields = payload.split("~")
        if len(fields) >= 35 and fields[1].strip():
            price = _as_float(fields[3])
            prev_close = _as_float(fields[4])
            if price is not None and price > 0:
                result = {
                    "code": code,
                    "name": fields[1],
                    "price": price,
                    "prev_close": prev_close,
                    "open": _as_float(fields[5]),
                    "volume": (_as_float(fields[6]) or 0) * 100,
                    "amount": (
                        (_as_float(fields[57]) if len(fields) > 57 else _as_float(fields[37])) or 0
                    ) * 10000 if len(fields) > 37 else None,
                    "change": _as_float(fields[31]),
                    "pct_change": _as_float(fields[32]),
                    "high": _as_float(fields[33]),
                    "low": _as_float(fields[34]),
                    "turnover_rate": _as_float(fields[38]) if len(fields) > 38 else None,
                    "source": "腾讯财经实时行情",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
                # Tencent order: buy 1-5 occupy fields 9-18; sell 1-5 occupy fields 19-28.
                # Prices are at the even index of each pair; quantities are lots (手).
                for level in range(1, 6):
                    bid_idx = 9 + (level - 1) * 2
                    ask_idx = 19 + (level - 1) * 2
                    result[f"bid_{level}"] = _as_float(fields[bid_idx]) if len(fields) > bid_idx else None
                    result[f"bid_{level}_vol"] = (_as_float(fields[bid_idx + 1]) or 0) * 100 if len(fields) > bid_idx + 1 else None
                    result[f"ask_{level}"] = _as_float(fields[ask_idx]) if len(fields) > ask_idx else None
                    result[f"ask_{level}_vol"] = (_as_float(fields[ask_idx + 1]) or 0) * 100 if len(fields) > ask_idx + 1 else None
                if len(fields) > 30 and fields[30].strip():
                    result["quote_time"] = fields[30].strip()
                return result
        errors.append("腾讯接口返回空数据或字段格式异常")
    except (requests.RequestException, IndexError, ValueError) as exc:
        errors.append(f"腾讯接口: {exc}")

    # Sina text API: volume and five-level quantities are already in shares.
    try:
        response = requests.get(
            f"https://hq.sinajs.cn/list={prefixed}",
            headers=headers,
            timeout=(4, 8),
        )
        response.raise_for_status()
        response.encoding = "gbk"
        raw = response.text
        payload = raw.split('="', 1)[1].split('"', 1)[0] if '="' in raw else ""
        fields = payload.split(",")
        if len(fields) >= 32 and fields[0].strip():
            price = _as_float(fields[3])
            prev_close = _as_float(fields[2])
            if price is not None and price > 0:
                change = price - prev_close if prev_close else None
                pct = (change / prev_close * 100) if change is not None and prev_close else None
                result = {
                    "code": code,
                    "name": fields[0],
                    "open": _as_float(fields[1]),
                    "prev_close": prev_close,
                    "price": price,
                    "high": _as_float(fields[4]),
                    "low": _as_float(fields[5]),
                    "volume": _as_float(fields[8]),
                    "amount": _as_float(fields[9]),
                    "change": change,
                    "pct_change": pct,
                    "source": "新浪财经实时行情",
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "quote_time": f"{fields[30]} {fields[31]}".strip(),
                }
                for level in range(1, 6):
                    bid_vol_idx = 10 + (level - 1) * 2
                    bid_price_idx = bid_vol_idx + 1
                    ask_vol_idx = 20 + (level - 1) * 2
                    ask_price_idx = ask_vol_idx + 1
                    result[f"bid_{level}"] = _as_float(fields[bid_price_idx])
                    result[f"bid_{level}_vol"] = _as_float(fields[bid_vol_idx])
                    result[f"ask_{level}"] = _as_float(fields[ask_price_idx])
                    result[f"ask_{level}_vol"] = _as_float(fields[ask_vol_idx])
                return result
        errors.append("新浪接口返回空数据或字段格式异常")
    except (requests.RequestException, IndexError, ValueError) as exc:
        errors.append(f"新浪接口: {exc}")

    raise DataUnavailableError(
        f"{code} 实时行情备用数据源均不可用: " + "；".join(errors)
    )



def spot_lookup(
    symbols: list[str],
    prefer_alternate: bool = False,
) -> list[dict]:
    """Look up selected stocks; optionally skip Eastmoney's full-market snapshot."""
    wanted = {codes.normalize(s) for s in symbols}

    if prefer_alternate:
        rows = []
        failures = []
        for code in wanted:
            try:
                rows.append(_alternate_quote(code))
            except DataUnavailableError as exc:
                failures.append(str(exc))
        if rows:
            return rows
        raise DataUnavailableError(
            "腾讯和新浪均未能提供个股行情: " + "；".join(failures)
        )

    try:
        df = call_ak("stock_zh_a_spot_em")
        code_col = "代码" if "代码" in df.columns else df.columns[1]
        sub = df[df[code_col].astype(str).isin(wanted)]
        return df_to_records(sub)
    except (DataUnavailableError, EmptyDataError) as exc:
        logger.warning(f"Eastmoney spot lookup unavailable; using Tencent/Sina: {exc}")
        rows = []
        failures = []
        for code in wanted:
            try:
                rows.append(_alternate_quote(code))
            except DataUnavailableError as fallback_exc:
                failures.append(str(fallback_exc))
        if rows:
            return rows
        raise DataUnavailableError(
            "东方财富、腾讯和新浪均未能提供个股行情: " + "；".join(failures)
        ) from exc


def spot_top(limit: int) -> list[dict]:
    df = call_ak("stock_zh_a_spot_em")
    return df_to_records(df, limit=limit)


def bid_ask(symbol: str) -> dict:
    """通过东方财富单股行情接口获取实时快照和五档盘口。

    使用独立 HTTP 请求并设置连接、读取超时，避免行情请求无限等待。
    """

    code = codes.normalize(symbol)
    market = codes.market_of(code)

    # 东方财富市场代码：沪市为 1，深市为 0
    market_code = "1" if market == "sh" else "0"

    url = "https://push2.eastmoney.com/api/qt/stock/get"

    params = {
        "fltt": "2",
        "invt": "2",
        "secid": f"{market_code}.{code}",
        "fields": (
            "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,"
            "f57,f58,f60,f86,f116,f117,f161,f162,f163,"
            "f164,f167,f168,f169,f170,"
            "f11,f12,f13,f14,f15,f16,f17,f18,f19,f20,"
            "f31,f32,f33,f34,f35,f36,f37,f38,f39,f40"
        ),
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=(5, 12),
        )
        response.raise_for_status()
        payload = response.json()

    except requests.RequestException as exc:
        logger.warning(f"Eastmoney quote unavailable for {code}; trying Tencent/Sina: {exc}")
        return _alternate_quote(code)

    except ValueError as exc:
        logger.warning(f"Eastmoney quote parse failed for {code}; trying Tencent/Sina: {exc}")
        return _alternate_quote(code)

    data = payload.get("data") or {}

    if not data:
        logger.warning(f"Eastmoney returned no quote for {code}; trying Tencent/Sina")
        return _alternate_quote(code)

    def get_value(key: str, multiplier: float = 1.0):
        value = data.get(key)

        if value is None or value == "" or value == "-":
            return None

        try:
            return float(value) * multiplier
        except (TypeError, ValueError):
            return value

    result = {
        "code": code,
        "name": data.get("f58"),
        "source": "东方财富单股实时行情",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "price": get_value("f43"),
        "high": get_value("f44"),
        "low": get_value("f45"),
        "open": get_value("f46"),
        "volume": get_value("f47"),
        "amount": get_value("f48"),
        "outer_volume": get_value("f49"),
        "volume_ratio": get_value("f50"),
        "limit_up": get_value("f51"),
        "limit_down": get_value("f52"),
        "prev_close": get_value("f60"),
        "pct_change": get_value("f170"),
        "change": get_value("f169"),
        "turnover_rate": get_value("f168"),
        "avg_price": get_value("f163"),
        "inner_volume": get_value("f161"),
        "total_market_cap": get_value("f116"),
        "float_market_cap": get_value("f117"),
    }

    # 卖盘五档：卖一至卖五
    ask_fields = [
        ("f39", "f40"),
        ("f37", "f38"),
        ("f35", "f36"),
        ("f33", "f34"),
        ("f31", "f32"),
    ]

    # 买盘五档：买一至买五
    bid_fields = [
        ("f19", "f20"),
        ("f17", "f18"),
        ("f15", "f16"),
        ("f13", "f14"),
        ("f11", "f12"),
    ]

    for level, (price_key, volume_key) in enumerate(ask_fields, 1):
        result[f"ask_{level}"] = get_value(price_key)
        result[f"ask_{level}_vol"] = get_value(volume_key, 100)

    for level, (price_key, volume_key) in enumerate(bid_fields, 1):
        result[f"bid_{level}"] = get_value(price_key)
        result[f"bid_{level}_vol"] = get_value(volume_key, 100)

    return result


def individual_info(symbol: str) -> dict:
    df = call_ak("stock_individual_info_em", symbol=codes.normalize(symbol))
    return df_kv_to_dict(df)


def daily_hist(
    symbol: str,
    start_date: str,
    end_date: str,
    adjust: str = "qfq",
) -> list[dict]:
    df = call_ak(
        "stock_zh_a_hist",
        symbol=codes.normalize(symbol),
        period="daily",
        start_date=dates.to_compact(start_date),
        end_date=dates.to_compact(end_date),
        adjust=adjust or "",
    )
    return df_to_records(df)


def minute_hist(
    symbol: str,
    period: str,
    start_date: str,
    end_date: str,
    adjust: str = "",
) -> list[dict]:
    df = call_ak(
        "stock_zh_a_hist_min_em",
        symbol=codes.normalize(symbol),
        period=str(period),
        start_date=start_date,
        end_date=end_date,
        adjust=adjust or "",
    )
    return df_to_records(df)


# --------------------------------------------------------------------------- #
# B. Fund flow
# --------------------------------------------------------------------------- #

def individual_fund_flow(symbol: str) -> list[dict]:
    df = call_ak(
        "stock_individual_fund_flow",
        stock=codes.normalize(symbol),
        market=codes.market_of(symbol),
    )
    return df_to_records(df)


def market_fund_flow_with_source() -> tuple[list[dict], str]:
    """Get market-wide flow, trying AkShare first and Tushare transport fallback second.

    Tushare moneyflow_mkt_dc uses Eastmoney/DC methodology; it is a separate
    access route, not an independent measurement source.
    """
    primary_error = ""
    try:
        df = call_ak("stock_market_fund_flow")
        records = df_to_records(df)
        latest = latest_record_date(records)
        if not records or latest is None:
            raise DataUnavailableError("AkShare market fund flow returned no dated records")
        age = data_age_days(latest)
        if age > MARKET_FLOW_MAX_AGE_DAYS:
            raise DataUnavailableError(
                f"AkShare market fund flow is stale: latest_date={latest.isoformat()}, "
                f"age_days={age}"
            )
        return records, "AkShare / 东方财富"
    except (DataUnavailableError, EmptyDataError) as exc:
        primary_error = str(exc)
        logger.warning(f"AkShare market fund flow unavailable or stale; trying Tushare fallback: {exc}")

    try:
        from . import tushare_src

        records = tushare_src.market_fund_flow()
        if not records:
            raise EmptyDataError("Tushare market fund flow returned no rows")
        return records, "Tushare Pro moneyflow_mkt_dc（东方财富DC口径，备用访问通道）"
    except (DataUnavailableError, EmptyDataError) as exc:
        raise DataUnavailableError(
            f"AkShare market flow failed: {primary_error}; "
            f"Tushare fallback failed: {exc}"
        ) from exc


def market_fund_flow() -> list[dict]:
    """Backward-compatible data-only wrapper."""
    return market_fund_flow_with_source()[0]


def sector_fund_flow_rank(
    indicator: str,
    sector_type: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_sector_fund_flow_rank",
        indicator=indicator,
        sector_type=sector_type,
    )
    return df_to_records(df, limit=limit)


# 东方财富市场范围参数
_MAIN_FLOW_SCOPES = {
    "全部股票": (
        "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
        "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2"
    ),
    "沪深A股": (
        "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
        "m:1+t:2+f:!2,m:1+t:23+f:!2"
    ),
    "沪市A股": "m:1+t:2+f:!2,m:1+t:23+f:!2",
    "科创板": "m:1+t:23+f:!2",
    "深市A股": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2",
    "创业板": "m:0+t:80+f:!2",
    "沪市B股": "m:1+t:3+f:!2",
    "深市B股": "m:0+t:7+f:!2",
}


def main_fund_flow_rank(
    symbol: str = "全部股票",
    limit: int | None = None,
) -> list[dict]:
    """直接从东方财富分页接口获取个股主力资金流排名。"""

    scope = str(symbol or "全部股票").strip()
    fs = _MAIN_FLOW_SCOPES.get(scope)

    if not fs:
        raise DataUnavailableError(
            f"不支持的取数范围: {scope}。支持范围: "
            + ", ".join(_MAIN_FLOW_SCOPES.keys())
        )

    try:
        page_size = int(limit) if limit is not None else 50
    except (TypeError, ValueError):
        page_size = 50

    page_size = max(1, min(page_size, 200))

    url = "https://push2.eastmoney.com/api/qt/clist/get"

    params = {
        "pn": "1",
        "pz": str(page_size),
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fs": fs,
        "fields": (
            "f2,f3,f12,f13,f14,f62,f184,"
            "f225,f165,f263,f109,f175,f264,f160,f100"
        ),
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        response = requests.get(
            url,
            params=params,
            headers=headers,
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()

    except requests.RequestException as exc:
        raise DataUnavailableError(
            f"东方财富主力资金排名请求失败: {exc}"
        ) from exc
    except ValueError as exc:
        raise DataUnavailableError(
            "东方财富主力资金排名返回了无法解析的数据"
        ) from exc

    data_block = payload.get("data") or {}
    diff = data_block.get("diff") or []

    if isinstance(diff, dict):
        diff = list(diff.values())

    if not diff:
        raise EmptyDataError(
            f"东方财富未返回主力资金排名数据，范围: {scope}"
        )

    results = []

    for index, item in enumerate(diff, start=1):
        if not isinstance(item, dict):
            continue

        results.append({
            "rank": index,
            "code": str(item.get("f12") or ""),
            "name": str(item.get("f14") or ""),
            "price": item.get("f2"),
            "pct_change": item.get("f3"),
            "main_net_inflow": item.get("f62"),
            "main_net_inflow_pct": item.get("f184"),
            "sector": item.get("f100"),
        })

    if not results:
        raise EmptyDataError("主力资金排名数据为空")

    return results


# --------------------------------------------------------------------------- #
# C. LHB (dragon-tiger list)
# --------------------------------------------------------------------------- #

def lhb_detail(
    start_date: str,
    end_date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_lhb_detail_em",
        start_date=dates.to_compact(start_date),
        end_date=dates.to_compact(end_date),
    )
    return df_to_records(df, limit=limit)


def lhb_stock_detail(symbol: str, date: str, flag: str) -> list[dict]:
    df = call_ak(
        "stock_lhb_stock_detail_em",
        symbol=codes.normalize(symbol),
        date=dates.to_compact(date),
        flag=flag,
    )
    return df_to_records(df)


def lhb_institution_daily(
    start_date: str,
    end_date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_lhb_jgmmtj_em",
        start_date=dates.to_compact(start_date),
        end_date=dates.to_compact(end_date),
    )
    return df_to_records(df, limit=limit)


def lhb_active_branches(
    start_date: str,
    end_date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_lhb_hyyyb_em",
        start_date=dates.to_compact(start_date),
        end_date=dates.to_compact(end_date),
    )
    return df_to_records(df, limit=limit)


# --------------------------------------------------------------------------- #
# D. Margin trading
# --------------------------------------------------------------------------- #

def margin_sse_summary(
    start_date: str,
    end_date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_margin_sse",
        start_date=dates.to_compact(start_date),
        end_date=dates.to_compact(end_date),
    )
    return df_to_records(df, limit=limit)


def margin_szse_summary(date: str) -> list[dict]:
    df = call_ak("stock_margin_szse", date=dates.to_compact(date))
    return df_to_records(df)


def margin_detail_sse(
    date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak("stock_margin_detail_sse", date=dates.to_compact(date))
    return df_to_records(df, limit=limit)


def margin_detail_szse(
    date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak("stock_margin_detail_szse", date=dates.to_compact(date))
    return df_to_records(df, limit=limit)


# --------------------------------------------------------------------------- #
# E. HSGT (stock connect)
# --------------------------------------------------------------------------- #

def southbound_hist(limit: int | None = None) -> list[dict]:
    df = call_ak(["stock_hsgt_hist_em"], symbol="南向资金")
    return df_to_records(df, limit=limit)


def hsgt_summary() -> list[dict]:
    df = call_ak("stock_hsgt_fund_flow_summary_em")
    return df_to_records(df)


def northbound_top10(
    date: str,
    limit: int | None = None,
) -> list[dict]:
    """Best-effort northbound activity. Degraded since 2024-08-19."""
    df = call_ak(
        "stock_hsgt_stock_statistics_em",
        symbol="北向持股",
        start_date=dates.to_compact(date),
        end_date=dates.to_compact(date),
    )
    return df_to_records(df, limit=limit)


def northbound_holdings(
    symbol: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_hsgt_individual_em",
        symbol=codes.normalize(symbol),
    )
    return df_to_records(df, limit=limit)


# --------------------------------------------------------------------------- #
# F. Chip distribution
# --------------------------------------------------------------------------- #

def chip_distribution(
    symbol: str,
    adjust: str = "",
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_cyq_em",
        symbol=codes.normalize(symbol),
        adjust=adjust or "",
    )
    return df_to_records(df, limit=limit)


# --------------------------------------------------------------------------- #
# G. Financial / announcements / research
# --------------------------------------------------------------------------- #

_REPORT_FN = {
    "balance": "stock_balance_sheet_by_report_em",
    "income": "stock_profit_sheet_by_report_em",
    "cashflow": "stock_cash_flow_sheet_by_report_em",
}


def financial_report(
    symbol: str,
    report_type: str,
    period: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    fn = _REPORT_FN.get(report_type)
    if not fn:
        raise DataUnavailableError(
            f"unknown report_type {report_type!r}"
        )

    df = call_ak(fn, symbol=codes.to_em(symbol))

    if period:
        p = dates.to_compact(period)
        for col in ("报告期", "REPORT_DATE", "报表日期"):
            if col in df.columns:
                df = df[
                    df[col].astype(str)
                    .str.replace("-", "")
                    .str.startswith(p)
                ]
                break

    return df_to_records(df, limit=limit)


def financial_abstract(
    symbol: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        ["stock_financial_abstract"],
        symbol=codes.normalize(symbol),
    )
    return df_to_records(df, limit=limit)


def earnings_forecast(
    date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak("stock_yjyg_em", date=dates.to_compact(date))
    return df_to_records(df, limit=limit)


def earnings_express(
    date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak("stock_yjkb_em", date=dates.to_compact(date))
    return df_to_records(df, limit=limit)


def announcements(
    symbol: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Recent announcements. Prefer per-stock disclosure; fall back to market feed."""
    if symbol:
        code = codes.normalize(symbol)
        try:
            recent_dates = dates.recent_trade_dates(60)
            start_date = (
                recent_dates[0].strftime("%Y-%m-%d")
                if recent_dates
                else dates.today_str()
            )

            df = call_ak(
                ["stock_zh_a_disclosure_report_cninfo"],
                symbol=code,
                market="沪深京",
                start_date=dates.to_dashed(start_date),
                end_date=dates.today_str(),
            )
            return df_to_records(df, limit=limit)

        except DataUnavailableError:
            pass

    d = dates.to_compact(
        dates.latest_trade_date().strftime("%Y-%m-%d")
    )
    df = call_ak(
        "stock_notice_report",
        symbol="全部",
        date=d,
    )

    if symbol:
        code = codes.normalize(symbol)
        for col in ("代码", "股票代码"):
            if col in df.columns:
                df = df[df[col].astype(str) == code]
                break

    return df_to_records(df, limit=limit)


def research_reports(
    symbol: str,
    limit: int = 10,
) -> list[dict]:
    df = call_ak(
        "stock_research_report_em",
        symbol=codes.normalize(symbol),
    )
    return df_to_records(df, limit=limit)


# --------------------------------------------------------------------------- #
# H. Meta / sentiment
# --------------------------------------------------------------------------- #

def zt_pool(
    date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_zt_pool_em",
        date=dates.to_compact(date),
    )
    return df_to_records(df, limit=limit)


def stock_comment_all(limit: int | None = None) -> list[dict]:
    df = call_ak("stock_comment_em")
    return df_to_records(df, limit=limit)


def stock_comment_one(symbol: str) -> dict:
    code = codes.normalize(symbol)
    df = call_ak("stock_comment_em")
    code_col = "代码" if "代码" in df.columns else df.columns[1]
    sub = df[df[code_col].astype(str) == code]
    recs = df_to_records(sub)
    return recs[0] if recs else {}


def restricted_release(
    start_date: str,
    end_date: str,
    limit: int | None = None,
) -> list[dict]:
    df = call_ak(
        "stock_restricted_release_summary_em",
        start_date=dates.to_compact(start_date),
        end_date=dates.to_compact(end_date),
    )
    return df_to_records(df, limit=limit)
