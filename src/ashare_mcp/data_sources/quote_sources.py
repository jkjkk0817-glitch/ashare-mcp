"""两个相互独立的实时行情源（腾讯、新浪），支持批量，并解析行情时间。

用于"最新价第二来源核验"：腾讯和新浪是两个独立接口，各自带行情时间，
可以直接比较价格偏差和时间差。批量请求，每个来源一次请求取多只，盘中调用频率高也轻量。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from loguru import logger

from ._base import DataUnavailableError

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
    "Accept": "*/*",
}
_TIMEOUT = (3, 6)

_TENCENT_LINE = re.compile(r'v_([a-z]{2}\w+)="(.*?)";', re.DOTALL)
_SINA_LINE = re.compile(r'hq_str_([a-z]{2}\w+)="(.*?)";', re.DOTALL)


def _f(value: Any) -> float | None:
    try:
        text = str(value).strip()
        if text in {"", "-"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_time(text: str, fmt: str) -> datetime | None:
    try:
        return datetime.strptime(text.strip(), fmt).replace(tzinfo=_SHANGHAI)
    except (TypeError, ValueError):
        return None


def parse_tencent(raw: str) -> dict[str, dict]:
    """解析腾讯返回文本。金额统一换算为元，成交量为股。"""
    out: dict[str, dict] = {}
    for prefixed, payload in _TENCENT_LINE.findall(raw or ""):
        fields = payload.split("~")
        if len(fields) < 35 or not fields[1].strip():
            continue
        price = _f(fields[3])
        if price is None or price <= 0:
            continue
        amount_wan = (
            _f(fields[57]) if len(fields) > 57 and _f(fields[57]) is not None
            else (_f(fields[37]) if len(fields) > 37 else None)
        )
        out[prefixed] = {
            "name": fields[1].strip(),
            "code": fields[2].strip(),
            "price": price,
            "prev_close": _f(fields[4]),
            "open": _f(fields[5]),
            "volume": (_f(fields[6]) or 0.0) * 100,
            "change": _f(fields[31]),
            "pct_change": _f(fields[32]),
            "high": _f(fields[33]),
            "low": _f(fields[34]),
            "amount": None if amount_wan is None else amount_wan * 10000,
            "turnover_rate": _f(fields[38]) if len(fields) > 38 else None,
            "quote_time": _parse_time(fields[30], "%Y%m%d%H%M%S") if len(fields) > 30 else None,
        }
    return out


def parse_sina(raw: str) -> dict[str, dict]:
    """解析新浪返回文本。成交量为股，金额为元。"""
    out: dict[str, dict] = {}
    for prefixed, payload in _SINA_LINE.findall(raw or ""):
        fields = payload.split(",")
        if len(fields) < 32 or not fields[0].strip():
            continue
        price = _f(fields[3])
        prev_close = _f(fields[2])
        if price is None or price <= 0:
            continue
        change = price - prev_close if prev_close else None
        pct = change / prev_close * 100 if change is not None and prev_close else None
        out[prefixed] = {
            "name": fields[0].strip(),
            "code": prefixed[2:],
            "price": price,
            "prev_close": prev_close,
            "open": _f(fields[1]),
            "high": _f(fields[4]),
            "low": _f(fields[5]),
            "volume": _f(fields[8]),
            "amount": _f(fields[9]),
            "change": change,
            "pct_change": pct,
            "turnover_rate": None,
            "quote_time": _parse_time(f"{fields[30]} {fields[31]}", "%Y-%m-%d %H:%M:%S"),
        }
    return out


def _get(url: str, encoding: str = "gbk") -> str:
    response = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    response.raise_for_status()
    response.encoding = encoding
    return response.text


def fetch_tencent(prefixed: list[str]) -> dict[str, dict]:
    return parse_tencent(_get("https://qt.gtimg.cn/q=" + ",".join(prefixed)))


def fetch_sina(prefixed: list[str]) -> dict[str, dict]:
    return parse_sina(_get("https://hq.sinajs.cn/list=" + ",".join(prefixed)))


def fetch_both(prefixed: list[str]) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """并行取两个来源。返回 (腾讯, 新浪, 各来源错误)。两个都失败才抛异常。"""
    errors: dict[str, str] = {}

    def run(name: str, fn):
        try:
            return fn(prefixed)
        except (requests.RequestException, ValueError) as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            logger.warning(f"quote source {name} failed: {exc}")
            return {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        tencent_future = pool.submit(run, "tencent", fetch_tencent)
        sina_future = pool.submit(run, "sina", fetch_sina)
        tencent = tencent_future.result()
        sina = sina_future.result()

    if not tencent and not sina:
        raise DataUnavailableError(
            "腾讯和新浪行情均不可用: " + "；".join(f"{k}={v}" for k, v in errors.items())
            if errors else "腾讯和新浪均未返回所请求的行情"
        )
    return tencent, sina, errors


def _is_post_close(moment: datetime) -> bool:
    """行情时间戳是否在当日15:00:00之后（即收盘数据）。"""
    return moment.hour >= 15


def verify_pair(
    tencent: dict | None,
    sina: dict | None,
    max_seconds: float = 180.0,
    max_deviation_pct: float = 0.3,
) -> dict:
    """按双源核验标准比较两边：名称一致、行情日期一致、时间差<=180秒、价格偏差<=0.3%。

    偏差 = 绝对价差 / 第一来源（腾讯）价格 × 100%。
    例外：两边时间戳都在同一天15:00之后（收盘数据）时不比较时间差，
    结果中 time_rule="post_close"，其余条件照常核验。
    """
    reasons: list[str] = []
    result: dict[str, Any] = {
        "verified": False,
        "price_diff_pct": None,
        "time_diff_seconds": None,
        "reasons": reasons,
    }
    if not tencent and not sina:
        reasons.append("两个来源均无数据")
        return result
    if not tencent or not sina:
        reasons.append("只有一个来源有数据，无法交叉核验")
        return result

    if tencent["name"].replace(" ", "") != sina["name"].replace(" ", ""):
        reasons.append(f"名称不一致: 腾讯={tencent['name']} 新浪={sina['name']}")

    t_time, s_time = tencent.get("quote_time"), sina.get("quote_time")
    result["time_rule"] = None
    if t_time is None or s_time is None:
        reasons.append("至少一个来源没有可识别的行情时间")
    else:
        same_date = t_time.date() == s_time.date()
        if not same_date:
            reasons.append(f"行情日期不一致: 腾讯={t_time.date()} 新浪={s_time.date()}")
        diff_seconds = abs((t_time - s_time).total_seconds())
        result["time_diff_seconds"] = round(diff_seconds, 1)
        # 收盘后各来源的时间戳含义不同（新浪固定15:00:00，腾讯是收盘后的最后更新时间，
        # 可能晚到十几点甚至更晚），此时比较时间差没有意义；两边都在15:00之后且同一
        # 交易日，就说明两边都是收盘数据，改为只核验日期和价格。
        post_close = same_date and _is_post_close(t_time) and _is_post_close(s_time)
        if post_close:
            result["time_rule"] = "post_close"
        elif diff_seconds > max_seconds:
            reasons.append(f"行情时间相差{diff_seconds:.0f}秒，超过{max_seconds:.0f}秒")

    base = tencent["price"]
    deviation = abs(tencent["price"] - sina["price"]) / base * 100 if base else None
    if deviation is not None:
        result["price_diff_pct"] = round(deviation, 4)
        if deviation > max_deviation_pct:
            reasons.append(f"价格偏差{deviation:.3f}%，超过{max_deviation_pct}%")

    result["verified"] = not reasons
    return result
