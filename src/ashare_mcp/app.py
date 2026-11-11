
"""The shared FastMCP application instance."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth.oidc_proxy import OIDCProxy

from . import __version__
from .config import settings


INSTRUCTIONS = """\
中国 A 股免费数据 MCP server。覆盖行情、资金流向、龙虎榜、融资融券、
沪深港通、筹码分布、财报公告、千股千评等。主数据源为 akshare(东方财富等),
K 线与财报在 akshare 失败时降级到 baostock,可选 tushare。

【重要免责 / 数据可靠性边界】
- "主力资金 / 大单拆分" 是东方财富按单笔成交金额机械分桶估算,各行情软件口径不一,
  仅供参考,不代表真实机构意图。
- "筹码分布 / 获利盘" 是概率模型估算,各家算法不同,精度有限。
- 北向资金:2024-08-19 起官方取消盘中实时与日频买卖明细披露。本服务只能提供
  T+1 成交总额、十大活跃成交股(无买卖拆分)、季度持仓(延迟约 3 个月)。
  实时 / 日频净流入接口已停用,调用会返回明确说明。
- 龙虎榜、融资融券、财务报表是交易所 / 上市公司官方原始数据,可靠(通常 T+1)。
- 南向资金(港股通)数据完整可用,未受 2024 调整影响。
- 任何数据与分析均不构成投资建议。

【调用约定】
- 股票代码接受多种写法:600519 / sh600519 / 600519.SH,内部会统一规范化。
- 日期统一使用 "YYYY-MM-DD"(也接受 "YYYYMMDD");"今天 / 最近" 等会自动解析为最近交易日。
- 排名 / 全市场类工具默认只返回前 N 条,可用 limit 参数调整。
- 若需要"按关键词搜索股票 / 公告 / 研报"或"按 id 取详情",请使用 search / fetch
  工具(同时兼容 ChatGPT deep research)。
"""


auth = None

if all((
    settings.auth0_domain,
    settings.auth0_audience,
    settings.auth0_client_id,
    settings.public_base_url,
    settings.jwt_signing_key,
)):
    auth = OIDCProxy(
        config_url=(
            f"https://{settings.auth0_domain}/"
            ".well-known/openid-configuration"
        ),
        client_id=settings.auth0_client_id,
        client_secret=None,
        audience=settings.auth0_audience,
        base_url=settings.public_base_url,
        token_endpoint_auth_method="none",
        jwt_signing_key=settings.jwt_signing_key,
    )


mcp: FastMCP = FastMCP(
    "ashare-mcp",
    instructions=INSTRUCTIONS,
    version=__version__,
    auth=auth,
)
