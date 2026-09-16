#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Isolated curl_cffi HAR-faithful signup. Does not start a browser."""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from cffi_client import bootstrap_signup, open_session, post_auth, response_json_keys
from cffi_headers import assert_no_grpc_signup_paths, cookie_names_from_session, request_json_keys
from param_origins import preflight_live_origins
from grok2api_import import import_build_sso
from grok_onboard import onboard_new_account
from guard_check import (
    NO_STREAK_KINDS,
    RISK_CLASSES,
    apply_streak,
    classify,
    register_check,
    should_retry_guard,
)
from outlook_min import apply_live_otp, load_inventory, wait_for_code
from signup_protocol import (
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    SIGNUP_PATHS,
    auth_cookie_names_from_har,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    json_keys_for_path,
    sso_from_response,
)
from turnstile_capsolver import solve_turnstile

FIRST = "James Mary Robert Patricia John Jennifer Michael Linda David Elizabeth".split()
LAST = "Smith Johnson Williams Brown Jones Garcia Miller Davis Wilson Anderson".split()


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def rand_password() -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "Aa1!" + "".join(random.choice(chars) for _ in range(10))


def session_exit_ip(session, timeout: float = 20.0) -> str:
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            resp = session.get(url, timeout=timeout)
            ip = str(getattr(resp, "text", "") or "").strip()
            if ip and " " not in ip and "." in ip:
                return ip
        except Exception:
            continue
    return ""


def compare_to_har(har, path: str, built: dict, status: int, data, session) -> dict:
    har_req = set(json_keys_for_path(har, path) or [])
    live_req = set(request_json_keys(built))
    extra_ok = {"castleRequestToken"} if path == AUTH_CREATE_ACCOUNT else set()
    return {
        "path": path,
        "status": status,
        "har_request_json_keys": sorted(har_req),
        "live_request_json_keys": sorted(live_req),
        "missing_request_keys": sorted(har_req - live_req),
        "extra_request_keys": sorted(live_req - har_req - extra_ok),
        "live_response_json_keys": response_json_keys(data),
        "live_cookie_names": cookie_names_from_session(session),
        "har_cookie_names": auth_cookie_names_from_har(har),
        "url": built.get("url"),
    }


def one_account(
    *,
    har,
    proxy: str,
    account: dict,
    capsolver_key: str,
    capsolver_api: str,
    guard_url: str,
    guard_token: str,
    grok2api: dict | None = None,
) -> dict:
    email = account["email"]
    session = open_session(har, proxy)
    boot = bootstrap_signup(session, har)
    log(f"bootstrap status={boot.get('status')} cookies={boot.get('cookie_names')} pk={str(boot.get('castle_pk') or '')[:12]}")
    exit_ip = session_exit_ip(session)
    log(f"exit_ip={exit_ip or '?'}")
    problems = preflight_live_origins(
        har=har,
        http_cookies=boot.get("http_cookies") or {},
        castle_source="missing",
        sentry_source="missing",
    )
    if problems:
        for item in problems:
            log(f"[origin] {item}")
        raise RuntimeError(
            "refusing synthetic/guessed params: "
            + " | ".join(problems[:6])
            + (f" (+{len(problems) - 6} more)" if len(problems) > 6 else "")
        )
    castle_email = str(boot.get("castle_token") or "")
    send = build_send_verification_code(email=email, castle_request_token=castle_email)
    assert_no_grpc_signup_paths([send["url"]])
    diffs = []
    preset = str(account.get("otp") or "").strip()
    if preset:
        code = preset
        log(f"skip send-code, otp preset len={len(code)}")
    else:
        sent_at = time.time()
        st, data, resp = post_auth(session, har, send)
        diffs.append(compare_to_har(har, AUTH_SEND_CODE, send, st, data, session))
        log(f"send-code status={st} keys={response_json_keys(data)}")
        if st >= 400 or st == 0:
            raise RuntimeError(f"send-code {st} {str(data)[:180]}")
        code = wait_for_code(account, after_ts=sent_at)
        log(f"otp len={len(code)}")
    verify = apply_live_otp(build_verify_email(email=email, code=code), code, "code")
    st, data, resp = post_auth(session, har, verify)
    if (st >= 400 or st == 0) and "-" in str(code):
        log(f"verify-email hyphen status={st}; retry digits-only HAR form")
        verify = build_verify_email(email=email, code=code)
        st, data, resp = post_auth(session, har, verify)
    diffs.append(compare_to_har(har, AUTH_VERIFY_EMAIL, verify, st, data, session))
    log(f"verify-email status={st} keys={response_json_keys(data)} code_form={verify['json'].get('code')}")
    if st >= 400 or st == 0:
        raise RuntimeError(f"verify-email {st} {str(data)[:180]}")
    sitekey = boot.get("sitekey") or "0x4AAAAAAAhr9JGVDZbrZOo0"
    ts_token = solve_turnstile(
        capsolver_key,
        website_url="https://accounts.x.ai/sign-up?redirect=grok-com",
        website_key=sitekey,
        api=capsolver_api,
    )
    log(f"turnstile len={len(ts_token)}")
    create = build_create_account(
        email=email,
        password=rand_password(),
        given_name=random.choice(FIRST),
        family_name=random.choice(LAST),
        email_validation_code=code,
        turnstile_token=ts_token,
        castle_request_token=castle_email,
    )
    create = apply_live_otp(create, code, "emailValidationCode")
    if "-" not in str(verify["json"].get("code") or ""):
        create = build_create_account(
            email=email,
            password=create["json"]["password"],
            given_name=create["json"]["givenName"],
            family_name=create["json"]["familyName"],
            email_validation_code=code,
            turnstile_token=ts_token,
            castle_request_token=str(boot.get("castle_token") or castle_email),
        )
    assert_no_grpc_signup_paths([create["url"]])
    st, data, resp = post_auth(session, har, create)
    diffs.append(compare_to_har(har, AUTH_CREATE_ACCOUNT, create, st, data, session))
    log(f"create-account status={st} keys={response_json_keys(data)}")
    if st >= 400 or st == 0:
        raise RuntimeError(f"create-account {st} {str(data)[:180]}")
    sso = sso_from_response(resp, data)
    if not sso:
        raise RuntimeError(f"no sso keys={response_json_keys(data)}")
    log(f"sso len={len(sso)}")
    onboard = onboard_new_account(session, sso)
    log(
        f"onboard home={onboard.get('home_status')} "
        f"birth={onboard.get('birth_status')} tos={onboard.get('tos_status')} "
        f"birth_err={onboard.get('birth_error') or onboard.get('birth_body') or ''}"
    )
    try:
        panel = Path("/data/claw-relocated/grok-register-panel")
        if str(panel) not in sys.path:
            sys.path.insert(0, str(panel))
        from sso_to_auth_json import inspect_sso_account_state  # noqa: WPS433

        state = inspect_sso_account_state(sso, proxy=proxy, log=log)
        log(
            f"grok.com warmup status={state.get('status_code')} "
            f"botFlag={state.get('bot_flag_source')} "
            f"denied={state.get('denied')} err={state.get('error')}"
        )
    except Exception as exc:
        log(f"grok.com warmup skip {exc}")
    g2a = grok2api or {}
    imported = None
    if g2a.get("url") and g2a.get("username") and g2a.get("password"):
        imported = import_build_sso(
            email,
            sso,
            base_url=str(g2a.get("url") or ""),
            username=str(g2a.get("username") or ""),
            password=str(g2a.get("password") or ""),
            proxy=proxy,
            log=log,
        )
        if not isinstance(imported, dict):
            imported = {"raw": str(imported)[:120]}
        log(
            "grok2api import "
            + ",".join(
                f"{k}={imported.get(k)}"
                for k in ("created", "updated", "skipped", "synced", "syncFailed")
            )
        )
    guard = {}
    kind = "no_verdict"
    for attempt in range(8):
        guard = register_check(
            email,
            guard_url=guard_url,
            guard_token=guard_token,
            egress_ip=exit_ip,
            timeout_seconds=90,
        )
        kind = classify(guard)
        log(
            f"guard attempt={attempt + 1} kind={kind} "
            f"classification={guard.get('classification')!r} "
            f"reason={guard.get('reason')!r} timed_out={guard.get('timed_out')} "
            f"account_id={str(guard.get('account_id') or '')[:16]}"
        )
        if not should_retry_guard(kind):
            break
        time.sleep(12.0)
    raw_class = str(guard.get("classification") or "")
    log(
        f"[guard] classification={raw_class!r} kind={kind} "
        f"reason={guard.get('reason')!r} risk={guard.get('risk')} "
        f"thinking={guard.get('thinking_seen')}"
    )
    return {
        "email": email,
        "exit_ip": exit_ip,
        "sso": bool(sso),
        "g2a_imported": bool(imported),
        "guard": kind,
        "guard_payload": {
            k: guard.get(k)
            for k in (
                "classification",
                "risk",
                "thinking_seen",
                "reason",
                "timed_out",
                "error",
            )
        },
        "diffs": diffs,
        "boot": {k: boot.get(k) for k in ("status", "castle_pk", "sitekey", "cookie_names")},
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--har", default=os.environ.get("CFFI_HAR") or str(ROOT / "har" / "signup-protocol-full.har"))
    p.add_argument("--assets", default=os.environ.get("CFFI_ASSETS") or str(ROOT / "isolated-assets.json"))
    p.add_argument("--outlook", default="")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--otp", default="", help="Reuse an already-fetched OTP for the first mailbox")
    p.add_argument("--out", default="")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    har_path = Path(args.har)
    if not har_path.is_file():
        raise SystemExit(f"HAR missing: {har_path}")
    assets = json.loads(Path(args.assets).read_text(encoding="utf-8"))
    proxies = list(assets.get("proxies") or [])
    outlook_path = Path(args.outlook or assets.get("outlook_file") or "")
    accounts = load_inventory(outlook_path)
    if args.otp and accounts:
        accounts[0]["otp"] = str(args.otp).strip()
    cap = assets.get("capsolver") or {}
    guard = assets.get("guard") or {}
    grok2api = assets.get("grok2api") or {}
    streak = 0
    needed = max(1, int(args.count))
    results = []
    idx = 0
    mailbox_i = 0
    while streak < needed and mailbox_i < len(accounts):
        account = accounts[mailbox_i]
        mailbox_i += 1
        proxy = proxies[idx % max(1, len(proxies))]
        proxy_url = proxy.get("url") if isinstance(proxy, dict) else str(proxy)
        idx += 1
        log(f"===== try {mailbox_i} email={account['email']} proxy={proxy.get('exit_ip') if isinstance(proxy, dict) else ''} =====")
        try:
            row = one_account(
                har=har_path,
                proxy=proxy_url,
                account=account,
                capsolver_key=str(cap.get("key") or os.environ.get("CAPSOLVER_KEY") or ""),
                capsolver_api=str(cap.get("api") or "https://api.capsolver.com"),
                guard_url=str(guard.get("url") or "http://192.168.0.5:8788"),
                guard_token=str(guard.get("token") or os.environ.get("GUARD_TOKEN") or ""),
                grok2api=grok2api,
            )
        except TimeoutError as exc:
            log(f"infra timeout (no streak change): {exc}")
            results.append({"email": account["email"], "guard": "timeout", "error": str(exc)})
            continue
        except Exception as exc:
            msg = str(exc)
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                log(f"infra timeout (no streak change): {msg[:200]}")
                results.append({"email": account["email"], "guard": "timeout", "error": msg[:240]})
                continue
            log(f"fail (no streak change unless guard): {msg[:240]}")
            results.append({"email": account["email"], "guard": "error", "error": msg[:240]})
            continue
        results.append(row)
        kind = classify(row.get("guard_payload") or {})
        streak = apply_streak(streak, kind)
        raw_class = str((row.get("guard_payload") or {}).get("classification") or "")
        if kind == "normal":
            log(f"[guard] 新号检测通过 classification={raw_class} streak={streak}/{needed}")
        elif kind in NO_STREAK_KINDS:
            log(
                f"[guard] no-verdict kind={kind} classification={raw_class!r} "
                f"reason={(row.get('guard_payload') or {}).get('reason')!r} streak={streak}"
            )
        else:
            log(f"[guard] 新号检测风控 classification={raw_class or kind} streak=0")
    payload = {"streak": streak, "needed": needed, "results": results}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"streak": streak, "needed": needed, "n": len(results)}, ensure_ascii=False))
    return 0 if streak >= needed else 1


if __name__ == "__main__":
    raise SystemExit(main())
