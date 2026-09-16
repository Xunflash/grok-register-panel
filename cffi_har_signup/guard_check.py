# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import urllib.request
from typing import Any


def register_check(
    email: str,
    *,
    guard_url: str,
    guard_token: str = "",
    egress_ip: str = "",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    url = str(guard_url or "").rstrip("/") + "/api/register-check"
    body = {
        "email": str(email or "").strip(),
        "egress_ip": str(egress_ip or "").strip(),
        "timeout_seconds": int(timeout_seconds),
    }
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if guard_token:
        headers["X-Guard-Control-Token"] = guard_token
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds + 60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return {"risk": False, "error": "invalid guard payload"}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return data if isinstance(data, dict) else {"risk": False}


NO_VERDICT_REASONS = frozenset({"account_not_found", "email_or_account_id_required"})
RISK_CLASSES = frozenset({"risk_controlled", "suspicious"})
NO_STREAK_KINDS = frozenset({"timeout", "no_verdict", "error"})


def classify(payload: dict[str, Any]) -> str:
    """Map a Guard register-check payload to a streak kind.

    A win is only when payload `classification` is literally `normal`.
    Guard may still set `timed_out` after a real probe; prefer the literal
    classification over the timeout flag. `account_not_found` / empty
    classification is `no_verdict` (not a pass).
    """
    data = payload if isinstance(payload, dict) else {}
    classification = str(data.get("classification") or "").strip()
    if classification == "normal":
        return "normal"
    if classification in RISK_CLASSES:
        return classification
    reason = str(data.get("reason") or "").strip()
    if reason in NO_VERDICT_REASONS:
        return "no_verdict"
    if data.get("error") or data.get("timed_out"):
        return "timeout"
    if data.get("risk"):
        return "risk_controlled"
    if not classification:
        return "no_verdict"
    return classification


def should_retry_guard(kind: str) -> bool:
    """Retry register-check when Guard has no probed classification yet."""
    return str(kind or "").strip() in NO_STREAK_KINDS


def apply_streak(streak: int, kind: str) -> int:
    """Update consecutive Guard-clean count.

    `normal` increments. `risk_controlled` / `suspicious` / other probed
    classes reset to 0. timeout / no_verdict / error leave the streak unchanged.
    """
    current = int(streak or 0)
    label = str(kind or "").strip()
    if label in NO_STREAK_KINDS:
        return current
    if label == "normal":
        return current + 1
    return 0
