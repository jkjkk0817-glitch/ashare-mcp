"""Optional tushare fallback for financial statements. Active only when TUSHARE_TOKEN
is set AND the tushare package is installed (``uv sync --extra tushare``)."""

from __future__ import annotations

from loguru import logger

from ._base import DataUnavailableError, EmptyDataError, df_to_records
from ..config import settings
from ..utils import codes

_pro = None

_API = {"income": "income", "balance": "balancesheet", "cashflow": "cashflow"}


def is_available() -> bool:
    if not settings.tushare_token:
        return False
    try:
        import tushare  # noqa: F401

        return True
    except ImportError:
        return False


def _get_pro():
    global _pro
    if _pro is not None:
        return _pro
    if not settings.tushare_token:
        raise DataUnavailableError("TUSHARE_TOKEN not set")
    try:
        import tushare as ts
    except ImportError as e:
        raise DataUnavailableError(
            "tushare not installed; run: uv sync --extra tushare"
        ) from e
    ts.set_token(settings.tushare_token)
    _pro = ts.pro_api()
    logger.debug("tushare pro_api initialized")
    return _pro


def financial(symbol: str, report_type: str, period: str | None = None) -> list[dict]:
    api = _API.get(report_type)
    if not api:
        raise DataUnavailableError(f"tushare has no fallback for report_type {report_type!r}")
    pro = _get_pro()
    fn = getattr(pro, api)
    kwargs = {"ts_code": codes.to_tushare(symbol)}
    if period:
        kwargs["period"] = period.replace("-", "")
    df = fn(**kwargs)
    if df is None or df.empty:
        raise EmptyDataError("tushare returned empty")
    return df_to_records(df)


def market_fund_flow() -> list[dict]:
    """Optional Tushare transport fallback for Eastmoney's market-wide DC flow.

    This endpoint uses the existing TUSHARE_TOKEN setting and requests directly,
    so the optional tushare Python package is not required. Tushare's
    moneyflow_mkt_dc is still Eastmoney/DC methodology, not an independent
    measurement of market-wide capital flow.
    """
    import requests

    token = (settings.tushare_token or "").strip()
    if not token:
        raise DataUnavailableError("TUSHARE_TOKEN not configured; market-flow fallback disabled")

    fields = (
        "trade_date,close_sh,pct_change_sh,close_sz,pct_change_sz,"
        "net_amount,net_amount_rate,buy_elg_amount,buy_elg_amount_rate,"
        "buy_lg_amount,buy_lg_amount_rate,buy_md_amount,buy_md_amount_rate,"
        "buy_sm_amount,buy_sm_amount_rate"
    )
    body = {
        "api_name": "moneyflow_mkt_dc",
        "token": token,
        "params": {},
        "fields": fields,
    }
    try:
        response = requests.post(
            "https://api.tushare.pro",
            json=body,
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise DataUnavailableError(
            f"Tushare moneyflow_mkt_dc request/parse failed: {type(exc).__name__}: {exc}"
        ) from exc

    try:
        response_code = int(payload.get("code", -1)) if isinstance(payload, dict) else -1
    except (TypeError, ValueError):
        response_code = -1
    if response_code != 0:
        code = payload.get("code") if isinstance(payload, dict) else "invalid_response"
        message = payload.get("msg", "invalid response") if isinstance(payload, dict) else "invalid response"
        raise DataUnavailableError(f"Tushare moneyflow_mkt_dc rejected request ({code}): {message}")

    block = payload.get("data") or {}
    names = block.get("fields") or []
    items = block.get("items") or []
    if not names or not items:
        raise EmptyDataError("Tushare moneyflow_mkt_dc returned no rows")

    mapping = {
        "trade_date": "date",
        "close_sh": "sh_close",
        "pct_change_sh": "sh_pct_change",
        "close_sz": "sz_close",
        "pct_change_sz": "sz_pct_change",
        "net_amount": "main_net_inflow",
        "net_amount_rate": "main_net_inflow_pct",
        "buy_elg_amount": "xl_net_inflow",
        "buy_elg_amount_rate": "xl_net_inflow_pct",
        "buy_lg_amount": "lg_net_inflow",
        "buy_lg_amount_rate": "lg_net_inflow_pct",
        "buy_md_amount": "md_net_inflow",
        "buy_md_amount_rate": "md_net_inflow_pct",
        "buy_sm_amount": "sm_net_inflow",
        "buy_sm_amount_rate": "sm_net_inflow_pct",
    }
    records = []
    for values in items:
        if not isinstance(values, (list, tuple)):
            continue
        raw = dict(zip(names, values))
        row = {target: raw.get(source) for source, target in mapping.items() if source in raw}
        if row.get("date"):
            row["date"] = str(row["date"]).replace("-", "")[:8]
            if len(row["date"]) == 8:
                row["date"] = f"{row['date'][:4]}-{row['date'][4:6]}-{row['date'][6:8]}"
            records.append(row)

    if not records:
        raise EmptyDataError("Tushare moneyflow_mkt_dc returned no valid dated rows")
    records.sort(key=lambda item: item["date"], reverse=True)
    return records
