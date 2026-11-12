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
    """根据 search 返回的 id 取详细资料(兼容 ChatGPT deep research / company knowledge)。

    参数: id 形如 "stock:600519"(由 search 返回)。
    返回: {"id","title","text"(人类可读综述),"url","metadata"(结构化字段: info/quote/comment)}。
    """
    raw = str(id or "")
    kind, _, val = raw.partition(":")
    if kind == "stock" and val:
        code = codes.normalize(val)
        info = {}
        quote = {}
        comment = {}
        try:
            info = ak.individual_info(code)
        except DataSourceError:
            pass
        try:
            quote = (ak.spot_lookup([code]) or [{}])[0]
        except DataSourceError:
            pass
        try:
            comment = ak.stock_comment_one(code)
        except DataSourceError:
            pass

        name = info.get("name") or quote.get("name") or code
        lines = [
            f"{name} ({code})",
            f"行业: {info.get('industry', '—')}",
            f"上市时间: {info.get('list_date', '—')}",
            f"总市值: {info.get('total_market_cap', '—')} | 流通市值: {info.get('float_market_cap', '—')}",
            f"总股本: {info.get('total_shares', '—')} | 流通股: {info.get('float_shares', '—')}",
            f"最新价: {quote.get('price', '—')} | 涨跌幅: {quote.get('pct_change', '—')}% | "
            f"换手率: {quote.get('turnover_rate', '—')}% | 量比: {quote.get('volume_ratio', '—')}",
        ]
        if comment:
            lines.append(
                f"千股千评(软指标,仅供参考): 综合得分 {comment.get('composite_score', '—')} | "
                f"机构参与度 {comment.get('institution_participation', '—')} | "
                f"关注指数 {comment.get('attention_index', '—')}"
            )
        lines.append("数据来源: akshare / 东方财富。数据仅供研究,不构成投资建议。")
        return {
            "id": raw,
            "title": f"{name} ({code})",
            "text": "\n".join(str(x) for x in lines),
            "url": _stock_url(code),
            "metadata": {"code": code, "industry": info.get("industry"),
                         "info": info, "quote": quote, "comment": comment},
        }

    return {
        "id": raw,
        "title": raw or "unknown",
        "text": "未知的 id。请先调用 search 获取形如 'stock:600519' 的 id 再 fetch。",
        "url": "",
        "metadata": {},
    }
