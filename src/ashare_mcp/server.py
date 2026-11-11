
"""Entry point: argparse switches stdio / Streamable HTTP."""

from __future__ import annotations

import argparse
import hmac
import re
import sys

from loguru import logger

from . import __version__
from .app import mcp
from .config import settings, setup_logging

# Import all tool modules so their @mcp.tool decorators register on import.
from .tools import (  # noqa: F401
    chip,
    financial,
    fundflow,
    hsgt,
    lhb,
    margin,
    market,
    meta,
    search_fetch,
)

MCP_PATH = "/mcp"


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def _tool_count() -> int | str:
    try:
        import asyncio
        import inspect

        r = mcp.list_tools()
        if inspect.iscoroutine(r):
            r = asyncio.run(r)
        return len(r)
    except Exception:  # noqa: BLE001
        tm = getattr(mcp, "_tool_manager", None)
        d = getattr(tm, "_tools", None) if tm is not None else None
        return len(d) if isinstance(d, dict) else "?"


def _auth0_enabled() -> bool:
    return bool(
        settings.auth0_domain
        and settings.auth0_audience
        and settings.public_base_url
    )


def _print_banner(
    transport: str,
    host: str | None = None,
    port: int | None = None,
) -> None:
    try:
        import akshare

        ak_ver = akshare.__version__
    except Exception:  # noqa: BLE001
        ak_ver = "?"

    endpoint = (
        f"http://{host}:{port}{MCP_PATH}"
        if transport == "http"
        else "(stdio)"
    )

    if transport == "http" and _auth0_enabled():
        auth_mode = "Auth0 JWT"
    elif transport == "http" and settings.mcp_auth_token:
        auth_mode = "Bearer token"
    else:
        auth_mode = "OFF"

    banner = f"""
╭───────────────────────────────────────────────────────────╮
│  ashare-mcp  v{__version__}   (China A-share data MCP server)
│  transport : {transport:<10s}  endpoint: {endpoint}
│  http auth : {auth_mode:<10s}  tools   : {_tool_count()}
│  akshare   : {ak_ver}
│  cache db  : {settings.db_path}
│  log dir   : {settings.log_dir}  (level={settings.log_level})
│  tushare   : {"enabled" if settings.tushare_token else "disabled"}
╰───────────────────────────────────────────────────────────╯
"""
    print(banner, file=sys.stderr, flush=True)

    if _ver_tuple(ak_ver) < (1, 18, 0):
        logger.warning(
            f"akshare {ak_ver} < 1.18.0 — 建议升级: uv add 'akshare>=1.18.0'"
        )


class BearerAuthMiddleware:
    """Legacy static Bearer token middleware. /health remains public."""

    def __init__(self, app, token: str = "") -> None:
        self.app = app
        self.token = token or ""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive=receive)
        path = request.url.path.rstrip("/")

        if path.endswith("/health"):
            await JSONResponse(
                {
                    "status": "ok",
                    "service": "ashare-mcp",
                    "version": __version__,
                }
            )(scope, receive, send)
            return

        if self.token:
            header = request.headers.get("authorization", "")
            presented = (
                header[7:].strip()
                if header[:7].lower() == "bearer "
                else ""
            )

            if not (
                presented
                and hmac.compare_digest(presented, self.token)
            ):
                await JSONResponse(
                    {
                        "error": "unauthorized",
                        "detail": "missing or invalid bearer token",
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return

        await self.app(scope, receive, send)


def _run_http(host: str, port: int) -> None:
    import uvicorn
    from starlette.middleware import Middleware

    if _auth0_enabled():
        logger.info("HTTP transport: Auth0 JWT authentication ENABLED")

        # FastMCP handles JWT verification and OAuth protected-resource metadata.
        app = mcp.http_app(path=MCP_PATH)

    else:
        token = settings.mcp_auth_token

        if token:
            logger.info("HTTP transport: legacy Bearer auth ENABLED")
        else:
            logger.warning(
                "HTTP transport: authentication DISABLED — "
                "do not expose publicly without authentication!"
            )

        app = mcp.http_app(
            path=MCP_PATH,
            middleware=[
                Middleware(BearerAuthMiddleware, token=token)
            ],
        )

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
    )


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        prog="ashare-mcp",
        description=(
            "China A-share data MCP server "
            "(akshare/baostock; stdio & Streamable HTTP)."
        ),
    )

    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (local clients) or Streamable HTTP (remote clients).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind host (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9876,
        help="HTTP bind port (default 9876)",
    )

    args = parser.parse_args()

    _print_banner(args.transport, args.host, args.port)

    if args.transport == "http":
        _run_http(args.host, args.port)
    else:
        mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
