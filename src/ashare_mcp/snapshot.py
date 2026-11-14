"""Last-good-result snapshots for tools.

When a data source fails, a tool can return the last successful result of the same
call (same tool + same arguments), clearly marked as stale and dated. Snapshots live
in Redis (the same REDIS_URL already used for OAuth storage) so they survive Render
free-tier sleeps and restarts; an in-memory copy is the fallback when Redis is
unavailable.

Design rules:
* Only successful results (``{"ok": True, ...}``) are saved.
* Realtime-style tools are never snapshotted (a stale quote is dangerous).
* Every Redis call has short timeouts and never raises into the caller.
* A stale result is always labelled: ``stale=True`` plus the save time and age.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from .config import settings

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_KEY_PREFIX = "ashare:snap:v1:"

# 4 days covers a Friday close being read on Monday morning with margin.
SNAPSHOT_TTL_SECONDS = 4 * 24 * 3600
_MAX_PAYLOAD_BYTES = 800_000
_SAVE_THROTTLE_SECONDS = 600
_REDIS_RETRY_AFTER_SECONDS = 60

# Tools whose data is only meaningful in the moment. Never serve these from a snapshot.
NO_SNAPSHOT_TOOLS = frozenset(
    {
        "get_realtime_quote",
        "get_minute_kline",
        "get_northbound_realtime",
        "get_stock_sentiment",
    }
)

_memory: dict[str, tuple[float, str]] = {}
_last_saved: dict[str, float] = {}
_client: Any = None
_client_failed_at: float = 0.0


def make_key(tool_name: str, args: dict) -> str:
    raw = tool_name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return _KEY_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _redis():
    """Return a Redis client, or None when unconfigured / recently failed."""
    global _client, _client_failed_at

    url = (getattr(settings, "redis_url", "") or "").strip()
    if not url:
        return None
    if _client is not None:
        return _client
    if time.time() - _client_failed_at < _REDIS_RETRY_AFTER_SECONDS:
        return None
    try:
        import redis  # installed through py-key-value-aio[redis]

        _client = redis.Redis.from_url(
            url,
            socket_connect_timeout=2,
            socket_timeout=3,
            decode_responses=True,
        )
        return _client
    except Exception as exc:  # noqa: BLE001
        _client_failed_at = time.time()
        logger.warning(f"snapshot: redis client unavailable: {type(exc).__name__}")
        return None


def _mark_redis_failed(action: str, exc: Exception) -> None:
    global _client, _client_failed_at
    _client = None
    _client_failed_at = time.time()
    logger.warning(f"snapshot: redis {action} failed: {type(exc).__name__}: {exc}")


def _now_iso() -> str:
    return datetime.now(_SHANGHAI).isoformat(timespec="seconds")


def save(tool_name: str, args: dict, result: Any) -> None:
    """Store a successful result. Never raises."""
    try:
        if tool_name in NO_SNAPSHOT_TOOLS:
            return
        if not (isinstance(result, dict) and result.get("ok") is True):
            return
        if result.get("stale"):
            return

        key = make_key(tool_name, args)
        now = time.time()
        if now - _last_saved.get(key, 0.0) < _SAVE_THROTTLE_SECONDS:
            return

        payload = json.dumps(
            {"saved_at": _now_iso(), "saved_ts": now, "result": result},
            ensure_ascii=False,
            default=str,
        )
        if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            logger.info(f"snapshot: {tool_name} result too large; not saved")
            return

        _memory[key] = (now, payload)
        _last_saved[key] = now

        client = _redis()
        if client is not None:
            try:
                client.set(key, payload, ex=SNAPSHOT_TTL_SECONDS)
            except Exception as exc:  # noqa: BLE001
                _mark_redis_failed("set", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"snapshot: save skipped: {type(exc).__name__}: {exc}")


def _load_raw(key: str) -> str | None:
    client = _redis()
    if client is not None:
        try:
            value = client.get(key)
            if value:
                return value
        except Exception as exc:  # noqa: BLE001
            _mark_redis_failed("get", exc)

    item = _memory.get(key)
    if item and time.time() - item[0] <= SNAPSHOT_TTL_SECONDS:
        return item[1]
    return None


def load(tool_name: str, args: dict) -> dict | None:
    """Return ``{"saved_at", "saved_ts", "result"}`` or None. Never raises."""
    try:
        if tool_name in NO_SNAPSHOT_TOOLS:
            return None
        raw = _load_raw(make_key(tool_name, args))
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
            return None
        saved_ts = float(data.get("saved_ts") or 0)
        if saved_ts and time.time() - saved_ts > SNAPSHOT_TTL_SECONDS:
            return None
        return data
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"snapshot: load skipped: {type(exc).__name__}: {exc}")
        return None


def as_stale(snapshot: dict, reason: str) -> dict:
    """Build the labelled stale response from a loaded snapshot."""
    out = dict(snapshot["result"])
    saved_ts = float(snapshot.get("saved_ts") or 0)
    age_hours = round((time.time() - saved_ts) / 3600, 1) if saved_ts else None

    # Fields that described the original fetch and are no longer true.
    out.pop("age_calendar_days", None)

    out["stale"] = True
    out["freshness"] = "stale_snapshot"
    out["snapshot_saved_at"] = snapshot.get("saved_at")
    out["snapshot_age_hours"] = age_hours
    out["stale_reason"] = reason[:300]
    out["stale_warning"] = (
        "实时取数失败，以下为上次成功取得的同参数结果，不是当前数据。"
        "使用时必须按 snapshot_saved_at 标注数据时间，不得当作当日实时数据。"
    )
    return out
