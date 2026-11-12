"""I. ChatGPT 兼容: search() 与 fetch()。

遵循 OpenAI Apps SDK / MCP compatibility 规范:
- search(query) -> {"results": [{"id","title","url","snippet"}, ...]}
- fetch(id)     -> {"id","title","text","url","metadata"}
这样 ChatGPT deep research / company knowledge 可用本服务作为数据连接器。
"""

from __future__ import annotations

import re

from loguru import logger

from ..app import mcp
from ..cache import cached
from ..data_sources import akshare_src as ak
from ..data_sources._base import DataSourceError
from ..utils import codes
from ._helpers import guard

_MARKET_HOST = {"sh": "sh", "sz": "sz", "bj": "bj"}


def _stock_url(code: str) -> str:
    pre = _MARKET_HOST.get(codes.market_of(code), "sh")
    return f"https://quote.eastmoney.com/{pre}{code}.html"


@mcp.tool
@guard
@cached(ttl=60)
def search(query: str) -> dict:
    """搜索 A 股股票(按代码或公司简称匹配),返回可被 fetch 取详情的结果列表。

    兼容 ChatGPT deep research / company knowledge。
    参数: query 关键词,可为股票代码(600519)或公司简称片段(茅台/宁德/比亚迪)。
    返回: {"results": [{"id": "stock:600519", "title": "贵州茅台 (600519)",
            "url": "...", "snippet": "最新价/涨跌幅/换手率"}, ...]} (最多 20 条)。
    用 results[i].id 调用 fetch 获取该股票的详细资料。
    """
    q = str(query or "").strip()
    if not q:
        return {"results": []}

    results: list[dict] = []
    seen: set[str] = set()
    digits = re.sub(r"\D", "", q)

    # Exact stock-code searches should avoid downloading the entire market snapshot.
    # spot_lookup has its own Tencent/Sina fallback when Eastmoney is unavailable.
    try:
        if len(digits) >= 6:
            rows = ak.spot_lookup([digits])
        else:
            rows = ak.spot_top(6000)
    except DataSourceError as e:
        logger.warning(f"search spot fetch failed: {e}")
        rows = []

    ql = q.lower()
    # exact-code first
    for r in rows:
        code = str(r.get("code") or "")
        if len(digits) >= 6 and code == digits:
            seen.add(code)
            results.append(_result_from_row(code, r))
            break

    for r in rows:
        if len(results) >= 20:
            break
        code = str(r.get("code") or "")
        name = str(r.get("name") or "")
        if not code or code in seen:
            continue
        if (digits and digits in code) or (ql and ql in name.lower()):
            seen.add(code)
            results.append(_result_from_row(code, r))

    return {"results": results}


def _result_from_row(code: str, r: dict) -> dict:
    name = r.get("name") or ""
    snippet = (
        f"最新价 {r.get('price')} | 涨跌幅 {r.get('pct_change')}% | "
        f"换手率 {r.get('turnover_rate')}% | 流通市值 {r.get('float_market_cap')}"
    )
    return {
        "id": f"stock:{code}",
        "title": f"{name} ({code})",
        "url": _stock_url(code),
        "snippet": snippet,
    }


@mcp.tool
@guard
@cached(ttl=30)
def fetch(id: str) -> dict:
    """Fetch a fast single-stock snapshot for ids such as stock:600519.

    Uses Tencent/Sina directly instead of downloading Eastmoney's full-market
    snapshot. Slow full-market sentiment and basic-info endpoints are deliberately
    excluded from this latency-sensitive tool.
    """
    raw = str(id or "")
    kind, _, val = raw.partition(":")
    if kind == "stock" and val:
        code = codes.normalize(val)
        quote = {}
        try:
            quote = (ak.spot_lookup([code], prefer_alternate=True) or [{}])[0]
        except DataSourceError as exc:
            logger.warning(f"fast quote lookup failed for {code}: {exc}")

        name = quote.get("name") or code
        lines = [
            f"{name} ({code})",
            "基本资料: 本次快速查询未请求（避免慢速接口拖延响应）",
            f"最新价: {quote.get('price', '—')} | 涨跌幅: {quote.get('pct_change', '—')}% | "
            f"换手率: {quote.get('turnover_rate', '—')}%",
            f"成交量: {quote.get('volume', '—')} | 成交额: {quote.get('amount', '—')}",
        ]
        quote_source = quote.get("source") or "腾讯/新浪备用行情"
        quote_time = quote.get("quote_time") or quote.get("retrieved_at") or "未提供"
        lines.append(
            f"行情数据来源: {quote_source} | 行情时间/获取时间: {quote_time}。"
            "数据仅供研究,不构成投资建议。"
        )
        lines.append("辅助指标: 本次快速查询未请求；如需分析，请另行调用对应指标工具。")
        return {
            "id": raw,
            "title": f"{name} ({code})",
            "text": "\n".join(str(x) for x in lines),
            "url": _stock_url(code),
            "metadata": {
                "code": code,
                "quote": quote,
                "info": {},
                "comment": {},
                "comment_data_date": None,
                "comment_source": None,
                "note": "快速查询模式：跳过慢速基本资料及全市场千股千评接口",
            },
        }

