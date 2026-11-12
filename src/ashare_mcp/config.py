
"""Configuration (pydantic-settings) + loguru logging setup.

Reads from environment / .env. Production OAuth storage requires Redis.
Runtime data (SQLite cache + logs) live under cache_dir.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

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

    # Runtime environment. Must be explicitly configured.
    environment: str

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

    # Persistent OAuth storage
    redis_url: str = ""
    storage_encryption_key: str = ""

    @field_validator("environment", mode="before")
    @classmethod
    def _coerce_environment(cls, value):
        if value is None or not str(value).strip():
            raise ValueError("ENVIRONMENT must be explicitly configured.")
        value = str(value).strip().lower()
        if value not in {"development", "production"}:
            raise ValueError("ENVIRONMENT must be development or production.")
        return value

    @field_validator("cache_dir", mode="before")
    @classmethod
    def _coerce_cache_dir(cls, value):
        if value is None or value == "":
            return Path.home() / ".ashare-mcp"
        return Path(str(value)).expanduser()

    @field_validator("log_level", mode="before")
    @classmethod
    def _coerce_level(cls, value):
        if not value:
            return "INFO"
        return str(value).upper()

    @field_validator("default_limit", mode="before")
    @classmethod
    def _coerce_limit(cls, value):
        if value is None or value == "":
            return 50
        try:
            return int(value)
        except (TypeError, ValueError):
            return 50

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "cache.db"

    @property
    def log_dir(self) -> Path:
        return self.cache_dir / "logs"


def validate_production_settings(config: Settings) -> None:
    """Fail fast when production OAuth or Redis configuration is incomplete."""

    if config.environment != "production":
        return

    required = {
        "AUTH0_DOMAIN": config.auth0_domain,
        "AUTH0_AUDIENCE": config.auth0_audience,
        "AUTH0_CLIENT_ID": config.auth0_client_id,
        "PUBLIC_BASE_URL": config.public_base_url,
        "JWT_SIGNING_KEY": config.jwt_signing_key,
        "REDIS_URL": config.redis_url,
        "STORAGE_ENCRYPTION_KEY": config.storage_encryption_key,
    }

    missing = [
        name for name, value in required.items()
        if not value or not value.strip()
    ]

    if missing:
        raise RuntimeError(
            "Missing required production environment variables: "
            + ", ".join(missing)
        )

    try:
        redis_address = urlsplit(config.redis_url.strip())
        redis_host = redis_address.hostname
        redis_port = redis_address.port
    except ValueError:
        raise RuntimeError(
            "REDIS_URL is invalid. Check its format in the Render environment."
        ) from None

    if (
        redis_address.scheme != "rediss"
        or not redis_host
        or not redis_port
    ):
        raise RuntimeError(
            "Production REDIS_URL must use rediss:// with a valid host and port."
        )


settings = Settings()
validate_production_settings(settings)

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
