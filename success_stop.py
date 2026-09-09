# -*- coding: utf-8 -*-
"""Stop a registration run when the panel 1h success rate is below the UI floor."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from runtime_platform import TZ_BEIJING, now_beijing

DEFAULT_MIN_ACCOUNTS = 10
DEFAULT_MIN_SUCCESS_RATE = 65.0
MIN_ACCOUNTS_BOUNDS = (1, 5000)
MIN_RATE_BOUNDS = (0.0, 100.0)
DEFAULT_WINDOW_HOURS = 1
JSONL_TAIL_BYTES = 8_000_000

_WINDOW_LOCK = threading.Lock()
_WINDOW_CACHE: dict[tuple[str, int, int], tuple[float, float, dict[str, Any]]] = {}


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(lo), min(int(hi), parsed))


def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(lo), min(float(hi), parsed))


def normalize_success_stop(control: Mapping[str, Any] | None = None) -> dict[str, float | int]:
    data = dict(control or {})
    min_accounts = _clamp_int(
        data.get("stop_min_accounts", DEFAULT_MIN_ACCOUNTS),
        DEFAULT_MIN_ACCOUNTS,
        MIN_ACCOUNTS_BOUNDS[0],
        MIN_ACCOUNTS_BOUNDS[1],
    )
    min_rate = _clamp_float(
        data.get("stop_min_success_rate", DEFAULT_MIN_SUCCESS_RATE),
        DEFAULT_MIN_SUCCESS_RATE,
        MIN_RATE_BOUNDS[0],
        MIN_RATE_BOUNDS[1],
    )
    return {"stop_min_accounts": min_accounts, "stop_min_success_rate": min_rate}


def load_success_stop_settings(path: str | os.PathLike[str] | None = None) -> dict[str, float | int]:
    env_accounts = os.environ.get("GROK_STOP_MIN_ACCOUNTS")
    env_rate = os.environ.get("GROK_STOP_MIN_SUCCESS_RATE")
    data: dict[str, Any] = {}
    control_path = Path(path) if path else Path(__file__).resolve().parent / "log" / "monitor_control.json"
    try:
        raw = json.loads(control_path.read_text(encoding="utf-8") or "{}")
        if isinstance(raw, dict):
            data.update(raw)
    except Exception:
        pass
    if env_accounts not in (None, ""):
        data["stop_min_accounts"] = env_accounts
    if env_rate not in (None, ""):
        data["stop_min_success_rate"] = env_rate
    return normalize_success_stop(data)


def parse_result_datetime(ts: str) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ_BEIJING)
    except Exception:
        return None


def result_status_bucket(status: Any) -> str | None:
    st = str(status or "").strip()
    if not st:
        return None
    if st == "ok":
        return "ok"
    if st == "risk":
        return "risk"
    return "fail"


def window_rate_percent(ok: int, fail: int, risk: int = 0) -> float | None:
    """Same formula as the panel rates chip: 100 * ok / (ok + fail + risk), 1 decimal."""
    total = max(0, int(ok or 0)) + max(0, int(fail or 0)) + max(0, int(risk or 0))
    if total <= 0:
        return None
    return round(100.0 * max(0, int(ok or 0)) / total, 1)


def success_rate_percent(ok: int, fail: int) -> float | None:
    return window_rate_percent(ok, fail, 0)


def iter_result_records(
    path: str | os.PathLike[str] | None,
    tail_bytes: int = JSONL_TAIL_BYTES,
) -> Iterator[tuple[Any, datetime | None]]:
    results = Path(path) if path else Path()
    if not results.is_file():
        return
    try:
        size = results.stat().st_size
        with results.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
                handle.readline()
            for line in handle:
                try:
                    payload = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                yield payload.get("status"), parse_result_datetime(payload.get("ts") or "")
    except Exception:
        return


def window_success_counts(
    path: str | os.PathLike[str] | None,
    hours: int = DEFAULT_WINDOW_HOURS,
    *,
    now: datetime | None = None,
    tail_bytes: int = JSONL_TAIL_BYTES,
    cache_seconds: float = 5.0,
) -> dict[str, Any]:
    hours_n = max(1, int(hours or DEFAULT_WINDOW_HOURS))
    now_dt = now or now_beijing()
    path_obj = Path(path) if path else Path()
    cache_key = (str(path_obj), hours_n, int(tail_bytes))
    try:
        mtime = path_obj.stat().st_mtime if path_obj.is_file() else -1.0
    except OSError:
        mtime = -1.0
    if cache_seconds and cache_seconds > 0:
        with _WINDOW_LOCK:
            hit = _WINDOW_CACHE.get(cache_key)
            if hit and hit[0] == mtime and (time.monotonic() - hit[1]) < cache_seconds:
                return dict(hit[2])
    ok = fail = risk = 0
    horizon = timedelta(hours=hours_n)
    for status, dt in iter_result_records(path_obj, tail_bytes=tail_bytes):
        if not dt:
            continue
        if now_dt - dt > horizon:
            continue
        bucket = result_status_bucket(status)
        if bucket == "ok":
            ok += 1
        elif bucket == "risk":
            risk += 1
        elif bucket == "fail":
            fail += 1
    total = ok + fail + risk
    value = {
        "ok": ok,
        "fail": fail,
        "risk": risk,
        "total": total,
        "success_rate": window_rate_percent(ok, fail, risk),
        "hours": hours_n,
    }
    if cache_seconds and cache_seconds > 0:
        with _WINDOW_LOCK:
            _WINDOW_CACHE[cache_key] = (mtime, time.monotonic(), dict(value))
    return value


def should_stop_low_success(
    ok: int,
    fail: int,
    settings: Mapping[str, Any] | None = None,
    *,
    window_label: str = "近1小时",
) -> tuple[bool, str]:
    """True when completed attempts exceed the account floor and rate is below the floor.

    Completed attempts are successes + counted failures (including risk when the
    caller folds risk into ``fail``, matching the panel 1h denominator).
    """
    cfg = normalize_success_stop(settings)
    min_accounts = int(cfg["stop_min_accounts"])
    min_rate = float(cfg["stop_min_success_rate"])
    ok_n = max(0, int(ok or 0))
    fail_n = max(0, int(fail or 0))
    done = ok_n + fail_n
    if done <= min_accounts:
        return False, ""
    rate = window_rate_percent(ok_n, fail_n, 0)
    if rate is None or rate >= min_rate:
        return False, ""
    label = window_label or "近1小时"
    return True, (
        f"{label}成功率过低，停止注册: {ok_n}/{done} = {rate:.1f}% < {min_rate:.1f}% "
        f"(完成>{min_accounts})"
    )


def should_stop_from_hour_window(
    jsonl_path: str | os.PathLike[str] | None,
    settings: Mapping[str, Any] | None = None,
    hours: int = DEFAULT_WINDOW_HOURS,
    *,
    now: datetime | None = None,
    cache_seconds: float = 5.0,
) -> tuple[bool, str]:
    counts = window_success_counts(
        jsonl_path,
        hours=hours,
        now=now,
        cache_seconds=cache_seconds,
    )
    label = f"近{int(counts.get('hours') or hours)}小时"
    return should_stop_low_success(
        int(counts.get("ok") or 0),
        int(counts.get("fail") or 0) + int(counts.get("risk") or 0),
        settings,
        window_label=label,
    )
