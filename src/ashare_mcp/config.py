
"""Configuration (pydantic-settings) + loguru logging setup.

Reads from environment / .env. All values optional with sensible defaults.
Runtime data (SQLite cache + logs) live under cache_dir.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    tushare_token: str = ""
    cache_dir: Path = Field(default_factory=lambda: Path.home() / ".ashare-mcp")
    log_level: str = "INFO"
    mcp_auth_token: str = ""
    default_limit: int = 50

    # Auth0 OAuth / OIDC configuration
    auth0_domain: str = ""
    auth0_audience: str = ""
    auth0_client_id: str = ""
    public_base_url: str = ""
    jwt_signing_key: str = ""

    @field_validator("cache_dir", mode="before")
    @classmethod
    def _coerce_cache_dir(cls, v):
        if v is None or v == "":
            return Path.home() / ".ashare-mcp"
        return Path(str(v)).expanduser()

    @field_validator("log_level", mode="before")
    @classmethod
    def _coerce_level(cls, v):
        if not v:
            return "INFO"
        return str(v).upper()

    @field_validator("default_limit", mode="before")
    @classmethod
    def _coerce_limit(cls, v):
        if v is None or v == "":
            return 50
        try:
            return int(v)
        except (TypeError, ValueError):
            return 50

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "cache.db"

    @property
    def log_dir(self) -> Path:
        return self.cache_dir / "logs"


settings = Settings()
settings.cache_dir.mkdir(parents=True, exist_ok=True)
settings.log_dir.mkdir(parents=True, exist_ok=True)


_LOG_CONFIGURED = False


def setup_logging() -> None:
    """Configure loguru once.

    Logs go to stderr and a rotating file, never stdout.
    """
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        backtrace=False,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
    )
    logger.add(
        str(settings.log_dir / "server.log"),
        level=settings.log_level,
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )
    _LOG_CONFIGURED = True


setup_logging()
