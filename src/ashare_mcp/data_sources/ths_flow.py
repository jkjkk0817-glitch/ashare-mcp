"""同花顺行业/概念资金流 —— 东方财富不可用时的备用通道。

口径说明（重要）:
* 同花顺的"流入资金/流出资金/净额"与东方财富的"主力净流入"统计口径不同，
  两者数值不可直接比较或混用。
* 同花顺页面不提供数据日期。"即时"在盘中是实时快照，盘后是最近一个交易日；
  使用时须结合行情日期自行核对。
"""

from __future__ import annotations

import math
import re
from typing import Any

from loguru import logger

from ._base import DataUnavailableError, EmptyDataError
from ._retry import call_ak

# 本工具的周期 -> 同花顺 symbol
_PERIOD_MAP = {
    "今日": "即时",
    "5日": "5日排行",
    "10日": "10日排行",
}

# 本工具的板块类型 -> akshare 函数名
_FUNC_MAP = {
    "行业资金流": "stock_fund_flow_industry",
    "概念资金流": "stock_fund_flow_concept",
}

_YI = 1e8
_WAN = 1e4
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def to_yuan(value: Any) -> float | None:
    """把同花顺的金额转成人民币元。

    同花顺行业/概念资金流表头单位为"亿"。纯数字按亿处理；
    带"亿"/"万"后缀的字符串按其后缀换算。无法解析返回 None。
    """
    if _is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value) * _YI

    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "nan", "None"}:
        return None
    if text.endswith("亿"):
        body, scale = text[:-1], _YI
    elif text.endswith("万"):
        body, scale = text[:-1], _WAN
    else:
        body, scale = text, _YI
    body = body.strip()
    if not _NUM_RE.match(body):
        return None
    return float(body) * scale


def to_pct(value: Any) -> float | None:
    """'3.21%' / 3.21 -> 3.21；无法解析返回 None。"""
    if _is_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("%", "").replace(",", "")
    if not _NUM_RE.match(text):
        return None
    return float(text)


def _to_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def normalize_rows(rows: list[dict], realtime: bool) -> list[dict]:
    """把同花顺行（中文列名）转成统一的英文字段，并按净额从高到低排序。"""
    out: list[dict] = []
    for row in rows:
        name = row.get("行业")
        if _is_missing(name) or not str(name).strip():
            continue
        item: dict[str, Any] = {
            "name": str(name).strip(),
            "index_value": _to_float(row.get("行业指数")),
            "inflow": to_yuan(row.get("流入资金")),
            "outflow": to_yuan(row.get("流出资金")),
            "net_inflow": to_yuan(row.get("净额")),
            "company_count": _to_int(row.get("公司家数")),
        }
        if realtime:
            item["pct_change"] = to_pct(row.get("行业-涨跌幅"))
            item["leader_name"] = (
                None if _is_missing(row.get("领涨股")) else str(row.get("领涨股"))
            )
            item["leader_pct_change"] = to_pct(row.get("领涨股-涨跌幅"))
            item["leader_price"] = _to_float(row.get("当前价"))
        else:
            item["pct_change"] = to_pct(row.get("阶段涨跌幅"))
        out.append(item)

    out.sort(
        key=lambda r: (r["net_inflow"] is None, -(r["net_inflow"] or 0.0)),
    )
    for rank, item in enumerate(out, start=1):
        item["rank"] = rank
        item["basis"] = "ths"
    return out


def sector_fund_flow(
    period: str,
    sector_type: str,
    limit: int | None = None,
) -> list[dict]:
    """同花顺行业/概念资金流排名（备用通道）。金额单位：人民币元。"""
    func = _FUNC_MAP.get(sector_type)
    ths_symbol = _PERIOD_MAP.get(period)
    if func is None or ths_symbol is None:
        raise DataUnavailableError(
            f"同花顺备用通道不支持: period={period!r}, sector_type={sector_type!r}"
        )

    df = call_ak(func, symbol=ths_symbol)
    rows = df.to_dict(orient="records")
    records = normalize_rows(rows, realtime=(ths_symbol == "即时"))
    if not records:
        raise EmptyDataError(f"同花顺{sector_type}返回的数据无法解析")

    if limit is not None:
        records = records[: max(1, int(limit))]
    logger.info(f"ths_flow: {func}({ths_symbol}) -> {len(records)} rows")
    return records
