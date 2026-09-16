# -*- coding: utf-8 -*-
"""Patchright protocol adapter. Not Camoufox.

Uses the live page Castle/Sentry SDKs then posts the shipped HAR auth trio via
in-page fetch (same as protocol_register_once, different browser).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import string
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CFFI = ROOT / "cffi_har_signup"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CFFI) not in sys.path:
    sys.path.insert(0, str(CFFI))

from nobrowser_protocol.page_sdk import (  # noqa: E402
    CASTLE_MINT_JS,
    CLICK_VISIBLE_JS,
    DUMP_PAGE_JS,
    FILL_EMAIL_JS,
    FILL_OTP_JS,
    OTP_VISIBLE_JS,
    PAGE_POST_JS,
    SENTRY_JS,
    SITEKEY_JS,
    SUBMIT_EMAIL_JS,
)
from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_ORIGIN,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    apply_live_otp,
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
_T = TypeVar("_T")


def _asyncio_loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def run_patchright_sync(fn: Callable[[], _T]) -> _T:
    """Run Patchright sync API off a thread that already has a running asyncio loop.

    Panel workers import Camoufox/Playwright and can already be inside a loop.
    Patchright's sync_playwright() then raises Sync API inside the asyncio loop.
    A fresh thread has no running loop, so driver enter can proceed.
    """
    if not _asyncio_loop_running():
        return fn()
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=_target, name="patchright-sync", daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]
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


COOKIE_NEEDLES = ("Reject All", "Accept All Cookies", "Accept All", "Reject")
EMAIL_ENTRY_NEEDLES = (
    "Sign up with email",
    "Continue with email",
    "Use email",
    "使用邮箱注册",
    "使用邮箱",
    "邮箱注册",
)
SUBMIT_NEEDLES = ("Continue", "Next", "Sign up", "Create account", "继续", "下一步", "注册")
SUBMIT_DENY = (
    "with email",
    "with apple",
    "with google",
    "with x",
    "privacy",
    "go back",
    "使用邮箱",
)


def _js_click(page, needles: tuple[str, ...], deny: tuple[str, ...] = (), timeout: int = 4000) -> str:
    deadline = time.time() + (timeout / 1000.0)
    last = ""
    while time.time() < deadline:
        try:
            last = str(page.evaluate(CLICK_VISIBLE_JS, {"needles": list(needles), "deny": list(deny)}) or "")
        except Exception:
            last = ""
        if last:
            return last
        page.wait_for_timeout(250)
    return last


def _click_first(
    page,
    names: tuple[str, ...],
    timeout: int = 4000,
    deny: tuple[str, ...] = (),
) -> str:
    hit = _js_click(page, names, deny=deny, timeout=timeout)
    if hit:
        return hit
    skip = tuple(x.lower() for x in deny)
    for name in names:
        loc = page.get_by_role("button", name=name, exact=False)
        try:
            if loc.count() == 0:
                loc = page.get_by_text(name, exact=False)
            loc.first.wait_for(state="visible", timeout=min(timeout, 1500))
            label = ""
            try:
                label = str(loc.first.inner_text(timeout=500) or name)
            except Exception:
                label = name
            compact = label.replace(" ", "").lower()
            if any(item.replace(" ", "").lower() in compact for item in skip if item):
                continue
            loc.first.click(timeout=timeout)
            return label or name
        except Exception:
            continue
    return ""


def _dump_page(page, log: Callable[[str], None], prefix: str) -> None:
    try:
        dump = page.evaluate(DUMP_PAGE_JS)
    except Exception as exc:
        log(f"{prefix} dump_error={exc}")
        return
    log(f"{prefix} {json.dumps(dump, ensure_ascii=False)[:700]}")


def _open_email_form(page, log: Callable[[str], None]) -> None:
    cookie = _click_first(page, COOKIE_NEEDLES, timeout=2500)
    if cookie:
        log(f"cookie banner click={cookie!r}")
        page.wait_for_timeout(500)
    opened = _click_first(page, EMAIL_ENTRY_NEEDLES, timeout=8000)
    if opened:
        log(f"email entry click={opened!r}")
        page.wait_for_timeout(800)
    else:
        _dump_page(page, log, "email-entry-miss")


def _capture_auth_headers(captured: dict[str, Any], request) -> None:
    url = str(request.url or "")
    method = str(request.method or "").upper()
    if "accounts.x.ai" not in url and "/api/auth/" not in url:
        return
    captured.setdefault("auth_urls", []).append(f"{method} {url}")
    if "/api/auth/" not in url:
        return
    headers = request.headers or {}
    sentry_trace = str(headers.get("sentry-trace") or headers.get("sentry_trace") or "").strip()
    baggage = str(headers.get("baggage") or "").strip()
    if sentry_trace:
        captured["sentry_trace"] = sentry_trace
        captured["baggage"] = baggage
        captured["sentry_how"] = "page_request_headers"
    if method != "POST":
        return
    try:
        body = request.post_data_json
    except Exception:
        body = None
    if not isinstance(body, dict):
        raw = request.post_data or ""
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
    if not isinstance(body, dict):
        body = {}
    captured.setdefault("posts", []).append({"url": url, "keys": list(body)})
    token = str(body.get("castleRequestToken") or "").strip()
    if token:
        captured["token"] = token
        captured["json"] = body
        captured["url"] = url
        captured["how"] = "page_send_code_request"
    if AUTH_SEND_CODE in url:
        captured["json"] = body or captured.get("json") or {}
        captured["url"] = url
    if AUTH_VERIFY_EMAIL in url:
        captured["verify_json"] = body
    if AUTH_CREATE_ACCOUNT in url:
        captured["create_json"] = body


def _otp_visible(page) -> bool:
    try:
        return bool(page.evaluate(OTP_VISIBLE_JS))
    except Exception:
        return False


def _send_code_succeeded(captured: dict | None, otp_visible: bool = False) -> bool:
    cap = captured or {}
    if str(cap.get("token") or "").strip():
        return True
    body = cap.get("json") or {}
    if isinstance(body, dict) and (body.get("email") or body.get("castleRequestToken")):
        return True
    how = str(cap.get("how") or "")
    if how in {"page_send_code_request", "page_otp_form"}:
        return True
    return bool(otp_visible)


def _fill_otp_and_confirm(page, code: str, log: Callable[[str], None]) -> str:
    live = str(code or "").strip()
    digits = "".join(ch for ch in live if ch.isalnum())
    filled = ""
    for value in (live, digits):
        if not value:
            continue
        try:
            loc = page.locator(
                'input[name="code"], input[autocomplete="one-time-code"], input[data-input-otp]'
            ).first
            loc.wait_for(state="visible", timeout=8000)
            loc.fill(value, timeout=5000)
            filled = value
            break
        except Exception as exc:
            log(f"otp locator fill: {exc}")
    if not filled:
        try:
            react = page.evaluate(FILL_OTP_JS, live or digits)
            log(f"otp react fill={react!r}")
            filled = live or digits
        except Exception as exc:
            log(f"otp react fill: {exc}")
    clicked = _click_first(
        page,
        ("Confirm email", "Confirm", "Verify", "Continue", "确认", "验证"),
        timeout=5000,
        deny=("with email", "privacy", "resend"),
    )
    if not clicked:
        clicked = str(page.evaluate(SUBMIT_EMAIL_JS) or "js-submit")
    log(f"otp submit click={clicked!r} filled={bool(filled)}")
    return clicked


def _castle_from_capture_or_mint(captured: dict | None, minted: dict | None) -> dict:
    """Prefer live send-code XHR castle; only then Castle SDK evaluate."""
    cap = captured or {}
    token = str(cap.get("token") or "").strip()
    if token:
        return {
            "token": token,
            "how": str(cap.get("how") or "page_send_code_request"),
            "pk": str(cap.get("pk") or ""),
        }
    mint = minted or {}
    return {
        "token": str(mint.get("token") or "").strip(),
        "how": str(mint.get("how") or "missing"),
        "pk": str(mint.get("pk") or ""),
    }


def _capture_send_code(
    page,
    email: str,
    log: Callable[[str], None],
    timeout: float = 45.0,
    captured: dict[str, Any] | None = None,
) -> dict:
    """Let the live page SDK fire send-code; capture castleRequestToken from that XHR."""
    captured = captured if captured is not None else {}

    def on_request(request):
        _capture_auth_headers(captured, request)

    page.on("request", on_request)
    try:
        _open_email_form(page, log)
        loc = page.locator(
            'input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]'
        ).first
        loc.wait_for(state="visible", timeout=20000)
        filled = False
        try:
            loc.fill(email, timeout=8000)
            filled = True
        except Exception as exc:
            log(f"locator fill: {exc}")
        if not filled:
            react = page.evaluate(FILL_EMAIL_JS, email)
            log(f"react fill={react!r}")
        clicked = ""
        try:
            submit = page.locator('button[type="submit"]').first
            if submit.count() > 0:
                submit.wait_for(state="visible", timeout=3000)
                clicked = str(submit.inner_text(timeout=1000) or "submit")
                submit.click(timeout=5000)
        except Exception:
            clicked = ""
        if not clicked:
            clicked = _click_first(page, SUBMIT_NEEDLES, timeout=5000, deny=SUBMIT_DENY)
        if not clicked:
            clicked = str(page.evaluate(SUBMIT_EMAIL_JS) or "js-submit")
        log(f"email submit click={clicked!r}")
    except Exception as exc:
        log(f"page fill/submit: {exc}")
        _dump_page(page, log, "fill-submit-fail")
    deadline = time.time() + timeout
    otp_since = None
    while time.time() < deadline:
        token = str(
            captured.get("token")
            or ((captured.get("json") or {}).get("castleRequestToken"))
            or ""
        ).strip()
        if token:
            captured["token"] = token
            captured["how"] = captured.get("how") or "page_send_code_request"
            log(f"castle how=page_send_code_request len={len(token)}")
            return captured
        if _otp_visible(page):
            if otp_since is None:
                otp_since = time.time()
                log("OTP form visible; wait briefly for send-code XHR")
            elif time.time() - otp_since >= 3.0:
                captured["how"] = "page_otp_form"
                log(f"send-code inferred from OTP form auth_urls={captured.get('auth_urls')!r}"[:400])
                return captured
        time.sleep(0.4)
    _dump_page(page, log, "send-code-timeout")
    captured["how"] = captured.get("how") or "page_send_code_timeout"
    captured.setdefault("token", "")
    captured.setdefault("json", {})
    return captured


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
    payload = {"url": built["url"], "body": built.get("json") or {}, "headers": headers}
    try:
        result = page.evaluate(PAGE_POST_JS, payload)
        status, data, raw = parse_inpage_post_result(result)
        log(f"post {built.get('path')} status={status} how=evaluate")
        if status:
            return status, data, raw
    except Exception as exc:
        log(f"post evaluate: {exc}")
    try:
        resp = page.request.post(
            built["url"],
            headers={**headers, "content-type": "application/json"},
            data=json.dumps(payload["body"]),
        )
        text = resp.text()
        try:
            data = resp.json()
        except Exception:
            data = text
        log(f"post {built.get('path')} status={resp.status} how=context_request")
        return int(resp.status), data, text
    except Exception as exc:
        log(f"post request: {exc}")
        return 0, {"error": str(exc)}, None


def _chrome_path() -> str:
    env = str(os.environ.get("PATCHRIGHT_CHROME") or "").strip()
    if env:
        return env
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"):
        if Path(candidate).is_file():
            return candidate
    return ""


def _default_har_path() -> Path:
    for candidate in (
        ROOT / "log" / "signup-protocol-full.har",
        ROOT / "log" / "signup-protocol.har",
        ROOT / "tests" / "fixtures" / "signup_har_excerpt.json",
        ROOT / "cffi_har_signup" / "tests" / "fixtures" / "signup_har_excerpt.json",
    ):
        if candidate.is_file():
            return candidate
    return ROOT / "log" / "signup-protocol-full.har"


def signup_with_patchright(
    *,
    har: Any,
    proxy: str,
    email: str,
    wait_otp: Callable[[float], str],
    solve_ts: Callable[[str], str],
    log: Callable[[str], None] | None = None,
) -> dict:
    """HAR auth trio through Patchright Chromium. Returns email/profile/sso. No Camoufox."""
    emit = log or (lambda _msg: None)
    emit("engine=patchright camoufox=no")
    if _asyncio_loop_running():
        return run_patchright_sync(
            lambda: signup_with_patchright(
                har=har,
                proxy=proxy,
                email=email,
                wait_otp=wait_otp,
                solve_ts=solve_ts,
                log=log,
            )
        )
    from patchright.sync_api import sync_playwright

    pw = _rand_password()
    given = random.choice(FIRST)
    family = random.choice(LAST)
    proxy_cfg = _proxy_for_patchright(proxy)
    user_dir = tempfile.mkdtemp(prefix="patchright-ctx-")
    headed = bool(os.environ.get("DISPLAY") or os.environ.get("PATCHRIGHT_HEADED"))
    chrome = _chrome_path()
    sso = ""
    with sync_playwright() as playwright:
        emit("patchright driver entered")
        launch_kw: dict[str, Any] = {
            "headless": not headed,
            "no_viewport": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if proxy_cfg:
            launch_kw["proxy"] = proxy_cfg
        if chrome:
            launch_kw["executable_path"] = chrome
        context = playwright.chromium.launch_persistent_context(user_dir, **launch_kw)
        browser = context.browser
        net: dict[str, Any] = {}

        def on_request(request):
            _capture_auth_headers(net, request)
            url = str(request.url or "")
            if "/api/auth/" in url:
                emit(
                    f"net {request.method} {url} "
                    f"keys={list((net.get('json') or net.get('verify_json') or net.get('create_json') or {}))[:8]}"
                )

        context.on("request", on_request)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(SIGNUP_URL, wait_until="load", timeout=60000)
            try:
                page.wait_for_function(
                    "() => /castlePk/.test(document.documentElement.innerHTML) || !!(window.Castle && window.Castle.createRequestToken)",
                    timeout=30000,
                )
            except Exception as exc:
                emit(f"castlePk wait: {exc}")
            page.wait_for_timeout(1500)
            # Patchright isolates Runtime.enable, so window.Castle is often
            # invisible to evaluate. Prefer the live page XHR as Castle source.
            emit("capture live send-code XHR from page submit")
            sent_at = time.time()
            captured = _capture_send_code(
                page, email, emit, timeout=45.0, captured=net
            )
            sent_via_page = _send_code_succeeded(captured, otp_visible=_otp_visible(page))
            minted = {}
            if not captured.get("token") and not sent_via_page:
                emit("page XHR empty; try Castle SDK evaluate")
                minted = _mint_castle(page, email, emit, timeout=8.0)
            castle = _castle_from_capture_or_mint(captured, minted)
            if not castle.get("token") and not sent_via_page:
                title = ""
                try:
                    title = page.title()
                except Exception:
                    title = ""
                raise RuntimeError(
                    f"patchright castle SDK empty how={castle.get('how')} "
                    f"title={title!r} url={page.url} auth={captured.get('auth_urls')!r}"[:500]
                )
            if sent_via_page and not castle.get("token"):
                emit("send-code succeeded on page without captured castle; continue")
            sentry = _sentry(page)
            if captured.get("sentry_trace"):
                sentry = {
                    "sentry_trace": captured.get("sentry_trace") or "",
                    "baggage": captured.get("baggage") or "",
                    "how": captured.get("sentry_how") or "page_request_headers",
                }
            emit(f"sentry how={sentry.get('how')} castle_how={castle.get('how')}")
            try:
                sitekey = str(page.evaluate(SITEKEY_JS) or "").strip()
            except Exception:
                sitekey = ""
            if sent_via_page:
                emit("send-code already fired by live page SDK")
            elif castle.get("token"):
                send = build_send_verification_code(
                    email=email, castle_request_token=castle["token"]
                )
                assert_send_code_has_castle(send)
                st, data, _raw = _post(page, har, send, sentry, emit)
                if st >= 400 or st == 0:
                    raise RuntimeError(f"send-code {st} {str(data)[:180]}")
            else:
                raise RuntimeError("send-code not fired and castle missing")
            code = wait_otp(sent_at)
            emit(f"otp len={len(code)}")
            verify = apply_live_otp(build_verify_email(email=email, code=code), code, "code")
            st, data, _raw = _post(page, har, verify, sentry, emit)
            if (st >= 400 or st == 0) and "-" in code:
                verify = build_verify_email(email=email, code=code)
                st, data, _raw = _post(page, har, verify, sentry, emit)
            if st >= 400 or st == 0:
                emit(f"HAR verify-email {st}; fill OTP on live page")
                _fill_otp_and_confirm(page, code, emit)
                page.wait_for_timeout(1500)
                if captured.get("verify_json") or not _otp_visible(page):
                    st, data = 200, captured.get("verify_json") or {"page": True}
            if st >= 400 or st == 0:
                raise RuntimeError(f"verify-email {st} {str(data)[:180]}")
            ts_token = solve_ts(sitekey or "0x4AAAAAAAhr9JGVDZbrZOo0")
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
            context.close()
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            shutil.rmtree(user_dir, ignore_errors=True)
    if not sso:
        raise RuntimeError("patchright signup returned no sso")
    return {
        "email": email,
        "sso": sso,
        "password": pw,
        "given_name": given,
        "family_name": family,
        "engine": "patchright",
        "camoufox": False,
    }


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
    from grok2api_import import import_build_sso
    from grok_onboard import onboard_new_account
    from guard_check import classify, register_check, should_retry_guard
    from outlook_min import wait_for_code
    from signup_protocol import make_protocol_session
    from turnstile_capsolver import solve_turnstile

    emit = log or (lambda _msg: None)
    email = account["email"]

    def wait_otp(after_ts: float) -> str:
        return str(account.get("otp") or "").strip() or wait_for_code(account, after_ts=after_ts)

    def solve_ts(sitekey: str) -> str:
        return solve_turnstile(
            capsolver_key,
            website_url=SIGNUP_URL,
            website_key=sitekey or "0x4AAAAAAAhr9JGVDZbrZOo0",
            api=capsolver_api,
        )

    outcome = signup_with_patchright(
        har=har,
        proxy=proxy,
        email=email,
        wait_otp=wait_otp,
        solve_ts=solve_ts,
        log=emit,
    )
    sso = str(outcome.get("sso") or "")
    email = str(outcome.get("email") or email)

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
