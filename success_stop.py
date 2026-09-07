# -*- coding: utf-8 -*-
"""Stop a registration run when enough attempts have a low success rate."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

DEFAULT_MIN_ACCOUNTS = 10
DEFAULT_MIN_SUCCESS_RATE = 65.0
MIN_ACCOUNTS_BOUNDS = (1, 5000)
MIN_RATE_BOUNDS = (0.0, 100.0)


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


def success_rate_percent(ok: int, fail: int) -> float | None:
    done = max(0, int(ok or 0)) + max(0, int(fail or 0))
    if done <= 0:
        return None
    return 100.0 * max(0, int(ok or 0)) / done


def should_stop_low_success(
    ok: int,
    fail: int,
    settings: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """True when completed attempts exceed the account floor and rate is below the floor.

    Completed attempts are successes + counted failures (including risk).
    """
    cfg = normalize_success_stop(settings)
    min_accounts = int(cfg["stop_min_accounts"])
    min_rate = float(cfg["stop_min_success_rate"])
    ok_n = max(0, int(ok or 0))
    fail_n = max(0, int(fail or 0))
    done = ok_n + fail_n
    if done <= min_accounts:
        return False, ""
    rate = success_rate_percent(ok_n, fail_n) or 0.0
    if rate >= min_rate:
        return False, ""
    return True, (
        f"成功率过低，停止注册: {ok_n}/{done} = {rate:.1f}% < {min_rate:.1f}% "
        f"(完成>{min_accounts})"
    )
