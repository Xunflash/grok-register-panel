#!/usr/bin/env python3
"""Retry CPA Device Flow + Guard for pending SSOs (no new signup)."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONUNBUFFERED", "1")
for proxy_key in (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
):
    os.environ.pop(proxy_key, None)

from grok_register_ttk import (  # noqa: E402
    RegistrationRiskDenied,
    _wire_runtime_modules,
    add_sso_to_cpa,
    load_config,
    pick_proxy_for_worker,
    set_thread_proxy,
)
import grok_register_ttk as app  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def classify_guard(payload: dict) -> str:
    if not payload:
        return "skipped"
    if payload.get("error") or payload.get("timed_out"):
        return "error"
    if "risk" in payload:
        return "risk" if payload.get("risk") else "pass"
    return "skipped"


def load_pending(wanted: set[str]) -> list[tuple[str, str]]:
    path = ROOT / "accounts" / "sso_pending.txt"
    out = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if "----" not in raw:
            continue
        email, sso = raw.split("----", 1)
        email = email.strip()
        sso = sso.strip()
        if not email or not sso or email.lower() in seen:
            continue
        if wanted and email.lower() not in wanted:
            continue
        seen.add(email.lower())
        out.append((email, sso))
    return out


def convert_one(index: int, email: str, sso: str) -> dict:
    result = {"email": email, "cpa": False, "guard": None, "ok": False, "error": ""}
    px = pick_proxy_for_worker(0, index - 1)
    set_thread_proxy(px)
    guard_box = {}
    orig_apply = app._apply_guard_register_result

    def _capture_guard(email_arg, payload, log_callback=None):
        guard_box["payload"] = payload if isinstance(payload, dict) else {}
        return orig_apply(email_arg, payload, log_callback=log_callback)

    app._apply_guard_register_result = _capture_guard
    try:
        for attempt in range(1, 4):
            guard_box.pop("payload", None)
            try:
                cpa_ok = add_sso_to_cpa(sso, email=email, log_callback=log)
            except RegistrationRiskDenied as exc:
                result["cpa"] = True
                result["guard"] = classify_guard(guard_box.get("payload") or {}) or "risk"
                result["error"] = str(exc)[:240]
                return result
            if cpa_ok:
                result["cpa"] = True
                result["guard"] = classify_guard(guard_box.get("payload") or {})
                result["ok"] = result["guard"] == "pass"
                return result
            log(f"[*] [{email}] CPA fail attempt {attempt}/3, retry")
            time.sleep(3)
        result["error"] = "cpa_token_failed"
        result["guard"] = classify_guard(guard_box.get("payload") or {})
        return result
    finally:
        app._apply_guard_register_result = orig_apply


def main() -> int:
    wanted = {a.strip().lower() for a in sys.argv[1:] if a.strip()}
    load_config()
    app.config["register_workers"] = 1
    _wire_runtime_modules()
    rows = load_pending(wanted)
    if not rows:
        log("[!] no pending SSO matched")
        return 2
    results = []
    for i, (email, sso) in enumerate(rows, 1):
        log(f"===== pending CPA/Guard {i}/{len(rows)} {email} sso_len={len(sso)} =====")
        results.append(convert_one(i, email, sso))
    log("===== pending CPA/Guard summary =====")
    ok_n = 0
    for row in results:
        if row.get("ok"):
            ok_n += 1
        log(
            f"email={row.get('email')} cpa={row.get('cpa')} guard={row.get('guard')} "
            f"ok={row.get('ok')} err={row.get('error') or '-'}"
        )
    log(f"[*] success {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
