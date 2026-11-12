from __future__ import annotations

import json
import multiprocessing
import re
import socket
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import akshare as ak
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

TZ = ZoneInfo("Asia/Shanghai")
STOCK = "sh600519"
REPORT = {
    "started_at": datetime.now(TZ).isoformat(),
    "akshare_version": getattr(ak, "__version__", "unknown"),
    "python": __import__("sys").version.split()[0],
    "checks": [],
    "notes": [
        "Render-only diagnostic runner; does not import or modify production MCP configuration.",
        "Each data interface is called three consecutive times. first_request_seconds records the first call.",
        "data_date is reported only when the returned payload contains an identifiable date; request time is never substituted.",
    ],
    "complete": False,
}
LOCK = threading.Lock()


def _dates(value):
    found = set()
    if isinstance(value, dict):
        for v in value.values():
            found.update(_dates(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.update(_dates(v))
    elif isinstance(value, str):
        for m in re.findall(r"(?<!\d)(20\d{2}[-/]?\d{2}[-/]?\d{2})(?!\d)", value):
            s = m.replace("/", "").replace("-", "")
            try:
                found.add(datetime.strptime(s, "%Y%m%d").date().isoformat())
            except ValueError:
                pass
    return found


def _frame_meta(obj):
    if hasattr(obj, "empty") and hasattr(obj, "columns"):
        if obj.empty:
            return 0, None, list(map(str, obj.columns))
        cols = list(map(str, obj.columns))
        date_cols = [c for c in cols if any(k in c.lower() for k in ("日期", "时间", "date", "day", "trade"))]
        found = set()
        for c in date_cols:
            try:
                found.update(_dates(obj[c].astype(str).tolist()))
            except Exception:
                pass
        if not found:
            try:
                found.update(_dates(obj.tail(5).astype(str).values.tolist()))
            except Exception:
                pass
        return len(obj), max(found) if found else None, cols[:24]
    if isinstance(obj, (list, tuple, dict)):
        return len(obj), (max(_dates(obj)) if _dates(obj) else None), []
    return (1 if obj is not None else 0), None, []


def _record(name, fn, source, notes=""):
    attempts = []
    first = None
    for i in range(3):
        started = time.perf_counter()
        try:
            obj = fn()
            elapsed = round(time.perf_counter() - started, 3)
            if first is None:
                first = elapsed
            count, data_date, columns = _frame_meta(obj)
            ok = count > 0
            attempts.append({
                "attempt": i + 1,
                "ok": ok,
                "seconds": elapsed,
                "rows": count,
                "data_date": data_date,
                "columns": columns,
                "error": None if ok else "empty_result",
            })
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 3)
            if first is None:
                first = elapsed
            attempts.append({
                "attempt": i + 1,
                "ok": False,
                "seconds": elapsed,
                "rows": 0,
                "data_date": None,
                "columns": [],
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            })
    successes = sum(1 for a in attempts if a["ok"])
    item = {
        "interface": name,
        "source": source,
        "first_request_seconds": first,
        "success_count_3": successes,
        "all_three_success": successes == 3,
        "latest_data_date": max((a["data_date"] for a in attempts if a["data_date"]), default=None),
        "attempts": attempts,
        "notes": notes,
    }
    with LOCK:
        REPORT["checks"].append(item)
    print("[PROBE] " + json.dumps(item, ensure_ascii=False), flush=True)


def _child_ak_call(name, kwargs, out_queue):
    try:
        import akshare as child_ak
        obj = getattr(child_ak, name)(**kwargs)
        count, data_date, columns = _frame_meta(obj)
        out_queue.put({
            "ok": count > 0,
            "rows": count,
            "data_date": data_date,
            "columns": columns,
            "error": None if count > 0 else "empty_result",
        })
    except BaseException as exc:
        out_queue.put({
            "ok": False,
            "rows": 0,
            "data_date": None,
            "columns": [],
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        })


def _record_ak(name, kwargs, source, notes="", timeout_seconds=25):
    attempts = []
    first = None
    ctx = multiprocessing.get_context("spawn")
    for i in range(3):
        started = time.perf_counter()
        q = ctx.Queue()
        p = ctx.Process(target=_child_ak_call, args=(name, kwargs, q))
        p.start()
        p.join(timeout_seconds)
        elapsed = round(time.perf_counter() - started, 3)
        if first is None:
            first = elapsed
        if p.is_alive():
            p.terminate()
            p.join(3)
            result = {
                "ok": False, "rows": 0, "data_date": None, "columns": [],
                "error": f"TimeoutError: interface exceeded {timeout_seconds}s",
            }
        else:
            try:
                result = q.get(timeout=1)
            except Exception:
                result = {
                    "ok": False, "rows": 0, "data_date": None, "columns": [],
                    "error": f"WorkerExit: exitcode={p.exitcode}, no result",
                }
        attempts.append({
            "attempt": i + 1,
            "ok": result["ok"],
            "seconds": elapsed,
            "rows": result["rows"],
            "data_date": result["data_date"],
            "columns": result["columns"],
            "error": result["error"],
        })
        q.close()
        p.close()
    successes = sum(1 for a in attempts if a["ok"])
    item = {
        "interface": name,
        "source": source,
        "first_request_seconds": first,
        "success_count_3": successes,
        "all_three_success": successes == 3,
        "latest_data_date": max((a["data_date"] for a in attempts if a["data_date"]), default=None),
        "attempts": attempts,
        "notes": notes,
    }
    with LOCK:
        REPORT["checks"].append(item)
    print("[PROBE] " + json.dumps(item, ensure_ascii=False), flush=True)


def _probe_ths_board():
    _record(
        "stock_board_industry_summary_ths",
        lambda: ak.stock_board_industry_summary_ths(),
        "同花顺",
        "批量行业板块概况；已返回90条，包含涨跌幅和净流入字段。"
    )
    # AkShare has no verified bulk real-time THS concept-board ranking function.
    REPORT["notes"].append(
        "未把 stock_board_concept_summary_ths 当作概念涨幅榜：该接口是概念时间表，不是实时涨幅排名。"
    )


def _probe_ths_flows():
    tests = [
        ("stock_fund_flow_concept", lambda: ak.stock_fund_flow_concept(symbol="即时"), "同花顺概念资金流"),
        ("stock_fund_flow_industry", lambda: ak.stock_fund_flow_industry(symbol="即时"), "同花顺行业资金流"),
        ("stock_fund_flow_individual", lambda: ak.stock_fund_flow_individual(symbol="即时"), "同花顺个股资金流排名"),
    ]
    for name, fn, source in tests:
        if callable(getattr(ak, name, None)):
            _record_ak(name, {"symbol": "即时"}, source, "按 AkShare 当前公开接口名调用；返回日期缺失时不推断所属交易日。")
        else:
            _record(name, lambda: (_ for _ in ()).throw(RuntimeError("function_missing")), source, "当前安装版本未发现该函数。")


def _probe_sina_minute():
    fn = getattr(ak, "stock_zh_a_minute", None)
    if callable(fn):
        _record_ak("stock_zh_a_minute", {"symbol": STOCK, "period": "5", "adjust": ""}, "新浪", "新浪分钟K；返回时间列用于核对最近交易日。")
    else:
        _record("stock_zh_a_minute", lambda: (_ for _ in ()).throw(RuntimeError("function_missing")), "新浪")


def _probe_tencent_minute():
    url = "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline"
    params = {"param": f"{STOCK},m5,,320"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    def call():
        r = requests.get(url, params=params, headers=headers, timeout=(5, 12))
        r.raise_for_status()
        data = r.json()
        node = (data.get("data") or {}).get(STOCK) or {}
        rows = node.get("m5") or node.get("m5") or []
        if not rows:
            raise RuntimeError("Tencent response contains no m5 rows")
        return rows
    _record("Tencent web.ifzq m5 minute K", call, "腾讯", "腾讯公开分钟K接口；需检查返回日期和成交量单位。")


def _probe_tdx():
    servers = [
        ("119.147.212.81", 7709),
        ("119.147.164.60", 7709),
        ("124.71.187.122", 7709),
    ]
    attempts = []
    for host, port in servers:
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=4):
                attempts.append({"host": host, "port": port, "ok": True, "seconds": round(time.perf_counter()-t0, 3), "error": None})
        except Exception as exc:
            attempts.append({"host": host, "port": port, "ok": False, "seconds": round(time.perf_counter()-t0, 3), "error": f"{type(exc).__name__}: {str(exc)[:250]}"})
    item = {
        "interface": "通达信 TCP connectivity",
        "source": "通达信公开行情节点",
        "first_request_seconds": attempts[0]["seconds"] if attempts else None,
        "success_count_3": sum(1 for a in attempts if a["ok"]),
        "all_three_success": all(a["ok"] for a in attempts),
        "latest_data_date": None,
        "attempts": attempts,
        "notes": "仅验证 Render 出站 TCP 连通性，不代表已完成通达信协议握手或行情解析。",
    }
    with LOCK:
        REPORT["checks"].append(item)
    print("[PROBE] " + json.dumps(item, ensure_ascii=False), flush=True)


def _probe_baostock():
    try:
        import baostock as bs
    except Exception as exc:
        _record("BaoStock daily K after-close fallback", lambda exc=exc: (_ for _ in ()).throw(exc), "BaoStock", "依赖导入失败。")
        return
    login = bs.login()
    if getattr(login, "error_code", "1") != "0":
        _record("BaoStock daily K after-close fallback", lambda: (_ for _ in ()).throw(RuntimeError(f"login {login.error_code}: {login.error_msg}")), "BaoStock", "仅盘后兜底；登录失败。")
        return
    def query():
        rs = bs.query_history_k_data_plus(
            "sh.600519",
            "date,code,open,high,low,close,volume,amount",
            start_date=(datetime.now(TZ) - timedelta(days=12)).strftime("%Y-%m-%d"),
            end_date=datetime.now(TZ).strftime("%Y-%m-%d"),
            frequency="d",
            adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"{rs.error_code}: {rs.error_msg}")
        if not rows:
            raise RuntimeError("BaoStock returned no daily rows")
        return [{"date": r[0]} for r in rows]
    try:
        _record("BaoStock daily K after-close fallback", query, "BaoStock", "仅盘后兜底链；不用于盘中分钟K或实时行情。")
    finally:
        bs.logout()


def run_probes():
    print("[PROBE] starting Render data-source checks", flush=True)
    _probe_ths_board()
    _probe_ths_flows()
    _probe_sina_minute()
    _probe_tencent_minute()
    _probe_tdx()
    _probe_baostock()
    with LOCK:
        REPORT["completed_at"] = datetime.now(TZ).isoformat()
        REPORT["complete"] = True
    print("[PROBE] COMPLETE " + json.dumps(REPORT, ensure_ascii=False), flush=True)


async def health(request):
    return JSONResponse({"status": "ok", "probe_complete": REPORT["complete"]})


async def results(request):
    with LOCK:
        return JSONResponse(REPORT)


app = Starlette(routes=[
    Route("/health", health),
    Route("/results", results),
])


if __name__ == "__main__":
    threading.Thread(target=run_probes, daemon=True).start()
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
