"""Shared helpers for tool modules: response builders, the ``@guard`` decorator,
limit clamping, and the standard disclaimer strings."""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

from loguru import logger

from .. import snapshot
from ..config import settings
from ..data_sources._base import DataUnavailableError, EmptyDataError

# --------------------------------------------------------------------------- #
# Disclaimers (PRD requires these to appear in the relevant tool outputs/descriptions)
# --------------------------------------------------------------------------- #
DISCLAIMER_FUNDFLOW = (
    "【口径警告】主力/超大单/大单/中单/小单是东方财富按单笔成交金额机械分桶估算,"
    "不同行情软件口径不一致,仅供参考,不代表真实机构意图。"
)
DISCLAIMER_CHIP = (
    "【口径警告】筹码分布/获利盘为概率模型估算,各家算法与口径不一,精度有限,仅供参考。"
)
DISCLAIMER_NORTH = (
    "【口径警告】北向资金自 2024-08-19 起官方取消盘中/日频买卖明细披露,"
    "仅能获取 T+1 成交总额、十大活跃成交股(无买卖拆分)、季度持仓(延迟约3个月)。"
)
DISCLAIMER_SOUTH = "南向资金(港股通)数据完整可用,未受 2024 调整影响。"
DISCLAIMER_OFFICIAL = "交易所/上市公司官方原始数据(通常 T+1),可靠。"
DISCLAIMER_NOT_ADVICE = "数据仅供研究,不构成投资建议。"


def ok(data: Any = None, **meta) -> dict:
    out: dict[str, Any] = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(meta)
    return out


def err(code: str, detail: str = "", suggestion: str = "") -> dict:
    return {"ok": False, "error": code, "detail": str(detail), "suggestion": suggestion}


def _snapshot_args(sig: inspect.Signature, a: tuple, k: dict) -> dict:
    """Bound arguments (defaults applied) used as the snapshot key."""
    try:
        bound = sig.bind(*a, **k)
        bound.apply_defaults()
        return dict(bound.arguments)
    except TypeError:
        return {"_a": list(a), "_k": dict(k)}


def guard(fn: Callable) -> Callable:
    """Wrap a tool so no exception ever escapes to the MCP framework.

    Returns a structured error dict on failure (PRD §错误处理). Preserves the wrapped
    signature/annotations so FastMCP can still infer the tool schema.

    Snapshots: a successful result is saved; when the data source is unavailable the
    last successful result of the same call is returned, labelled ``stale=True`` with
    its save time. Empty results (``no_data``) never fall back to a snapshot.
    """
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*a, **k):
        snap_args = _snapshot_args(sig, a, k)
        try:
            result = fn(*a, **k)
        except DataUnavailableError as e:
            logger.warning(f"{fn.__name__} data_source_unavailable: {e}")
            snap = snapshot.load(fn.__name__, snap_args)
            if snap is not None:
                logger.warning(f"{fn.__name__} serving stale snapshot saved at {snap.get('saved_at')}")
                return snapshot.as_stale(snap, f"data_source_unavailable: {e}")
            return err("data_source_unavailable", str(e),
                       "请稍后重试,或检查网络 / akshare 版本(可 uv run ashare-mcp 查看启动 banner)")
        except EmptyDataError as e:
            logger.info(f"{fn.__name__} no_data: {e}")
            return err("no_data", str(e), "该标的/日期可能无数据,请调整参数(如换一个交易日)")
        except (ValueError, KeyError, TypeError) as e:
            logger.info(f"{fn.__name__} bad_request: {type(e).__name__}: {e}")
            return err("bad_request", f"{type(e).__name__}: {e}",
                       "请检查参数格式(股票代码、日期 YYYY-MM-DD 等)")
        except Exception as e:  # noqa: BLE001 - never let it reach the framework
            logger.exception(f"{fn.__name__} internal_error")
            return err("internal_error", f"{type(e).__name__}: {e}", "请稍后重试")
        snapshot.save(fn.__name__, snap_args, result)
        return result

    wrapper.__wrapped__ = fn
    wrapper.__signature__ = sig
    return wrapper


def clamp_limit(limit: int | None, default: int | None = None, maximum: int = 5000) -> int:
    if limit is None:
        limit = default if default is not None else settings.default_limit
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = settings.default_limit
    return max(1, min(limit, maximum))
