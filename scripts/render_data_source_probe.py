from __future__ import annotations

import inspect
import json
import multiprocessing
import os
import re
import threading
import time
from datetime import datetime, timedelta, time as clock_time
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import akshare as ak
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

TZ = ZoneInfo("Asia/Shanghai")
STOCKS_PENDING = True
SCHEDULE = {
    "2026-11-13": {"intraday": (clock_time(10, 0), clock_time(10, 30)),
                   "after_close": (clock_time(15, 10), clock_time(15, 35))},
    "2026-11-16": {"intraday": (clock_time(10, 0), clock_time(10, 30)),
                   "after_close": (clock_time(15, 10), clock_time(15, 35))},
    "2026-11-17": {"intraday": (clock_time(10, 0), clock_time(10, 30)),
                   "after_close": (clock_time(15, 10), clock_time(15, 35))},
}
REPORT = {
    "service": "Render-only board and fund-flow validation",
    "started_at": datetime.now(TZ).isoformat(),
    "akshare_version": getattr(ak, "__version__", "unknown"),
    "python": __import__("sys").version.split()[0],
    "sessions": [],
    "notes": [
        "Only THS/EM board ranking and sector fund-flow interfaces are tested in this phase.",
        "Minute K, TDX, and BaoStock checks are intentionally paused.",
        "No project Redis cache is bypassed by pretending to disable upstream/CDN caches; direct means no project Redis read.",
        "A missing interface date is inferred only when Sina Shanghai Composite daily K and THS industry daily-index sample agree; no Eastmoney trading calendar is used.",
        "session_final is false intraday and becomes true only after close when both independent daily sources confirm the current trading date.",
        "Redis cache timing is measured only when PROBE_REDIS_URL is configured; otherwise cache timing is marked skipped.",
    ],
}
LOCK = threading.Lock()
RUNNING = set()
COMPLETED = set()


def _date_from_any(value):
    if value is None:
        return None
    try:
        return pd.to_datetime(value).date().isoformat()
    except Exception:
        text = str(value)
        match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", text)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _frame_meta(obj):
    if isinstance(obj, pd.DataFrame):
        if obj.empty:
            return 0, None, list(map(str, obj.columns)), "[]"
        cols = list(map(str, obj.columns))
        date_cols = [c for c in cols if any(k in c.lower() for k in ("日期", "时间", "date", "day", "trade"))]
        dates = []
        for col in date_cols:
            for value in obj[col].astype(str).tolist():
                d = _date_from_any(value)
                if d:
                    dates.append(d)
        data_date = max(dates) if dates else None
        payload = obj.to_json(orient="records", force_ascii=False, date_format="iso")
        return len(obj), data_date, cols[:30], payload
    if isinstance(obj, (list, tuple)):
        payload = json.dumps(obj, ensure_ascii=False, default=str)
        return len(obj), None, [], payload
    if isinstance(obj, dict):
        payload = json.dumps(obj, ensure_ascii=False, default=str)
        return len(obj), None, list(obj.keys())[:30], payload
    raise TypeError(f"unsupported result type: {type(obj).__name__}")


def _child_call(function_name, kwargs, out_queue):
    try:
        import time as _time
        import akshare as _ak
        started = _time.perf_counter()
        obj = getattr(_ak, function_name)(**kwargs)
        call_seconds = round(_time.perf_counter() - started, 3)
        rows, data_date, columns, payload = _frame_meta(obj)
        out_queue.put({
            "ok": rows > 0,
            "call_seconds": call_seconds,
            "rows": rows,
            "data_date": data_date,
            "columns": columns,
            "payload": payload,
            "error": None if rows > 0 else "empty_result",
        })
    except BaseException as exc:
        out_queue.put({
            "ok": False, "call_seconds": None, "rows": 0, "data_date": None,
            "columns": [], "payload": None,
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        })


def _isolated_call(function_name, kwargs, timeout_seconds=60):
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=_child_call, args=(function_name, kwargs, q))
    p.start()
    p.join(timeout_seconds)
    if p.is_alive():
        p.terminate()
        p.join(3)
        result = {"ok": False, "call_seconds": None, "rows": 0, "data_date": None,
                  "columns": [], "payload": None,
                  "error": f"TimeoutError: exceeded {timeout_seconds}s"}
    else:
        try:
            result = q.get(timeout=2)
        except Exception:
            result = {"ok": False, "call_seconds": None, "rows": 0, "data_date": None,
                      "columns": [], "payload": None,
                      "error": f"WorkerExit: exitcode={p.exitcode}, no result"}
    q.close()
    p.close()
    return result


def _calendar_and_ths_sample():
    now = datetime.now(TZ)
    today = now.date()
    start = (today - timedelta(days=12)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    evidence = {
        "sina_shanghai_composite_daily": None,
        "ths_industry_sample": None,
        "inferred_date": None,
        "date_source": "unknown",
        "session_final": False,
        "session_final_reason": "not_confirmed",
        "error": None,
    }

    # Shanghai Composite daily K-line from Sina (AkShare). This is the
    # independent latest-trading-day reference; no Eastmoney calendar is used.
    try:
        index_result = _isolated_call(
            "stock_zh_index_daily",
            {"symbol": "sh000001"},
            timeout_seconds=60,
        )
        if index_result.get("ok") and index_result.get("payload"):
            records = json.loads(index_result["payload"])
            date_values = []
            for row in records:
                if isinstance(row, dict):
                    for key in ("date", "日期", "trade_date", "交易日期"):
                        if row.get(key) is not None:
                            parsed = _date_from_any(row[key])
                            if parsed:
                                date_values.append(parsed)
            index_result["data_date"] = max(date_values) if date_values else None
        evidence["sina_shanghai_composite_daily"] = {
            "symbol": "sh000001",
            "latest_date": index_result.get("data_date"),
            "rows": index_result.get("rows", 0),
            "error": index_result.get("error"),
        }
    except Exception as exc:
        evidence["sina_shanghai_composite_daily"] = {
            "error": f"{type(exc).__name__}: {str(exc)[:300]}"
        }

    # THS industry daily-index sample, independently queried.
    try:
        sample_result = _isolated_call(
            "stock_board_industry_index_ths",
            {"symbol": "半导体", "start_date": start, "end_date": end},
            timeout_seconds=60,
        )
        evidence["ths_industry_sample"] = {
            "symbol": "半导体",
            "latest_date": sample_result.get("data_date"),
            "columns": sample_result.get("columns", []),
            "rows": sample_result.get("rows", 0),
            "error": sample_result.get("error"),
        }
    except Exception as exc:
        evidence["ths_industry_sample"] = {
            "error": f"{type(exc).__name__}: {str(exc)[:300]}"
        }

    reference = evidence.get("sina_shanghai_composite_daily") or {}
    sample = evidence.get("ths_industry_sample") or {}
    latest_index = reference.get("latest_date")
    latest_ths = sample.get("latest_date")
    if latest_index and latest_ths and latest_index == latest_ths:
        evidence["inferred_date"] = latest_index
        evidence["date_source"] = "inferred"
        if now.time() >= clock_time(15, 5) and latest_index == today.isoformat():
            evidence["session_final"] = True
            evidence["session_final_reason"] = "after_close_and_both_sources_confirm_today"
        elif now.time() < clock_time(15, 5):
            evidence["session_final_reason"] = "intraday_not_final"
        else:
            evidence["session_final_reason"] = "after_close_but_sources_not_on_today"
    else:
        evidence["error"] = (
            "Sina Shanghai Composite daily K and THS industry daily-index sample "
            "do not agree; inferred date left unknown"
        )
    return evidence

def _source_evidence(function_name, expected_domain):
    try:
        fn = getattr(ak, function_name)
        source = inspect.getsource(fn)
        domains = sorted(set(re.findall(r"(?:https?://)?(?:[\w.-]+\.)?(?:10jqka\.com\.cn|eastmoney\.com)[\w./?=&%-]*", source)))
        module = getattr(fn, "__module__", None)
        expected_found = expected_domain in source
        return {
            "module": module, "expected_domain": expected_domain,
            "source_contains_expected_domain": expected_found,
            "domain_literals": domains,
            "source_excerpt": source[:1800],
            "status": "verified_in_function_source" if expected_found else "not_verified_from_function_source",
        }
    except Exception as exc:
        return {"status": "source_inspection_failed", "error": f"{type(exc).__name__}: {str(exc)[:250]}"}


def _redis_client():
    url = os.environ.get("PROBE_REDIS_URL")
    if not url:
        return None, "PROBE_REDIS_URL not configured"
    try:
        import redis
        client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=4, socket_timeout=4)
        client.ping()
        return client, "connected"
    except Exception as exc:
        return None, f"Redis connection failed: {type(exc).__name__}: {str(exc)[:250]}"


def _test_one_interface(session_id, spec, date_evidence, redis_client):
    attempts = []
    last_good = None
    for i in range(3):
        result = _isolated_call(spec["function"], spec["kwargs"])
        attempts.append({
            "attempt": i + 1, "ok": result["ok"],
            "source_call_seconds": result["call_seconds"],
            "rows": result["rows"], "source_data_date": result["data_date"],
            "error": result["error"],
        })
        if result["ok"]:
            last_good = result

    source_dates = [a["source_data_date"] for a in attempts if a["source_data_date"]]
    if source_dates:
        data_date = max(source_dates)
        date_source = "source"
    else:
        data_date = date_evidence.get("inferred_date")
        date_source = date_evidence.get("date_source", "unknown") if data_date else "unknown"

    now = datetime.now(TZ).isoformat()
    item = {
        "interface": spec["label"], "function": spec["function"],
        "source_claim": spec["source"], "source_evidence": _source_evidence(
            spec["function"], spec["expected_domain"]
        ),
        "session_id": session_id, "attempts": attempts,
        "success_count_3": sum(1 for a in attempts if a["ok"]),
        "first_source_call_seconds": next((a["source_call_seconds"] for a in attempts if a["source_call_seconds"] is not None), None),
        "data_date": data_date, "date_source": date_source,
        "date_evidence": date_evidence, "fetched_at": now,
        "cache": {"status": "skipped", "reason": "no successful source response" if not last_good else None},
    }

    if last_good and redis_client:
        key = f"ashare_probe:{session_id}:{spec['key']}"
        envelope = {
            "data": json.loads(last_good["payload"]),
            "data_date": data_date,
            "date_source": date_source,
            "fetched_at": now,
            "source": spec["source"],
        }
        try:
            t0 = time.perf_counter()
            redis_client.setex(key, 3600, json.dumps(envelope, ensure_ascii=False, default=str))
            write_seconds = round(time.perf_counter() - t0, 6)
            hit_times = []
            for _ in range(3):
                t0 = time.perf_counter()
                cached = redis_client.get(key)
                if cached is None:
                    raise RuntimeError("cache key missing after SETEX")
                json.loads(cached)
                hit_times.append(round(time.perf_counter() - t0, 6))
            item["cache"] = {
                "status": "redis_tested", "write_seconds": write_seconds,
                "hit_seconds": hit_times, "first_hit_seconds": hit_times[0],
                "average_hit_seconds": round(sum(hit_times) / len(hit_times), 6),
                "ttl_seconds": 3600, "cache_key": key,
                "fetched_at_preserved": json.loads(cached).get("fetched_at") == now,
            }
        except Exception as exc:
            item["cache"] = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    elif last_good:
        item["cache"] = {"status": "skipped", "reason": "Redis not configured"}

    print("[SESSION_PROBE] " + json.dumps(item, ensure_ascii=False), flush=True)
    return item


def run_session(session_id):
    now = datetime.now(TZ)
    date_evidence = _calendar_and_ths_sample()
    redis_client, redis_status = _redis_client()
    specs = [
        {"key": "ths_industry_board", "label": "THS行业板块榜", "function": "stock_board_industry_summary_ths",
         "kwargs": {}, "source": "同花顺", "expected_domain": "10jqka.com.cn"},
        {"key": "em_industry_board", "label": "EM行业板块榜", "function": "stock_board_industry_name_em",
         "kwargs": {}, "source": "东方财富", "expected_domain": "eastmoney.com"},
        {"key": "ths_industry_flow", "label": "THS行业资金流", "function": "stock_fund_flow_industry",
         "kwargs": {"symbol": "即时"}, "source": "同花顺", "expected_domain": "10jqka.com.cn"},
        {"key": "em_industry_flow", "label": "EM行业资金流", "function": "stock_sector_fund_flow_rank",
         "kwargs": {"indicator": "今日", "sector_type": "行业资金流"}, "source": "东方财富", "expected_domain": "eastmoney.com"},
        {"key": "ths_concept_flow", "label": "THS概念资金流", "function": "stock_fund_flow_concept",
         "kwargs": {"symbol": "即时"}, "source": "同花顺", "expected_domain": "10jqka.com.cn"},
        {"key": "em_concept_flow", "label": "EM概念资金流", "function": "stock_sector_fund_flow_rank",
         "kwargs": {"indicator": "今日", "sector_type": "概念资金流"}, "source": "东方财富", "expected_domain": "eastmoney.com"},
    ]
    results = []
    print(f"[SESSION] START {session_id} at {now.isoformat()} Redis={redis_status}", flush=True)
    for spec in specs:
        results.append(_test_one_interface(session_id, spec, date_evidence, redis_client))
    session_result = {
        "session_id": session_id, "started_at": now.isoformat(),
        "completed_at": datetime.now(TZ).isoformat(),
        "date_evidence": date_evidence,\n        "session_final": bool(date_evidence.get("session_final")) if session_id.endswith(":after_close") else False,
        "redis_status": redis_status, "checks": results,
        "individual_stock_flow": {
            "status": "pending_user_stock_codes",
            "note": "Full-market stock_fund_flow_individual is abandoned after 3/3 25-second timeouts. Candidate is one-by-one stock_individual_fund_flow(stock, market), pending a small user-specified code list."
        },
    }
    with LOCK:
        REPORT["sessions"].append(session_result)
        COMPLETED.add(session_id)
        RUNNING.discard(session_id)
    print("[SESSION] COMPLETE " + json.dumps(session_result, ensure_ascii=False), flush=True)


def _maybe_start_scheduled_session():
    now = datetime.now(TZ)
    day = now.date().isoformat()
    slots = SCHEDULE.get(day, {})
    for slot, (start, end) in slots.items():
        if start <= now.time().replace(tzinfo=None) <= end:
            session_id = f"{day}:{slot}"
            redis_client, _ = _redis_client()
            with LOCK:
                if session_id in RUNNING or session_id in COMPLETED:
                    return
                if redis_client:
                    try:
                        acquired = redis_client.set(
                            f"ashare_probe:scheduled:{session_id}", "started",
                            nx=True, ex=7200
                        )
                        if not acquired:
                            COMPLETED.add(session_id)
                            return
                    except Exception:
                        pass
                RUNNING.add(session_id)
            threading.Thread(target=run_session, args=(session_id,), daemon=True).start()
            return


async def health(request):
    _maybe_start_scheduled_session()
    with LOCK:
        running = sorted(RUNNING)
        completed = sorted(COMPLETED)
    return JSONResponse({
        "status": "ok", "service": "board-flow-probe",
        "scheduled_days": sorted(SCHEDULE.keys()),
        "running_sessions": running, "completed_sessions": completed,
        "timezone": "Asia/Shanghai",
    })


async def results(request):
    with LOCK:
        return JSONResponse(REPORT)


app = Starlette(routes=[
    Route("/health", health),
    Route("/results", results),
])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
