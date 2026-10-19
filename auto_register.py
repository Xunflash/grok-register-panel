# -*- coding: utf-8 -*-
"""Auto-start panel registration when grok2api 可调度账号 drop below a threshold."""
from __future__ import annotations

from typing import Any, Callable, Mapping, MutableMapping

DEFAULT_THRESHOLD = 100
DEFAULT_COOLDOWN_MIN = 120
THRESHOLD_BOUNDS = (1, 100000)
COOLDOWN_BOUNDS = (1, 10080)


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(lo), min(int(hi), parsed))


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n", ""}:
        return False
    return bool(default)


def _as_optional_timestamp(value: Any) -> float | None:
    if value is None or value is False:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def normalize_auto_register(control: Mapping[str, Any] | None = None) -> dict[str, Any]:
    data = dict(control or {})
    last_at = _as_optional_timestamp(data.get("auto_register_last_at"))
    return {
        "auto_register_threshold": _clamp_int(
            data.get("auto_register_threshold", DEFAULT_THRESHOLD),
            DEFAULT_THRESHOLD,
            THRESHOLD_BOUNDS[0],
            THRESHOLD_BOUNDS[1],
        ),
        "auto_register_cooldown_min": _clamp_int(
            data.get("auto_register_cooldown_min", DEFAULT_COOLDOWN_MIN),
            DEFAULT_COOLDOWN_MIN,
            COOLDOWN_BOUNDS[0],
            COOLDOWN_BOUNDS[1],
        ),
        "auto_register_enabled": _as_bool(data.get("auto_register_enabled"), True),
        "auto_register_last_at": last_at,
    }


def should_auto_register(
    *,
    available: int | None,
    threshold: int = DEFAULT_THRESHOLD,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MIN,
    last_auto_at: float | None,
    now: float,
    enabled: bool,
    already_running: bool,
) -> bool:
    """True when auto-register should start a 持续编排 job.

    Trigger is grok2api 可调度账号 ``available`` strictly less than ``threshold``.
    Cooldown is measured from the last *auto* activation only.
    """
    if not enabled:
        return False
    if already_running:
        return False
    if available is None:
        return False
    try:
        available_n = int(available)
        threshold_n = int(threshold)
        cooldown_n = int(cooldown_minutes)
        now_n = float(now)
    except (TypeError, ValueError):
        return False
    if available_n >= threshold_n:
        return False
    last = _as_optional_timestamp(last_auto_at)
    if last is not None and (now_n - last) < max(0, cooldown_n) * 60.0:
        return False
    return True


def run_auto_register_tick(
    *,
    control: Mapping[str, Any] | None,
    now: float,
    already_running: bool,
    available: int | None,
    start: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Near-start decision used by the panel watcher.

    ``start`` is the existing orch entry (``start_orch``). When the decision is
    yes, it is invoked once; the caller records last auto-activation on success.
    """
    cfg = normalize_auto_register(control)
    should = should_auto_register(
        available=available,
        threshold=int(cfg["auto_register_threshold"]),
        cooldown_minutes=int(cfg["auto_register_cooldown_min"]),
        last_auto_at=cfg["auto_register_last_at"],
        now=now,
        enabled=bool(cfg["auto_register_enabled"]),
        already_running=bool(already_running),
    )
    if not should:
        return {"started": False, "ok": False, "control": cfg}
    if start is None:
        return {"started": True, "ok": True, "result": None, "control": cfg}
    result = start()
    started = bool(isinstance(result, dict) and result.get("ok") is True)
    return {
        "started": started,
        "ok": started,
        "result": result,
        "control": cfg,
    }


def apply_auto_register_control(target: MutableMapping[str, Any]) -> None:
    """Write normalized auto-register fields onto a control mapping."""
    cfg = normalize_auto_register(target)
    target["auto_register_threshold"] = int(cfg["auto_register_threshold"])
    target["auto_register_cooldown_min"] = int(cfg["auto_register_cooldown_min"])
    target["auto_register_enabled"] = bool(cfg["auto_register_enabled"])
    if cfg["auto_register_last_at"] is None:
        target.pop("auto_register_last_at", None)
    else:
        target["auto_register_last_at"] = float(cfg["auto_register_last_at"])
