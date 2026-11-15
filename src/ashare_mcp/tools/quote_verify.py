"""盘中高频使用的行情工具（腾讯 + 新浪双源，带行情时间与核验结论）。

* get_verified_quote: 多只股票最新价，两个独立来源交叉核验。
* get_index_quotes:   五大指数最新点位与涨跌幅，两个来源交叉核验。

价格偏差、时间差的判定标准与自动化任务提示词第五节一致：
名称一致、行情日期一致、两边行情时间相差<=180秒、价格偏差<=0.3%。
行情工具不做快照，失败时直接报错，绝不返回旧价格。
"""

from __future__ import annotations

from ..app import mcp
from ..cache import TTL_REALTIME, cached, in_trading_session, now_cn
from ..data_sources import quote_sources as qs
from ..utils import codes
from ._helpers import DISCLAIMER_NOT_ADVICE, err, guard, ok

MAX_SYMBOLS = 30
LIMIT_UP_PCT = 9.9  # 沪深主板涨停约10%

INDEX_MAP = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sh000300", "沪深300"),
    ("sz399006", "创业板指"),
    ("sh000852", "中证1000"),
]

RULE_TEXT = "名称一致、行情日期一致、两来源行情时间相差<=180秒、价格偏差<=0.3%（以腾讯价为基准）"


def _iso(dt) -> str | None:
    return None if dt is None else dt.isoformat(timespec="seconds")


def _source_view(quote: dict | None) -> dict | None:
    if not quote:
        return None
    return {
        "price": quote["price"],
        "pct_change": None if quote.get("pct_change") is None else round(quote["pct_change"], 3),
        "quote_time": _iso(quote.get("quote_time")),
        "amount": quote.get("amount"),
    }


def _merge_row(prefixed: str, tencent: dict | None, sina: dict | None) -> dict:
    check = qs.verify_pair(tencent, sina)
    primary = tencent or sina or {}
    quote_time = primary.get("quote_time")
    now = now_cn()
    age_seconds = None if quote_time is None else round((now - quote_time).total_seconds())

    row = {
        "code": primary.get("code") or prefixed[2:],
        "name": primary.get("name"),
        "verified": check["verified"],
        "verify_reasons": check["reasons"],
        "price": primary.get("price"),
        "pct_change": None if primary.get("pct_change") is None
        else round(primary["pct_change"], 3),
        "prev_close": primary.get("prev_close"),
        "open": primary.get("open"),
        "high": primary.get("high"),
        "low": primary.get("low"),
        "amount": primary.get("amount"),
        "turnover_rate": primary.get("turnover_rate"),
        "quote_time": _iso(quote_time),
        "quote_age_seconds": age_seconds,
        "price_diff_pct": check["price_diff_pct"],
        "time_diff_seconds": check["time_diff_seconds"],
        "primary_source": "腾讯财经" if tencent else "新浪财经",
        "sources": {"tencent": _source_view(tencent), "sina": _source_view(sina)},
    }
    warnings = []
    if in_trading_session() and age_seconds is not None and age_seconds > 300:
        warnings.append("盘中行情时间距现在超过5分钟，可能停牌或数据延迟")
    name = row["name"] or ""
    if "ST" in name.upper():
        warnings.append("名称含ST，不在选股范围")
    row["warnings"] = warnings
    return row


@mcp.tool
@guard
@cached(ttl=TTL_REALTIME)
def get_verified_quote(symbols: list[str]) -> dict:
    """获取多只 A 股的最新价，并用腾讯、新浪两个独立来源交叉核验。

    盘中高频使用：每个来源一次批量请求，最多 30 只。
    每只返回: price(腾讯价为基准)、pct_change、quote_time(行情时间，北京时间)、
    verified(是否通过双源核验)、verify_reasons(未通过原因)、price_diff_pct、
    time_diff_seconds、sources(两个来源各自的价格与时间)、warnings。
    核验标准: 名称一致、行情日期一致、行情时间相差<=180秒、价格偏差<=0.3%。
    非交易时段返回最近一个交易日的收盘价和收盘时间，同样做双源核验。
    不做快照；两个来源都失败时直接报错，不返回旧价格。
    参数: symbols: 股票代码列表(多种写法均可)，如 ["600919", "601009"]。
    """
    if not symbols:
        return err("bad_request", "symbols 不能为空", "示例: ['600919','601009']")

    wanted = [codes.normalize(s) for s in symbols][:MAX_SYMBOLS]
    prefixed = [codes.with_prefix(c) for c in wanted]
    tencent, sina, errors = qs.fetch_both(prefixed)

    rows = [_merge_row(p, tencent.get(p), sina.get(p)) for p in prefixed]
    for row in rows:
        row["limit_up_like"] = bool(
            row["pct_change"] is not None and row["pct_change"] >= LIMIT_UP_PCT
        )

    return ok(
        rows,
        count=len(rows),
        verified_count=sum(1 for r in rows if r["verified"]),
        source="腾讯财经 + 新浪财经（两个独立接口）",
        verify_rule=RULE_TEXT,
        retrieved_at=_iso(now_cn()),
        market_session="trading" if in_trading_session() else "closed",
        source_errors=errors or None,
        note=DISCLAIMER_NOT_ADVICE,
    )


@mcp.tool
@guard
@cached(ttl=TTL_REALTIME)
def get_index_quotes() -> dict:
    """获取上证指数、深证成指、沪深300、创业板指、中证1000 的最新点位，双源核验。

    每个指数返回 price(点位)、pct_change、quote_time、verified 及两来源各自数值。
    two_market_amount_approx 为上证指数与深证成指成交额之和的近似值，仅在两个来源的成交额
    互相吻合(相差<=5%)时给出，口径与交易所公布的两市成交额可能有差异，仅供盘中参考。
    不含涨跌家数、涨跌停数；这些需另行获取。不做快照，失败直接报错。
    """
    prefixed = [p for p, _ in INDEX_MAP]
    tencent, sina, errors = qs.fetch_both(prefixed)

    rows = []
    for p, label in INDEX_MAP:
        row = _merge_row(p, tencent.get(p), sina.get(p))
        row["name"] = row["name"] or label
        row["label"] = label
        rows.append(row)

    approx = None
    amounts = {}
    for p in ("sh000001", "sz399001"):
        t_amt = (tencent.get(p) or {}).get("amount")
        s_amt = (sina.get(p) or {}).get("amount")
        if t_amt and s_amt and abs(t_amt - s_amt) / t_amt <= 0.05:
            amounts[p] = t_amt
    if len(amounts) == 2:
        approx = sum(amounts.values())

    return ok(
        rows,
        count=len(rows),
        verified_count=sum(1 for r in rows if r["verified"]),
        two_market_amount_approx=approx,
        amount_unit="CNY",
        source="腾讯财经 + 新浪财经（两个独立接口）",
        verify_rule=RULE_TEXT,
        retrieved_at=_iso(now_cn()),
        market_session="trading" if in_trading_session() else "closed",
        source_errors=errors or None,
        note=DISCLAIMER_NOT_ADVICE,
    )
