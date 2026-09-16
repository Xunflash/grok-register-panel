# -*- coding: utf-8 -*-
"""Patchright protocol adapter. Not Camoufox.

Uses the live page Castle/Sentry SDKs then posts the shipped HAR auth trio via
in-page fetch (same as protocol_register_once, different browser).
"""
from __future__ import annotations

import random
import string
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CFFI = ROOT / "cffi_har_signup"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CFFI) not in sys.path:
    sys.path.insert(0, str(CFFI))

from nobrowser_protocol.page_sdk import (  # noqa: E402
    CASTLE_MINT_JS,
    PAGE_POST_JS,
    SENTRY_JS,
    SITEKEY_JS,
)
from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_ORIGIN,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    assert_send_code_has_castle,
    auth_header_names_from_har,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    fetch_settable_headers,
    parse_inpage_post_result,
    sso_from_create_payload,
)

SIGNUP_URL = AUTH_ORIGIN + "/sign-up?redirect=grok-com"
FIRST = "James Mary Robert Patricia John Jennifer Michael Linda David Elizabeth".split()
LAST = "Smith Johnson Williams Brown Jones Garcia Miller Davis Wilson Anderson".split()


def camoufox_imports() -> tuple[str, ...]:
    return ()


def _rand_password() -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "Aa1!" + "".join(random.choice(chars) for _ in range(10))


def _proxy_for_patchright(proxy_url: str) -> dict | None:
    raw = str(proxy_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return {"server": raw}
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    out: dict[str, str] = {"server": server}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def _mint_castle(page, email: str, log: Callable[[str], None], timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = {"token": "", "how": "missing", "pk": ""}
    while time.time() < deadline:
        try:
            result = page.evaluate(CASTLE_MINT_JS, email)
        except Exception as exc:
            last = {"token": "", "how": f"evaluate_error:{exc}", "pk": ""}
            time.sleep(1.0)
            continue
        if isinstance(result, dict):
            last = {
                "token": str(result.get("token") or "").strip(),
                "how": str(result.get("how") or ""),
                "pk": str(result.get("pk") or ""),
            }
        if last["token"]:
            log(f"castle how={last['how']} pk={last['pk'][:12]} len={len(last['token'])}")
            return last
        time.sleep(1.0)
    log(f"castle missing how={last.get('how')}")
    return last


def _sentry(page) -> dict:
    try:
        data = page.evaluate(SENTRY_JS)
    except Exception:
        return {"sentry_trace": "", "baggage": "", "how": "error"}
    return data if isinstance(data, dict) else {"sentry_trace": "", "baggage": "", "how": "raw"}


def _post(page, har: Any, built: dict, sentry: dict, log: Callable[[str], None]):
    live = {}
    if sentry.get("sentry_trace"):
        live["sentry-trace"] = sentry["sentry_trace"]
    if sentry.get("baggage"):
        live["baggage"] = sentry["baggage"]
    headers = fetch_settable_headers(auth_header_names_from_har(har), live)
    result = page.evaluate(
        PAGE_POST_JS,
        {"url": built["url"], "body": built.get("json") or {}, "headers": headers},
    )
    status, data, raw = parse_inpage_post_result(result)
    log(f"post {built.get('path')} status={status}")
    return status, data, raw


def run_patchright_one(
    *,
    har: Any,
    proxy: str,
    account: dict,
    capsolver_key: str,
    capsolver_api: str,
    guard_url: str,
    guard_token: str,
    grok2api: dict | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    from patchright.sync_api import sync_playwright

    from grok2api_import import import_build_sso
    from grok_onboard import onboard_new_account
    from guard_check import classify, register_check, should_retry_guard
    from outlook_min import apply_live_otp, wait_for_code
    from signup_protocol import make_protocol_session
    from turnstile_capsolver import solve_turnstile

    emit = log or (lambda _msg: None)
    emit("engine=patchright camoufox=no")
    email = account["email"]
    pw = _rand_password()
    given = random.choice(FIRST)
    family = random.choice(LAST)
    proxy_cfg = _proxy_for_patchright(proxy)
    with sync_playwright() as playwright:
        launch_kw: dict[str, Any] = {"headless": True}
        if proxy_cfg:
            launch_kw["proxy"] = proxy_cfg
        browser = playwright.chromium.launch(**launch_kw)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)
            castle = _mint_castle(page, email, emit)
            if not castle.get("token"):
                raise RuntimeError(f"patchright castle SDK empty how={castle.get('how')}")
            sentry = _sentry(page)
            emit(f"sentry how={sentry.get('how')}")
            try:
                sitekey = str(page.evaluate(SITEKEY_JS) or "").strip()
            except Exception:
                sitekey = ""
            send = build_send_verification_code(
                email=email, castle_request_token=castle["token"]
            )
            assert_send_code_has_castle(send)
            sent_at = time.time()
            st, data, _raw = _post(page, har, send, sentry, emit)
            if st >= 400 or st == 0:
                raise RuntimeError(f"send-code {st} {str(data)[:180]}")
            code = str(account.get("otp") or "").strip() or wait_for_code(account, after_ts=sent_at)
            emit(f"otp len={len(code)}")
            verify = apply_live_otp(build_verify_email(email=email, code=code), code, "code")
            st, data, _raw = _post(page, har, verify, sentry, emit)
            if (st >= 400 or st == 0) and "-" in code:
                verify = build_verify_email(email=email, code=code)
                st, data, _raw = _post(page, har, verify, sentry, emit)
            if st >= 400 or st == 0:
                raise RuntimeError(f"verify-email {st} {str(data)[:180]}")
            ts_token = solve_turnstile(
                capsolver_key,
                website_url=SIGNUP_URL,
                website_key=sitekey or "0x4AAAAAAAhr9JGVDZbrZOo0",
                api=capsolver_api,
            )
            emit(f"turnstile len={len(ts_token)}")
            create = build_create_account(
                email=email,
                password=pw,
                given_name=given,
                family_name=family,
                email_validation_code=code,
                turnstile_token=ts_token,
                castle_request_token=castle["token"],
            )
            create = apply_live_otp(create, code, "emailValidationCode")
            st, data, _raw = _post(page, har, create, sentry, emit)
            if st >= 400 or st == 0:
                raise RuntimeError(f"create-account {st} {str(data)[:180]}")
            sso = sso_from_create_payload(data)
            if not sso:
                for cookie in context.cookies():
                    if str(cookie.get("name") or "").lower() in {"sso", "sso-rw"}:
                        sso = str(cookie.get("value") or "")
                        if sso:
                            break
            if not sso:
                raise RuntimeError(
                    f"no sso keys={list(data)[:12] if isinstance(data, dict) else type(data)}"
                )
            emit(f"sso len={len(sso)}")
        finally:
            browser.close()

    session = make_protocol_session(proxy_url=proxy)
    onboard = onboard_new_account(session, sso)
    emit(f"onboard birth={onboard.get('birth_status')} tos={onboard.get('tos_status')}")
    imported = None
    g2a = grok2api or {}
    if g2a.get("url") and g2a.get("username") and g2a.get("password"):
        imported = import_build_sso(
            email,
            sso,
            base_url=str(g2a.get("url") or ""),
            username=str(g2a.get("username") or ""),
            password=str(g2a.get("password") or ""),
            proxy=proxy,
            log=emit,
        )
        emit(f"grok2api imported={bool(imported)}")
    exit_ip = ""
    try:
        resp = session.get("https://api.ipify.org", timeout=15)
        exit_ip = str(getattr(resp, "text", "") or "").strip()
    except Exception:
        pass
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
        emit(
            f"guard attempt={attempt + 1} kind={kind} "
            f"classification={guard.get('classification')!r}"
        )
        if not should_retry_guard(kind):
            break
        time.sleep(12.0)
    return {
        "email": email,
        "engine": "patchright",
        "camoufox": False,
        "sso": bool(sso),
        "g2a_imported": bool(imported),
        "guard": kind,
        "guard_payload": {
            k: guard.get(k)
            for k in ("classification", "risk", "thinking_seen", "reason", "timed_out", "error")
        },
        "exit_ip": exit_ip,
    }
