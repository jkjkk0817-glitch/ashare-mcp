"""SQLite-backed KV cache with TTL, plus a ``@cached`` decorator and TTL policies.

Table: cache(key TEXT PRIMARY KEY, value BLOB, expires_at INTEGER, created_at INTEGER)
Key:   sha256(tool_name + json.dumps(args, sort_keys=True))
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import pickle
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from loguru import logger

from .config import settings


class SQLiteCache:
    def __init__(self, db_path: str | Any) -> None:
        self.db_path = str(db_path)
        self._local = threading.local()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache ("
                "key TEXT PRIMARY KEY, value BLOB, "
                "expires_at INTEGER, created_at INTEGER)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at)")
            try:
                c.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            self._local.conn = c
        return c

    @staticmethod
    def make_key(name: str, args: dict) -> str:
        raw = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        now = int(time.time())
        try:
            row = self._conn().execute(
                "SELECT value, expires_at FROM cache WHERE key=?", (key,)
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning(f"cache get error: {e}")
            return None
        if not row:
            return None
        value, expires_at = row
        if expires_at is not None and expires_at <= now:
            try:
                self._conn().execute("DELETE FROM cache WHERE key=?", (key,))
                self._conn().commit()
            except sqlite3.Error:
                pass
            return None
        try:
            return pickle.loads(value)
        except Exception:
            return None

    def set(self, key: str, value: Any, ttl_seconds: int | None) -> None:
        now = int(time.time())
        expires_at = None if ttl_seconds is None else now + int(ttl_seconds)
        try:
            blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            self._conn().execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, blob, expires_at, now),
            )
            self._conn().commit()
        except Exception as e:
            logger.warning(f"cache set error: {e}")

    def clear_expired(self) -> int:
        now = int(time.time())
        try:
            cur = self._conn().execute(
                "DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            )
            self._conn().commit()
            return cur.rowcount
        except sqlite3.Error:
            return 0

    def stats(self) -> dict:
        try:
            total = self._conn().execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            unexpired = self._conn().execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at IS NULL OR expires_at > ?",
                (int(time.time()),),
            ).fetchone()[0]
            return {"entries": total, "unexpired": unexpired, "db_path": self.db_path}
        except sqlite3.Error:
            return {"entries": 0, "unexpired": 0, "db_path": self.db_path}


cache = SQLiteCache(settings.db_path)


# --------------------------------------------------------------------------- #
# TTL policies (see PRD §缓存策略)
# --------------------------------------------------------------------------- #
_SHANGHAI = ZoneInfo("Asia/Shanghai")

# A 股连续竞价时段（含集合竞价和收盘后短暂缓冲）: 09:15-11:35, 13:00-15:05
_SESSION_WINDOWS = ((9 * 60 + 15, 11 * 60 + 35), (13 * 60, 15 * 60 + 5))


def now_cn() -> datetime:
    """北京时间当前时刻（Render 服务器是 UTC，不能直接用 datetime.now()）。"""
    return datetime.now(_SHANGHAI)


def _is_trade_day_cn(day: date) -> bool:
    try:
        from .utils import dates as _dates

        return bool(_dates.is_trade_day(day))
    except Exception:  # noqa: BLE001 - calendar unavailable: weekday approximation
        return day.weekday() < 5


def in_trading_session(now: datetime | None = None) -> bool:
    """是否处于交易时段（北京时间，交易日 09:15-11:35 / 13:00-15:05）。"""
    now = now or now_cn()
    if not _is_trade_day_cn(now.date()):
        return False
    minutes = now.hour * 60 + now.minute
    return any(start <= minutes <= end for start, end in _SESSION_WINDOWS)


def _secs_until_next(hour: int, minute: int = 0) -> int:
    now = now_cn()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return max(1, int((target - now).total_seconds()))


TTL_REALTIME = 10          # realtime quote / level-2 bid-ask
TTL_MINUTE = 300           # minute kline
TTL_FINANCIAL = 6 * 3600   # financial reports / announcements / research
TTL_INFO = 24 * 3600       # slow-changing metadata (stock info)
TTL_INTRADAY_FLOW = 180    # 盘中资金流/板块排名: 3 分钟
TTL_INTRADAY_KLINE = 60    # 盘中含当日K线的数据: 1 分钟


def ttl_daily_kline(args: dict) -> int | None:
    """Daily kline: permanent if end_date strictly before today (Beijing time);
    1 minute while the market is open; otherwise valid until next 09:00 Beijing time."""
    end = (args.get("end_date") or "").replace("-", "")
    today = now_cn().strftime("%Y%m%d")
    if end and end < today:
        return None  # permanent until manual clear
    if in_trading_session():
        return TTL_INTRADAY_KLINE
    return _secs_until_next(9, 0)


def ttl_intraday_official(args: dict | None = None) -> int:
    """LHB / margin / chip / fund-flow (Beijing time): market open -> 3 min;
    before 18:00 -> 30 min; after 18:00 -> until next 09:00."""
    if in_trading_session():
        return TTL_INTRADAY_FLOW
    if now_cn().hour < 18:
        return 1800
    return _secs_until_next(9, 0)


def ttl_session_aware_daily(args: dict | None = None) -> int:
    """含当日K线的数据包: 盘中 1 分钟；非盘中沿用 ttl_intraday_official。"""
    if in_trading_session():
        return TTL_INTRADAY_KLINE
    return ttl_intraday_official(args)


def cached(ttl: int | None | Callable[[dict], int | None]) -> Callable:
    """Decorator: cache a tool's return value.

    ``ttl`` may be an int (seconds), ``None`` (permanent), or a callable taking the
    bound-argument dict and returning seconds/None. Error dicts ({"error": ...}) are
    never cached. ``functools.wraps`` + explicit ``__signature__`` keep the original
    signature/annotations visible so FastMCP can still infer the tool schema.
    """

    def deco(fn: Callable) -> Callable:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                bound = sig.bind(*a, **k)
                bound.apply_defaults()
                key_args = dict(bound.arguments)
            except TypeError:
                key_args = {"_a": list(a), "_k": dict(k)}
            key = SQLiteCache.make_key(fn.__name__, key_args)
            hit = cache.get(key)
            if hit is not None:
                logger.debug(f"cache HIT  {fn.__name__} [{key[:10]}]")
                return hit
            result = fn(*a, **k)
            seconds = ttl(key_args) if callable(ttl) else ttl
            if not (isinstance(result, dict) and result.get("error")):
                cache.set(key, result, seconds)
                logger.debug(f"cache MISS {fn.__name__} [{key[:10]}] ttl={seconds}")
            return result

        wrapper.__wrapped__ = fn
        wrapper.__signature__ = sig
        return wrapper

    return deco
