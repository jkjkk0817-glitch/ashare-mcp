
"""The shared FastMCP application instance."""

from __future__ import annotations

print("[STARTUP] Importing application dependencies...", flush=True)

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from starlette.routing import Route

from . import __version__
from .config import settings

print("[STARTUP] Application dependencies imported.", flush=True)


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


class GrokCompatibleOIDCProxy(OIDCProxy):
    """Keep DCR registration and OAuth metadata consistent for public clients."""

    async def register_client(self, client_info):
        # This proxy stores downstream MCP clients as public clients.
        # Normalize the response object before the SDK serializes it.
        client_info.token_endpoint_auth_method = "none"
        client_info.client_secret = None
        client_info.client_secret_expires_at = None

        await super().register_client(client_info)

    def get_routes(self, mcp_path: str | None = None):
        routes = super().get_routes(mcp_path)
        custom_routes = []

        for route in routes:
            if (
                isinstance(route, Route)
                and route.path.startswith(
                    "/.well-known/oauth-authorization-server"
                )
            ):
                registration_options = (
                    self.client_registration_options
                    or ClientRegistrationOptions()
                )
                revocation_options = (
                    self.revocation_options
                    or RevocationOptions()
                )

                metadata = build_metadata(
                    self.base_url,
                    self.service_documentation_url,
                    registration_options,
                    revocation_options,
                )

                # The proxy authenticates downstream DCR clients as public clients.
                metadata.token_endpoint_auth_methods_supported = ["none"]

                # CIMD clients may additionally use private_key_jwt.
                if self._cimd_manager is not None:
                    metadata.client_id_metadata_document_supported = True
                    metadata.token_endpoint_auth_methods_supported.append(
                        "private_key_jwt"
                    )

                handler = MetadataHandler(metadata)

                custom_routes.append(
                    Route(
                        path=route.path,
                        endpoint=cors_middleware(
                            handler.handle,
                            ["GET", "OPTIONS"],
                        ),
                        methods=route.methods or ["GET", "OPTIONS"],
                        name=route.name,
                        include_in_schema=route.include_in_schema,
                    )
                )
            else:
                custom_routes.append(route)

        return custom_routes


# Production uses encrypted Redis storage.
# Development retains FastMCP's default local storage behavior.
client_storage = None

if settings.environment == "production":
    print("[STARTUP] Initializing Redis OAuth storage...", flush=True)

    redis_store = RedisStore(url=settings.redis_url)

    print("[STARTUP] Redis store object created.", flush=True)

    client_storage = FernetEncryptionWrapper(
        key_value=redis_store,
        fernet=Fernet(settings.storage_encryption_key.encode("utf-8")),
    )

    print("[STARTUP] Encrypted Redis storage initialized.", flush=True)


auth = None

if all((
    settings.auth0_domain,
    settings.auth0_audience,
    settings.auth0_client_id,
    settings.public_base_url,
    settings.jwt_signing_key,
)):
    proxy_options = {
        "config_url": (
            f"https://{settings.auth0_domain}/"
            ".well-known/openid-configuration"
        ),
        "timeout_seconds": 10,
        "client_id": settings.auth0_client_id,
        "client_secret": None,
        "audience": settings.auth0_audience,
        "base_url": settings.public_base_url,
        "token_endpoint_auth_method": "none",
        "jwt_signing_key": settings.jwt_signing_key,
    }

    if client_storage is not None:
        proxy_options["client_storage"] = client_storage

    print("[STARTUP] Initializing Auth0 OIDC proxy...", flush=True)

    auth = GrokCompatibleOIDCProxy(**proxy_options)

    print("[STARTUP] Auth0 OIDC proxy initialized.", flush=True)


# Production configuration validation in config.py ensures that
# Auth0 and Redis settings cannot silently be omitted.
if settings.environment == "production" and auth is None:
    raise RuntimeError(
        "Production authentication initialization failed."
    )


print("[STARTUP] Creating FastMCP server instance...", flush=True)

mcp: FastMCP = FastMCP(
    "ashare-mcp",
    instructions=INSTRUCTIONS,
    version=__version__,
    auth=auth,
)

print("[STARTUP] FastMCP server instance created.", flush=True)
