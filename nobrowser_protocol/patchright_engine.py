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
    FILL_CONTROL_JS,
    FILL_EMAIL_JS,
    FILL_OTP_JS,
    INJECT_TURNSTILE_JS,
    OTP_VISIBLE_JS,
    PAGE_POST_JS,
    PROFILE_VISIBLE_JS,
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
GROK_SIGNUP_URL = "https://grok.com/sign-up"
GIVEN_SELECTOR = 'input[name="givenName"], input[autocomplete="given-name"], input[data-testid="givenName"]'
FAMILY_SELECTOR = 'input[name="familyName"], input[autocomplete="family-name"], input[data-testid="familyName"]'
PASSWORD_SELECTOR = 'input[name="password"], input[autocomplete="new-password"], input[type="password"]'


class ExistingEmailError(RuntimeError):
    """create-account said this mailbox is already registered on x.ai."""

    def __init__(self, email: str, payload: Any = None):
        self.email = str(email or "").strip()
        self.payload = payload if isinstance(payload, dict) else {}
        super().__init__(f"existingEmail={self.email} keys={list(self.payload)[:8]}")


def existing_email_payload(data: Any) -> bool:
    """True when create-account returned an already-registered mailbox, not SSO."""
    if not isinstance(data, dict):
        return False
    if data.get("existingEmail"):
        return True
    methods = data.get("signInMethods")
    if methods and not sso_from_create_payload(data):
        return True
    return False
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
        posted = str(body.get("email") or "")
        expect = str(captured.get("expect_email") or "")
        captured["send_email_len"] = len(posted)
        captured["send_email_match"] = bool(expect) and posted == expect
    if AUTH_VERIFY_EMAIL in url:
        captured["verify_json"] = body
    if AUTH_CREATE_ACCOUNT in url:
        captured["create_json"] = body


def _otp_visible(page) -> bool:
    try:
        return bool(page.evaluate(OTP_VISIBLE_JS))
    except Exception:
        return False


def _send_code_fired(captured: dict | None, otp_visible: bool = False) -> bool:
    """The page submitted send-code. This does not confirm a 2xx response."""
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


def _send_code_succeeded(captured: dict | None, otp_visible: bool = False) -> bool:
    """True only when the send-code response is 2xx, or the OTP form is already up.

    A captured request body means the browser fired the call. It does not mean
    x.ai accepted it and sent mail.
    """
    cap = captured or {}
    status = cap.get("send_status")
    try:
        status_code = int(status or 0)
    except (TypeError, ValueError):
        status_code = 0
    if status_code:
        return 200 <= status_code < 300
    how = str(cap.get("how") or "")
    if how == "page_otp_form" or otp_visible:
        return True
    return False


def _otp_for_page(code: str) -> str:
    """The live form posts a 6-digit code, without a hyphen."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) >= 6:
        return digits[:6]
    return str(code or "").strip()


def _fill_otp_and_confirm(page, code: str, log: Callable[[str], None]) -> str:
    value = _otp_for_page(code)
    filled = ""
    selector = 'input[name="code"], input[autocomplete="one-time-code"], input[data-input-otp]'
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=8000)
        loc.click(timeout=3000)
        loc.fill("", timeout=3000)
        loc.fill(value, timeout=5000)
        filled = value
    except Exception as exc:
        log(f"otp locator fill: {exc}")
    try:
        react = page.evaluate(FILL_CONTROL_JS, {"selector": selector, "value": value}) or {}
        log(f"otp react fill={react!r}")
        if react.get("ok"):
            filled = value
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


EMAIL_INPUT_SELECTOR = (
    '#email:not([readonly]):visible, '
    'input[data-testid="email"]:not([readonly]):visible, '
    'input[autocomplete="email"]:not([readonly]):visible'
)


def _fill_signup_email(page, email: str, log: Callable[[str], None]) -> dict:
    """Write the address into the visible email-step field and its React state."""
    try:
        loc = page.locator(EMAIL_INPUT_SELECTOR).first
        loc.wait_for(state="visible", timeout=20000)
        loc.fill(email, timeout=8000)
    except Exception as exc:
        log(f"locator fill: {exc}")
    try:
        result = page.evaluate(FILL_EMAIL_JS, email) or {}
    except Exception as exc:
        log(f"react fill: {exc}")
        result = {"ok": False, "reason": str(exc)[:160]}
    if not isinstance(result, dict):
        result = {"ok": False, "reason": "bad-result"}
    log(
        f"email fill ok={bool(result.get('ok'))} id={result.get('id')!r} "
        f"auto={result.get('auto')!r} len={len(str(result.get('value') or ''))}"
    )
    return result


def _capture_send_code(
    page,
    email: str,
    log: Callable[[str], None],
    timeout: float = 45.0,
    captured: dict[str, Any] | None = None,
) -> dict:
    """Let the live page SDK fire send-code; capture castleRequestToken from that XHR."""
    captured = captured if captured is not None else {}
    captured["expect_email"] = email

    def on_request(request):
        _capture_auth_headers(captured, request)
        url = str(request.url or "")
        if AUTH_SEND_CODE in url and str(request.method or "").upper() == "POST":
            log(
                f"send-code request email_len={captured.get('send_email_len')} "
                f"match={captured.get('send_email_match')}"
            )

    def on_response(response):
        try:
            if AUTH_SEND_CODE not in str(response.url or ""):
                return
            if str(getattr(response.request, "method", "") or "").upper() != "POST":
                return
            status = int(response.status)
            code = ""
            if status >= 400:
                try:
                    data = response.json()
                except Exception:
                    data = None
                if isinstance(data, dict):
                    code = str(data.get("code") or data.get("error") or "")[:120]
                    captured["send_error"] = code
            captured["send_status"] = status
            if code:
                log(f"send-code response status={status} code={code}")
            else:
                log(f"send-code response status={status}")
        except Exception as exc:
            log(f"send-code response read: {exc}")

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        _open_email_form(page, log)
        filled = _fill_signup_email(page, email, log)
        clicked = ""
        if filled.get("ok"):
            clicked = str(page.evaluate(SUBMIT_EMAIL_JS) or "")
            if clicked in {"", "no-submit", "no-visible-email"}:
                clicked = _click_first(page, SUBMIT_NEEDLES, timeout=5000, deny=SUBMIT_DENY)
        else:
            log("email fill missed; skip submit")
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


def _await_send_status(captured: dict, timeout: float = 8.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = int(captured.get("send_status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status:
            return status
        time.sleep(0.2)
    try:
        return int(captured.get("send_status") or 0)
    except (TypeError, ValueError):
        return 0


_NO_RESEND_SEND_CODES = frozenset(
    {
        "account:email-signup-unavailable",
        "account:email-in-use",
        "account:email-malformed",
        "account:email-domain-rejected",
        "account:email-domain-pending",
        "email:send-validation-code-rate-limited",
        "rate-limit:too-many-requests",
    }
)
_SWITCH_EXIT_MAILBOX_CODES = frozenset(
    {
        "account:email-signup-unavailable",
        "email:send-validation-code-rate-limited",
    }
)


class SendCodeSwitchError(Exception):
    """x.ai rejected send-code. Caller switches exit and mailbox."""

    def __init__(self, code: str, email: str = "") -> None:
        self.code = str(code or "").strip()
        self.email = str(email or "").strip()
        super().__init__(f"send-code {self.code}")


def raise_send_code_rejection(status: object, error_code: str, email: str = "") -> None:
    code = str(error_code or "").strip()
    if code in _SWITCH_EXIT_MAILBOX_CODES:
        raise SendCodeSwitchError(code, email)
    detail = f" code={code}" if code else ""
    raise RuntimeError(f"send-code not accepted status={status}{detail}")


def _send_code_resend_allowed(error_code: str) -> bool:
    """Immediate Sign up clicks do not clear a terminal or rate-limit rejection."""
    code = str(error_code or "").strip()
    if not code:
        return True
    if code.startswith("form-invalid:"):
        return False
    return code not in _NO_RESEND_SEND_CODES


def _resend_verification_code(
    page,
    captured: dict,
    log: Callable[[str], None],
    email: str,
) -> int:
    """Retry a rejected send. OTP step clicks Resend; email step refills then submits."""
    for attempt in range(1, 4):
        previous = int(captured.get("send_status") or 0)
        captured["send_status"] = 0
        if _otp_visible(page):
            clicked = _click_first(
                page,
                ("Resend code", "Resend", "Send again", "Send code", "重新发送", "重发"),
                timeout=4000,
                deny=("privacy", "with email", "sign up"),
            )
        else:
            filled = _fill_signup_email(page, email, log)
            if not filled.get("ok"):
                log(f"send-code resend {attempt}/3 skipped submit; email field empty")
                continue
            clicked = str(page.evaluate(SUBMIT_EMAIL_JS) or "")
        log(f"send-code resend {attempt}/3 click={clicked!r}")
        status = _await_send_status(captured, timeout=12.0)
        if not status:
            status = previous
            captured["send_status"] = previous
        log(f"send-code resend {attempt}/3 status={status}")
        if 200 <= status < 300:
            return status
        error_code = str(captured.get("send_error") or "")
        if error_code and not _send_code_resend_allowed(error_code):
            log(f"send-code resend {attempt}/3 stop code={error_code}")
            return status
    try:
        return int(captured.get("send_status") or 0)
    except (TypeError, ValueError):
        return 0


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


def _cookie_chain_url(url: str) -> bool:
    text = str(url or "")
    return any(part in text for part in ("cookie-chain", "delete-cookie", "set-cookie"))


def _wait_cookie_chain(page, log: Callable[[str], None], timeout: float = 40.0, grace: float = 3.0) -> str:
    """Let the page finish the cookie-chain hops the manual signup walks."""
    started = time.time()
    saw = False
    while time.time() - started < timeout:
        url = str(getattr(page, "url", "") or "")
        if _cookie_chain_url(url):
            saw = True
            page.wait_for_timeout(400)
            continue
        if saw or time.time() - started >= grace:
            if saw:
                log(f"cookie-chain done url={url[:140]}")
            return url
        page.wait_for_timeout(250)
    url = str(getattr(page, "url", "") or "")
    log(f"cookie-chain wait expired url={url[:140]}")
    return url


def _chrome_error_navigation(text: object) -> bool:
    raw = str(text or "")
    return "chrome-error://chromewebdata" in raw or "interrupted by another navigation" in raw


def _goto_accounts_signup(page, log: Callable[[str], None]) -> None:
    """Open the accounts sign-up page. A chrome-error interrupt is retried, not fatal."""
    last = ""
    for attempt in range(1, 4):
        try:
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            last = str(exc)
            log(f"sign-up goto {attempt}/3: {exc}")
            if not _chrome_error_navigation(exc) and attempt >= 3:
                raise
            page.wait_for_timeout(600)
            continue
        url = str(getattr(page, "url", "") or "")
        if not _chrome_error_navigation(url):
            return
        last = url
        log(f"sign-up landed on chrome-error {attempt}/3")
        page.wait_for_timeout(600)
    raise RuntimeError(
        f'Page.goto: Navigation to "{SIGNUP_URL}" is interrupted by another navigation to "{last}"'
    )


def _open_signup_page(page, log: Callable[[str], None]) -> None:
    """Match the manual entry: grok.com/sign-up, then accounts sign-up, then cookie-chain."""
    try:
        page.goto(GROK_SIGNUP_URL, wait_until="commit", timeout=20000)
    except Exception as exc:
        log(f"grok.com/sign-up: {exc}")
    _goto_accounts_signup(page, log)
    _wait_cookie_chain(page, log)
    if _chrome_error_navigation(getattr(page, "url", "")) or "sign-up" not in str(
        getattr(page, "url", "") or ""
    ):
        _goto_accounts_signup(page, log)
        _wait_cookie_chain(page, log, grace=2.0)


def _await_flag(captured: dict, key: str, timeout: float = 20.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = int(captured.get(key) or 0)
        except (TypeError, ValueError):
            status = 0
        if status:
            return status
        time.sleep(0.25)
    return 0


def _profile_visible(page) -> bool:
    try:
        return bool(page.evaluate(PROFILE_VISIBLE_JS))
    except Exception:
        return False


def _fill_named(page, selector: str, value: str, log: Callable[[str], None]) -> dict:
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=12000)
        loc.click(timeout=3000)
        loc.fill("", timeout=3000)
        loc.fill(value, timeout=8000)
    except Exception as exc:
        log(f"profile locator {selector}: {exc}")
    try:
        result = page.evaluate(FILL_CONTROL_JS, {"selector": selector, "value": value}) or {}
    except Exception as exc:
        result = {"ok": False, "reason": str(exc)[:160]}
    return result if isinstance(result, dict) else {"ok": False}


def _type_password(page, password: str, log: Callable[[str], None]) -> None:
    """Type the password so the page fires ValidatePassword, then sync React state."""
    try:
        loc = page.locator(PASSWORD_SELECTOR).first
        loc.wait_for(state="visible", timeout=8000)
        loc.click(timeout=3000)
        loc.fill("", timeout=3000)
        loc.press_sequentially(password, delay=40)
    except Exception as exc:
        log(f"password type: {exc}")
    _fill_named(page, PASSWORD_SELECTOR, password, log)


def _submit_profile(page, log: Callable[[str], None]) -> str:
    clicked = _click_first(
        page,
        ("Complete sign up", "Create account", "Sign up", "Continue", "完成注册", "继续"),
        timeout=5000,
        deny=("with email", "with apple", "with google", "privacy", "go back", "resend"),
    )
    if clicked:
        return clicked
    try:
        return str(page.evaluate(SUBMIT_EMAIL_JS) or "")
    except Exception as exc:
        log(f"profile submit: {exc}")
        return ""


def _sso_from_context(context) -> str:
    try:
        cookies = context.cookies()
    except Exception:
        return ""
    for cookie in cookies or []:
        if str(cookie.get("name") or "").lower() in {"sso", "sso-rw"}:
            value = str(cookie.get("value") or "").strip()
            if value:
                return value
    return ""


def _note_auth_response(captured: dict, response) -> None:
    url = str(getattr(response, "url", "") or "")
    method = str(getattr(getattr(response, "request", None), "method", "") or "").upper()
    if method != "POST":
        return
    if AUTH_VERIFY_EMAIL in url:
        key, data_key = "verify_status", "verify_data"
    elif AUTH_CREATE_ACCOUNT in url:
        key, data_key = "create_status", "create_data"
    else:
        return
    try:
        captured[key] = int(response.status)
    except (TypeError, ValueError):
        captured[key] = 0
    try:
        data = response.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        captured[data_key] = data


def signup_with_patchright(
    *,
    har: Any,
    proxy: str,
    email: str,
    wait_otp: Callable[[float], str],
    solve_ts: Callable[[str], str],
    log: Callable[[str], None] | None = None,
    usage: Any = None,
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
                usage=usage,
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
        net: dict[str, Any] = {"expect_email": email}

        def on_request(request):
            _capture_auth_headers(net, request)
            url = str(request.url or "")
            if "/api/auth/" in url:
                emit(
                    f"net {request.method} {url} "
                    f"keys={list((net.get('json') or net.get('verify_json') or net.get('create_json') or {}))[:8]}"
                )

        def on_response(response):
            try:
                _note_auth_response(net, response)
            except Exception as exc:
                emit(f"auth response: {exc}")

        context.on("request", on_request)
        context.on("response", on_response)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _open_signup_page(page, emit)
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
            otp_visible = _otp_visible(page)
            sent_via_page = _send_code_fired(captured, otp_visible=otp_visible)
            accepted = _send_code_succeeded(captured, otp_visible=otp_visible)
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
                status = _await_send_status(captured) if not accepted else int(captured.get("send_status") or 0)
                emit(f"send-code response status={status or 'unconfirmed'}")
                if not accepted and not (200 <= status < 300):
                    error_code = str(captured.get("send_error") or "")
                    if error_code and not _send_code_resend_allowed(error_code):
                        raise_send_code_rejection(status or "missing", error_code, email)
                    emit(f"send-code not accepted status={status or 'missing'}; resend")
                    status = _resend_verification_code(page, captured, emit, email)
                    sent_at = time.time()
                    accepted = 200 <= status < 300 or _otp_visible(page)
                if not accepted:
                    error_code = str(captured.get("send_error") or "")
                    if error_code in _SWITCH_EXIT_MAILBOX_CODES:
                        raise SendCodeSwitchError(error_code, email)
                    detail = f" code={error_code}" if error_code else ""
                    raise RuntimeError(
                        f"send-code not accepted status={status} after 3 resends{detail}"
                    )
                if usage is not None:
                    usage.email_used = True
                emit("send-code accepted")
            elif castle.get("token"):
                send = build_send_verification_code(
                    email=email, castle_request_token=castle["token"]
                )
                assert_send_code_has_castle(send)
                st = 0
                data: Any = {}
                for attempt in range(1, 4):
                    st, data, _raw = _post(page, har, send, sentry, emit)
                    emit(f"send-code post {attempt}/3 status={st}")
                    if 200 <= st < 300:
                        break
                if st >= 400 or st == 0:
                    raise RuntimeError(f"send-code {st} {str(data)[:180]} after 3 resends")
                if usage is not None:
                    usage.email_used = True
            else:
                raise RuntimeError("send-code not fired and castle missing")
            if usage is not None:
                usage.email_used = True
            code = _otp_for_page(wait_otp(sent_at))
            emit(f"otp digits={len(code)}")
            _fill_otp_and_confirm(page, code, emit)
            st = _await_flag(captured, "verify_status", timeout=20)
            data = captured.get("verify_data") if isinstance(captured.get("verify_data"), dict) else {}
            if not (200 <= st < 300) and _profile_visible(page):
                st, data = 200, data or {"page": True}
            if not (200 <= st < 300):
                emit(f"page verify status={st or 'missing'}; fetch fallback")
                verify = build_verify_email(email=email, code=code)
                st, data, _raw = _post(page, har, verify, sentry, emit)
            if st >= 400 or st == 0:
                raise RuntimeError(f"verify-email {st} {str(data)[:180]}")
            emit(f"verify-email status={st}")
            if usage is not None:
                usage.remote_captcha_used = True
            deadline = time.time() + 20
            while time.time() < deadline and not _profile_visible(page):
                page.wait_for_timeout(300)
            given_fill = _fill_named(page, GIVEN_SELECTOR, given, emit)
            family_fill = _fill_named(page, FAMILY_SELECTOR, family, emit)
            _type_password(page, pw, emit)
            emit(
                f"profile given={bool(given_fill.get('ok'))} "
                f"family={bool(family_fill.get('ok'))}"
            )
            try:
                sitekey = str(page.evaluate(SITEKEY_JS) or sitekey or "").strip()
            except Exception:
                sitekey = sitekey or ""
            ts_token = solve_ts(sitekey or "0x4AAAAAAAhr9JGVDZbrZOo0")
            emit(f"turnstile len={len(ts_token)}")
            try:
                injected = page.evaluate(INJECT_TURNSTILE_JS, ts_token)
            except Exception as exc:
                injected = {"ok": False, "reason": str(exc)[:160]}
            emit(f"turnstile inject={injected!r}")
            if _profile_visible(page):
                clicked = _submit_profile(page, emit)
            else:
                clicked = ""
                _dump_page(page, emit, "profile-missing")
            emit(f"profile submit={clicked!r}")
            st = _await_flag(captured, "create_status", timeout=30)
            data = captured.get("create_data") if isinstance(captured.get("create_data"), dict) else {}
            if not (200 <= st < 300):
                emit(f"page create status={st or 'missing'}; fetch fallback")
                minted = _mint_castle(page, email, emit, timeout=8.0)
                fresh = str((minted or {}).get("token") or "").strip() or str(castle.get("token") or "")
                create = build_create_account(
                    email=email,
                    password=pw,
                    given_name=given,
                    family_name=family,
                    email_validation_code=code,
                    turnstile_token=ts_token,
                    castle_request_token=fresh,
                )
                create = apply_live_otp(create, code, "emailValidationCode")
                st, data, _raw = _post(page, har, create, sentry, emit)
            if st >= 400 or st == 0:
                raise RuntimeError(f"create-account {st} {str(data)[:180]}")
            emit(f"create-account status={st}")
            redirect = str(data.get("redirectUrl") or "") if isinstance(data, dict) else ""
            if redirect and not _cookie_chain_url(str(getattr(page, "url", "") or "")):
                try:
                    page.goto(redirect, wait_until="load", timeout=45000)
                except Exception as exc:
                    emit(f"cookie-chain goto: {exc}")
            _wait_cookie_chain(page, emit, timeout=50, grace=2)
            sso = _sso_from_context(context) or sso_from_create_payload(data)
            if not sso and existing_email_payload(data):
                emit(f"existingEmail={email} keys={list(data)[:8]}")
                raise ExistingEmailError(email, data)
            if not sso:
                raise RuntimeError(
                    f"no sso after cookie-chain keys={list(data)[:12] if isinstance(data, dict) else type(data)}"
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
