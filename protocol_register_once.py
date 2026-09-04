#!/usr/bin/env python3
"""Protocol signup: Camoufox for Castle/cookies/companions; YesCaptcha only for Turnstile."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def apply_protocol_runtime_env() -> None:
    """Env needed by the HAR protocol path. Safe to call more than once."""
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    os.environ.setdefault("XDG_CACHE_HOME", "/data/claw-relocated/camoufox-cache")
    if not str(os.environ.get("TURNSTILE_PROVIDER") or "").strip():
        os.environ["TURNSTILE_PROVIDER"] = "yescaptcha"
    # HAR send-code Accept-Language is en-US,en; geoip locale turns the UI into
    # French/Spanish and open_signup_page then clicks OAuth ("S'inscrire avec X").
    os.environ["GROK_FP_ENHANCE"] = "0"
    try:
        os.chdir(str(ROOT))
    except Exception:
        pass
    cfg_path = ROOT / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            cfg = {}
        if isinstance(cfg, dict):
            if cfg.get("yescaptcha_key") and not str(os.environ.get("YESCAPTCHA_KEY") or "").strip():
                os.environ["YESCAPTCHA_KEY"] = str(cfg["yescaptcha_key"]).strip()
            if cfg.get("yescaptcha_api") and not str(os.environ.get("YESCAPTCHA_API") or "").strip():
                os.environ["YESCAPTCHA_API"] = str(cfg["yescaptcha_api"]).strip()
            if cfg.get("capsolver_key") and not str(os.environ.get("CAPSOLVER_KEY") or "").strip():
                os.environ["CAPSOLVER_KEY"] = str(cfg["capsolver_key"]).strip()
            if cfg.get("capsolver_api") and not str(os.environ.get("CAPSOLVER_API") or "").strip():
                os.environ["CAPSOLVER_API"] = str(cfg["capsolver_api"]).strip()
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
    RegistrationCancelled,
    RegistrationRiskDenied,
    _registration_risk_should_block,
    _wire_runtime_modules,
    add_sso_to_cpa,
    ensure_sso_oauth_eligible,
    release_browser_before_cpa,
    get_email_and_token,
    get_oai_code,
    load_config,
    pick_proxy_for_worker,
    set_thread_proxy,
)
import grok_register_ttk as app  # noqa: E402
from browser_session import (  # noqa: E402
    get_bound_proxy,
    restart_browser,
    start_browser,
    stop_browser,
    try_rotate_proxy,
)
from retry_policy import (  # noqa: E402
    ReusableResourceError,
    protocol_pre_inbox_retries,
)
from register_flow import (  # noqa: E402
    _native_click_action,
    _native_fill_email,
    build_profile,
    open_signup_page,
    page,
)
from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_ORIGIN,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    assert_json_keys_match_capture,
    assert_send_code_has_castle,
    auth_cookie_names_from_har,
    auth_header_names_from_har,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    companion_class_for_url,
    companion_classes_from_har,
    required_companion_classes_from_har,
    extract_signup_calls,
    fetch_settable_headers,
    json_keys_for_path,
    extra_live_json_keys,
    har_json_key_union,
    live_names_subset_of_har,
    missing_live_names,
    hard_missing_live_cookie_names,
    overlay_live_on_har_names,
    parse_inpage_post_result,
    sso_from_create_payload,
)
from turnstile_solver import DEFAULT_XAI_SITEKEY, solve_turnstile  # noqa: E402

HAR_PATH = ROOT / "log" / "signup-protocol-full.har"
SURFACE_PATH = ROOT / "log" / "protocol-live-request-surface.json"


class ProtocolResourceUsage:
    """Track whether a protocol attempt spent mailbox or remote captcha."""

    __slots__ = ("email_used", "remote_captcha_used")

    def __init__(self) -> None:
        self.email_used = False
        self.remote_captcha_used = False


def recycle_protocol_browser(log_callback=None) -> None:
    """Reopen Camoufox and switch exit if the worker registered a rotate hook."""
    emit = log_callback or log
    rotated = try_rotate_proxy("protocol pre-inbox retry", emit)
    if rotated:
        emit("[*] 协议预检：已换出口")
    emit("[*] 协议预检：重开浏览器")
    restart_browser(log_callback=emit)


def retry_protocol_pre_inbox(
    run_once,
    *,
    recycle,
    attempts: int,
    log_fn,
    cancel=None,
    usage_factory=ProtocolResourceUsage,
):
    """Retry `run_once` while mailbox and remote captcha are still unused."""
    last_exc: Exception | None = None
    total = max(1, int(attempts or 1))
    for attempt in range(1, total + 1):
        if callable(cancel):
            cancel()
        usage = usage_factory()
        try:
            return run_once(usage)
        except RegistrationCancelled:
            raise
        except Exception as exc:
            if getattr(usage, "email_used", False) or getattr(
                usage, "remote_captcha_used", False
            ):
                raise
            last_exc = exc
            log_fn(f"[!] 协议收件前失败 {attempt}/{total}: {exc}")
            if attempt < total and callable(recycle):
                recycle()
                continue
            raise ReusableResourceError(str(exc)) from exc
    raise ReusableResourceError(str(last_exc or "protocol pre-inbox failed"))

CASTLE_JS = """
async () => {
  const tryOne = async (how, fn) => {
    try {
      const token = await fn();
      if (token) return { token: String(token), how };
    } catch (e) {}
    return null;
  };
  if (window.Castle && typeof window.Castle.createRequestToken === 'function') {
    const hit = await tryOne('Castle.createRequestToken', () => window.Castle.createRequestToken());
    if (hit) return hit;
  }
  if (typeof window._castle === 'function') {
    const hit = await tryOne('_castle', () => window._castle('createRequestToken'));
    if (hit) return hit;
  }
  return { token: '', how: 'missing' };
}
"""

SENTRY_JS = """
() => {
  const out = { sentry_trace: '', baggage: '', how: 'missing' };
  try {
    const S = window.Sentry;
    if (S && typeof S.getTraceData === 'function') {
      const d = S.getTraceData() || {};
      out.sentry_trace = String(d['sentry-trace'] || d.sentry_trace || '');
      out.baggage = String(d.baggage || '');
      out.how = 'Sentry.getTraceData';
      return out;
    }
  } catch (e) {}
  return out;
}
"""

SITEKEY_JS = """
() => {
  const keys = [];
  const push = (v) => {
    v = String(v || '').trim();
    if (/^0x[0-9A-Za-z_-]{8,}$/.test(v)) keys.push(v);
  };
  document.querySelectorAll('[data-sitekey]').forEach((el) => push(el.getAttribute('data-sitekey')));
  document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').forEach((el) => {
    const src = String(el.src || '');
    const m = src.match(/0x4[0-9A-Za-z_-]{8,}/);
    if (m) push(m[0]);
  });
  return keys[0] || '';
}
"""

AUTH_HOOK_INIT = """
if (!window.__grokAuthHooked) {
  window.__grokAuthHooked = true;
  window.__grokAuthPosts = [];
  const stash = (url, body, status) => {
    window.__grokAuthPosts.push({
      url: String(url || ''),
      body: String(body || '').slice(0, 20000),
      status: status || 0,
      t: Date.now(),
    });
  };
  const asText = (body) => {
    if (body == null) return '';
    if (typeof body === 'string') return body;
    try { return JSON.stringify(body); } catch (e) { return String(body); }
  };
  const origFetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const body = asText(init && init.body);
    const res = await origFetch.apply(this, arguments);
    if (String(url).includes('/api/auth/')) {
      try { stash(url, body, res.status); } catch (e) {}
    }
    return res;
  };
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__grokUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    this.addEventListener('load', function() {
      if (String(this.__grokUrl || '').includes('/api/auth/')) {
        stash(this.__grokUrl, asText(body), this.status);
      }
    });
    return origSend.apply(this, arguments);
  };
}
"""

AUTH_HOOK_JS = "() => { " + AUTH_HOOK_INIT + " return !!window.__grokAuthHooked; }"

READ_AUTH_HOOK_JS = """
() => window.__grokAuthPosts || []
"""

FILL_REACT_JS = """
(email) => {
  const input = document.querySelector('input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]');
  if (!input) return { ok: false, reason: 'no-input' };
  input.focus();
  input.click();
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) setter.call(input, email); else input.value = email;
  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
  input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.blur();
  const valid = (input.getAttribute('type') || '').toLowerCase() !== 'email' || input.checkValidity();
  return { ok: (input.value || '') === email && valid, value: input.value || '', valid };
}
"""

SUBMIT_REACT_JS = """
() => {
  const deny = /with email|使用邮箱|correo|regresar|privacidad|go back|privacy/i;
  const input = document.querySelector('input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]');
  const nodes = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'));
  const hit = nodes.find((n) => {
    if (n.disabled || n.getAttribute('aria-disabled') === 'true') return false;
    const t = ((n.innerText || n.textContent || n.value || '') + '').replace(/\\s+/g, ' ').trim();
    const testid = (n.getAttribute('data-testid') || '').toLowerCase();
    const type = (n.getAttribute('type') || '').toLowerCase();
    if (deny.test(t)) return false;
    if (type === 'submit') return true;
    if (testid.includes('submit') || testid.includes('continue') || testid === 'sign-up') return true;
    return /sign\\s*up|continue|next|注册|继续|regístrate|registrate|registrieren|inscrire|registrati|inscrever/i.test(t);
  });
  if (hit) {
    hit.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
    hit.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
    hit.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    hit.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
    hit.click();
  }
  const form = (input && input.closest('form')) || document.querySelector('form');
  if (form) {
    if (typeof form.requestSubmit === 'function') {
      try { form.requestSubmit(hit || undefined); } catch (e) { try { form.requestSubmit(); } catch (e2) {} }
    } else {
      form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    }
  }
  if (input) {
    input.focus();
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true }));
  }
  return hit ? ((hit.innerText || hit.getAttribute('data-testid') || 'clicked') + '').replace(/\\s+/g, ' ').trim().slice(0, 80) : (form ? 'form-submit' : 'enter');
}
"""

PAGE_FORM_JS = """
() => {
  const email = document.querySelector('input[type="email"], input[name="email"], input[data-testid="email"]');
  const code = document.querySelector('input[data-input-otp], input[autocomplete="one-time-code"], input[name="code"], input[inputmode="numeric"]');
  return {
    url: location.href,
    emailValue: email ? String(email.value || '') : '',
    hasEmail: !!(email && email.getClientRects().length),
    hasCode: !!(code && code.getClientRects().length),
  };
}
"""

CASTLE_MINT_JS = """
async (email) => {
  const html = document.documentElement ? document.documentElement.innerHTML : '';
  const pkMatch = html.match(/"castlePk":"(pk_[^"]+)"/);
  const pk = pkMatch ? pkMatch[1] : '';
  let mint = null;
  const seen = new Set();
  const consider = (bag) => {
    if (!bag || typeof bag !== 'object') return;
    if (typeof bag.createRequestToken === 'function') mint = bag.createRequestToken;
    else if (bag.value && typeof bag.value.createRequestToken === 'function') mint = bag.value.createRequestToken;
    else if (bag._currentValue && typeof bag._currentValue.createRequestToken === 'function') mint = bag._currentValue.createRequestToken;
  };
  const walk = (fiber, depth) => {
    if (!fiber || depth > 80 || seen.has(fiber) || mint) return;
    seen.add(fiber);
    consider(fiber.memoizedProps);
    consider(fiber.pendingProps);
    consider(fiber.memoizedState);
    consider(fiber.stateNode);
    consider(fiber.type);
    try { walk(fiber.child, depth + 1); } catch (e) {}
    if (mint) return;
    try { walk(fiber.sibling, depth + 1); } catch (e) {}
  };
  const roots = [document.getElementById('__next'), document.querySelector('[data-reactroot]'), document.body];
  for (const root of roots) {
    if (!root) continue;
    for (const key of Object.keys(root)) {
      if (key.startsWith('__react')) walk(root[key], 0);
    }
  }
  if (!mint) return { token: '', how: 'react_missing', pk };
  try {
    const token = await mint({ method: 'email_password', flow: 'signup', email });
    return { token: String(token || ''), how: 'useCastle.createRequestToken', pk };
  } catch (e) {
    return { token: '', how: 'mint_error:' + String((e && e.message) || e).slice(0, 120), pk };
  }
}
"""

PAGE_POST_JS = """
async ({ url, body, headers }) => {
  const hdrs = Object.assign({ 'content-type': 'application/json' }, headers || {});
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: hdrs,
    body: JSON.stringify(body || {}),
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) {}
  return {
    status: res.status,
    ok: res.ok,
    text: String(text || '').slice(0, 2000),
    json,
    sent_header_names: Object.keys(hdrs),
  };
}
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def _raw_page():
    return page.raw_page


class RequestTap:
    """Record live auth + companion POSTs. Values come from the page, not HAR bodies."""

    def __init__(self):
        self.auth: dict[str, dict] = {}
        self.companions: list[dict] = []
        self.live_headers: dict[str, str] = {}
        self._seen_companion: set[str] = set()
        self.post_paths: list[str] = []

    def on_request(self, request) -> None:
        try:
            method = str(getattr(request, "method", "") or "")
            url = str(getattr(request, "url", "") or "")
            if method.upper() != "POST":
                return
            headers = {}
            raw_headers = getattr(request, "headers", None)
            if isinstance(raw_headers, dict):
                headers = {str(k): str(v) for k, v in raw_headers.items() if v}
            for name in ("sentry-trace", "baggage", "accept", "content-type", "origin", "referer", "user-agent"):
                for hk, hv in headers.items():
                    if hk.lower() == name and hv:
                        self.live_headers[name if name != "user-agent" else "User-Agent"] = hv
                        if name == "user-agent":
                            self.live_headers["User-Agent"] = hv
                        else:
                            # keep captured HAR casing later via overlay
                            self.live_headers[hk] = hv
            post = ""
            try:
                post = str(request.post_data or "")
            except Exception:
                post = ""
            path = urlparse(url).path
            if path and path not in self.post_paths:
                self.post_paths.append(path)
            klass = companion_class_for_url(url)
            if klass and klass not in self._seen_companion:
                self._seen_companion.add(klass)
                json_keys = []
                form_keys = []
                if post.startswith("{") or post.startswith("["):
                    try:
                        body = json.loads(post)
                        if isinstance(body, dict):
                            json_keys = sorted(body.keys())
                    except Exception:
                        json_keys = []
                if "data=" in post:
                    form_keys = ["data"]
                self.companions.append(
                    {
                        "class": klass,
                        "method": "POST",
                        "path": path,
                        "json_keys": json_keys,
                        "form_keys": form_keys,
                    }
                )
            if path in (AUTH_SEND_CODE, AUTH_VERIFY_EMAIL, AUTH_CREATE_ACCOUNT):
                json_keys = []
                json_body = None
                try:
                    json_body = json.loads(post) if post else {}
                    if isinstance(json_body, dict):
                        json_keys = sorted(json_body.keys())
                    else:
                        json_body = None
                except Exception:
                    json_keys = []
                    json_body = None
                self.auth[path] = {
                    "method": "POST",
                    "path": path,
                    "header_names": sorted(headers),
                    "json_keys": json_keys,
                    "json": json_body,
                    "status": None,
                }
        except Exception:
            return

    def on_response(self, response) -> None:
        try:
            request = getattr(response, "request", None)
            url = str(getattr(request, "url", None) or getattr(response, "url", "") or "")
            path = urlparse(url).path
            if path not in (AUTH_SEND_CODE, AUTH_VERIFY_EMAIL, AUTH_CREATE_ACCOUNT):
                return
            status = None
            try:
                status = int(getattr(response, "status", 0) or 0)
            except Exception:
                status = 0
            row = self.auth.get(path) or {"path": path, "json": None, "json_keys": [], "header_names": []}
            row["status"] = status
            self.auth[path] = row
        except Exception:
            return

    def companion_classes(self) -> set[str]:
        return set(self._seen_companion)


_TAP: RequestTap | None = None


def _attach_tap() -> RequestTap:
    global _TAP
    tap = RequestTap()
    raw = _raw_page()
    raw.on("request", tap.on_request)
    raw.on("response", tap.on_response)
    try:
        raw.context.on("request", tap.on_request)
        raw.context.on("response", tap.on_response)
    except Exception:
        pass
    try:
        raw.context.add_init_script(AUTH_HOOK_INIT)
    except Exception:
        pass
    _TAP = tap
    return tap


def context_cookies() -> dict[str, str]:
    cookies = {}
    for cookie in _raw_page().context.cookies():
        name = str(cookie.get("name") or "")
        if name:
            cookies[name] = str(cookie.get("value") or "")
    return cookies


def post_signup_in_page(built: dict, extra_headers: dict | None = None):
    url = str(built.get("path") or built.get("url") or "")
    headers = dict(built.get("headers") or {})
    if extra_headers:
        headers.update(extra_headers)
    result = _raw_page().evaluate(
        PAGE_POST_JS,
        {"url": url, "body": built.get("json") or {}, "headers": headers},
    )
    return parse_inpage_post_result(result)


def read_sso_from_context() -> str:
    try:
        for cookie in _raw_page().context.cookies():
            name = str(cookie.get("name") or "").lower()
            if name in {"sso", "sso-rw"}:
                value = str(cookie.get("value") or "")
                if len(value) >= 20:
                    return value
    except Exception:
        pass
    return ""


def _eval_castle() -> dict:
    try:
        castle = _raw_page().evaluate(CASTLE_JS)
    except Exception:
        return {"token": "", "how": "error"}
    if not isinstance(castle, dict):
        return {"token": str(castle or ""), "how": "raw"}
    return castle


def _eval_sentry() -> dict:
    try:
        data = _raw_page().evaluate(SENTRY_JS)
    except Exception:
        return {"sentry_trace": "", "baggage": "", "how": "error"}
    return data if isinstance(data, dict) else {"sentry_trace": "", "baggage": "", "how": "raw"}


def wait_live_castle(timeout: float = 45.0) -> dict:
    deadline = time.time() + timeout
    last = {"token": "", "how": "missing"}
    while time.time() < deadline:
        last = _eval_castle()
        token = str(last.get("token") or "")
        if token.strip():
            return last
        time.sleep(1.0)
    return last


def wait_live_cookies(required: list[str], timeout: float = 45.0) -> dict[str, str]:
    deadline = time.time() + timeout
    cookies = {}
    while time.time() < deadline:
        cookies = context_cookies()
        missing = missing_live_names(required, cookies)
        if not missing:
            return cookies
        time.sleep(1.0)
    return cookies


def wait_companions(required_classes: list[str], tap: RequestTap, timeout: float = 25.0) -> set[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        have = tap.companion_classes()
        if required_classes and all(c in have for c in required_classes):
            return have
        time.sleep(0.5)
    return tap.companion_classes()


def live_sentry_headers(tap: RequestTap) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in ("sentry-trace", "baggage"):
        for name, value in (tap.live_headers or {}).items():
            if name.lower() == key and value:
                out[key] = value
    sdk = _eval_sentry()
    if sdk.get("sentry_trace") and "sentry-trace" not in out:
        out["sentry-trace"] = str(sdk["sentry_trace"])
    if sdk.get("baggage") and "baggage" not in out:
        out["baggage"] = str(sdk["baggage"])
    return out


def har_accept_for_path(path: str) -> str:
    calls = extract_signup_calls(HAR_PATH) if HAR_PATH.is_file() else {}
    headers = (calls.get(path) or {}).get("headers") or {}
    for name, value in headers.items():
        if str(name).lower() == "accept" and value:
            return str(value)
    return ""


def install_auth_hook() -> None:
    _raw_page().evaluate(AUTH_HOOK_JS)


def hooked_auth_posts() -> list:
    try:
        rows = _raw_page().evaluate(READ_AUTH_HOOK_JS)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def parse_hooked_send_code() -> dict:
    """Read live send-verification-code JSON from the page's own request (HAR source)."""
    for row in reversed(hooked_auth_posts()):
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if AUTH_SEND_CODE not in url:
            continue
        try:
            body = json.loads(str(row.get("body") or ""))
        except Exception:
            continue
        if not isinstance(body, dict):
            continue
        return {
            "json": body,
            "status": int(row.get("status") or 0),
            "url": url,
        }
    return {}


def _playwright_fill_email(email: str) -> bool:
    raw = _raw_page()
    try:
        loc = raw.locator(
            'input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]'
        ).first
        loc.wait_for(state="visible", timeout=8000)
        loc.click(timeout=3000)
        loc.fill(email, timeout=8000)
        current = str(loc.input_value(timeout=3000) or "")
        return current == email
    except Exception:
        return False


def _playwright_press_enter() -> None:
    raw = _raw_page()
    try:
        loc = raw.locator(
            'input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]'
        ).first
        loc.press("Enter", timeout=3000)
        return
    except Exception:
        pass
    try:
        raw.keyboard.press("Enter")
    except Exception:
        pass


def _page_form_state() -> dict:
    try:
        state = _raw_page().evaluate(PAGE_FORM_JS)
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def captured_send_code(tap: RequestTap | None = None) -> dict:
    last = parse_hooked_send_code()
    token = str((last.get("json") or {}).get("castleRequestToken") or "").strip()
    if token:
        return last
    if tap is not None:
        captured = (tap.auth.get(AUTH_SEND_CODE) or {}) or {}
        body = captured.get("json") or {}
        if isinstance(body, dict) and str(body.get("castleRequestToken") or "").strip():
            return {
                "json": body,
                "status": int(captured.get("status") or 0),
                "url": captured.get("path") or AUTH_SEND_CODE,
            }
        if isinstance(body, dict) and body.get("email"):
            return {
                "json": body,
                "status": int(captured.get("status") or 0),
                "url": captured.get("path") or AUTH_SEND_CODE,
            }
    return last


def mint_castle_from_page_sdk(email: str, log_callback=log) -> dict:
    """Call the live page Castle React SDK (same source as the HAR send-code XHR)."""
    try:
        result = _raw_page().evaluate(CASTLE_MINT_JS, email)
    except Exception as exc:
        log_callback(f"[!] Castle SDK mint evaluate failed: {exc}")
        return {"token": "", "how": "evaluate_error"}
    if not isinstance(result, dict):
        return {"token": str(result or ""), "how": "raw"}
    token = str(result.get("token") or "").strip()
    how = str(result.get("how") or "")
    pk = str(result.get("pk") or "")
    log_callback(f"[*] Castle SDK mint how={how} pk={(pk[:12] + '…') if pk else 'missing'} len={len(token)}")
    return {"token": token, "how": how, "pk": pk}


def _fill_email_on_page(email: str, log_callback=log) -> None:
    install_auth_hook()
    filled = _playwright_fill_email(email)
    if not filled:
        filled = _native_fill_email(email)
    react = {}
    try:
        react = _raw_page().evaluate(FILL_REACT_JS, email)
    except Exception:
        react = {}
    if not filled and not (isinstance(react, dict) and react.get("ok")):
        raise RuntimeError("email input not filled; cannot trigger live send-code")
    log_callback(f"[*] filled email on page playwright={filled} react={react!r}")
    time.sleep(0.8)


def _playwright_click_signup() -> str:
    raw = _raw_page()
    loc = raw.locator('button[type="submit"]').first
    loc.wait_for(state="visible", timeout=5000)
    text = ""
    try:
        text = str(loc.inner_text(timeout=2000) or "").strip()
    except Exception:
        text = "submit"
    loc.click(timeout=5000)
    return text or "submit"


def submit_email_on_page(email: str, log_callback=log, action: str = "playwright_submit") -> None:
    """One submit action per call so send-code is not fired twice (second OTP invalidates the first)."""
    if action == "playwright_submit":
        clicked = _playwright_click_signup()
        log_callback(f"[*] email submit action=playwright_submit click={clicked!r}")
        return
    if action == "enter":
        _playwright_press_enter()
        log_callback("[*] email submit action=enter")
        return
    if action == "js":
        clicked = _raw_page().evaluate(SUBMIT_REACT_JS)
        log_callback(f"[*] email submit action=js click={clicked!r}")
        return
    clicked = _native_click_action(
        (
            "注册",
            "继续",
            "下一步",
            "确认",
            "sign up",
            "signup",
            "continue",
            "next",
            "create account",
            "regístrate",
            "registrate",
            "registr",
            "continuar",
            "inscrire",
            "registrieren",
            "registrati",
            "inscrever",
        ),
        deny_keywords=(
            "with email",
            "使用邮箱",
            "correo",
            "regresar",
            "privacidad",
            "go back",
            "privacy",
        ),
    )
    log_callback(f"[*] email submit action=native click={clicked!r}")


def wait_hooked_send_code(timeout: float = 20.0, tap: RequestTap | None = None) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = captured_send_code(tap)
        token = str((last.get("json") or {}).get("castleRequestToken") or "").strip()
        if token:
            return last
        time.sleep(0.4)
    hooked = hooked_auth_posts()
    log(f"[!] send-code wait exhausted hook_n={len(hooked)} tap_auth={list((_TAP.auth if _TAP else {}))}")
    for row in hooked[-5:]:
        if isinstance(row, dict):
            log(f"[!] hooked {str(row.get('url') or '')[-60:]} body={str(row.get('body') or '')[:120]}")
    state = _page_form_state()
    if state:
        log(f"[!] page form after wait: {state!r}"[:300])
    return last


def trigger_live_send_code(email: str, tap: RequestTap | None, log_callback=log, timeout: float = 28.0) -> dict:
    """Retry fill/submit until the live send-code XHR carries castleRequestToken."""
    _fill_email_on_page(email, log_callback)
    try:
        info = _raw_page().evaluate(
            """
() => Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]')).map((n) => ({
  tag: n.tagName,
  type: n.getAttribute('type') || '',
  testid: n.getAttribute('data-testid') || '',
  text: ((n.innerText || n.textContent || n.value || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 80),
  disabled: !!(n.disabled || n.getAttribute('aria-disabled') === 'true'),
}))
"""
        )
        if isinstance(info, list) and info:
            log_callback(f"[*] signup buttons={info[:8]!r}"[:400])
    except Exception:
        pass
    last: dict = {}
    for action in ("playwright_submit", "enter", "js", "native"):
        if str((captured_send_code(tap).get("json") or {}).get("castleRequestToken") or "").strip():
            break
        submit_email_on_page(email, log_callback, action=action)
        last = wait_hooked_send_code(6.0, tap=tap)
        token = str((last.get("json") or {}).get("castleRequestToken") or "").strip()
        if token:
            log_callback(f"[*] live send-code castle captured action={action} len={len(token)}")
            return last
        body = last.get("json") or {}
        if isinstance(body, dict) and body.get("email") and not token:
            log_callback("[!] live send-code was tokenless; page Castle mint failed")
            return last
        state = _page_form_state()
        log_callback(f"[Debug] send-code not captured action={action} form={state!r}"[:300])
        if state.get("hasCode"):
            break
    token = str((last.get("json") or {}).get("castleRequestToken") or "").strip()
    if not token:
        mint = mint_castle_from_page_sdk(email, log_callback)
        token = str(mint.get("token") or "").strip()
        if token:
            last = {
                "json": {"email": email, "castleRequestToken": token},
                "status": 0,
                "url": AUTH_SEND_CODE,
                "castle_how": mint.get("how") or "useCastle.createRequestToken",
            }
            log_callback(f"[*] using live Castle SDK token for send-code len={len(token)}")
    return last


def bootstrap_live_surface(log_callback=log) -> dict:
    if not HAR_PATH.is_file():
        raise RuntimeError(f"HAR capture missing: {HAR_PATH}")
    required_cookies = auth_cookie_names_from_har(HAR_PATH)
    required_headers = auth_header_names_from_har(HAR_PATH)
    required_companions = required_companion_classes_from_har(HAR_PATH)
    log_callback(
        "[*] protocol bootstrap: wait live Castle/cookies/companions from page "
        f"(har cookies={required_cookies} companions={required_companions})"
    )
    tap = _attach_tap()
    open_signup_page(log_callback=log_callback)
    try:
        raw = _raw_page()
        raw.on("request", tap.on_request)
        raw.on("response", tap.on_response)
    except Exception:
        pass
    install_auth_hook()
    castle = wait_live_castle(2)
    castle_token = str(castle.get("token") or "").strip()
    cookies = wait_live_cookies(required_cookies, 45)
    missing_c = missing_live_names(required_cookies, cookies)
    if missing_c:
        log_callback(f"[!] live cookies still missing {missing_c}; wait extra 20s")
        cookies = wait_live_cookies(required_cookies, 20)
        missing_c = missing_live_names(required_cookies, cookies)
    hard_missing = hard_missing_live_cookie_names(required_cookies, cookies)
    if hard_missing:
        raise RuntimeError(f"live cookies missing HAR names {hard_missing}; not guessing values")
    if missing_c:
        log_callback(
            f"[!] continuing without optional live cookies {missing_c} "
            "(in-page fetch uses the browser jar as-is)"
        )
    have_comp = wait_companions(required_companions, tap, 60)
    missing_comp = [c for c in required_companions if c not in have_comp]
    if missing_comp:
        raise RuntimeError(
            f"live companion POSTs missing HAR classes {missing_comp} seen={sorted(have_comp)} "
            f"posts={tap.post_paths[:20]!r}; not inventing bodies"
        )
    ua = ""
    try:
        ua = str(_raw_page().evaluate("() => String(navigator.userAgent || '')") or "")
    except Exception:
        ua = ""
    sentry = live_sentry_headers(tap)
    missing_h = []
    for name in required_headers:
        low = name.lower()
        if low in {"sentry-trace", "baggage"} and low not in {k.lower() for k in sentry}:
            missing_h.append(name)
    if missing_h:
        raise RuntimeError(f"live headers missing HAR names {missing_h}; not copying stale HAR traces")
    sitekey = ""
    try:
        sitekey = str(_raw_page().evaluate(SITEKEY_JS) or "")
    except Exception:
        sitekey = ""
    live_headers = dict(tap.live_headers)
    if ua:
        live_headers["User-Agent"] = ua
    live_headers.update(sentry)
    log_callback(
        f"[*] bootstrap cookies={sorted(cookies)} castle_sdk={castle.get('how')} "
        f"castle_sdk_len={len(castle_token)} companions={sorted(have_comp)} "
        f"sitekey={(sitekey or DEFAULT_XAI_SITEKEY)[:16]}…"
    )
    return {
        "cookies": cookies,
        "user_agent": ua,
        "castle": castle_token,
        "castle_how": str(castle.get("how") or ""),
        "sitekey": sitekey or DEFAULT_XAI_SITEKEY,
        "live_headers": live_headers,
        "sentry": sentry,
        "har_cookie_names": required_cookies,
        "har_header_names": required_headers,
        "companion_classes": required_companions,
        "companions_seen": list(tap.companions),
        "tap": tap,
    }


def headers_for_auth_path(path: str, boot: dict) -> dict[str, str]:
    live = dict(boot.get("live_headers") or {})
    live.update(boot.get("sentry") or {})
    accept = har_accept_for_path(path)
    if accept:
        live["Accept"] = accept
    live.setdefault("content-type", "application/json")
    live.setdefault("Content-Type", "application/json")
    har_names = boot.get("har_header_names") or auth_header_names_from_har(HAR_PATH)
    return fetch_settable_headers(har_names, live)


def _redact_surface(payload: dict) -> dict:
    return payload


def write_live_surface(boot: dict, sent: dict) -> None:
    har_calls = extract_signup_calls(HAR_PATH) if HAR_PATH.is_file() else {}
    har_auth = {}
    for path in (AUTH_SEND_CODE, AUTH_VERIFY_EMAIL, AUTH_CREATE_ACCOUNT):
        call = har_calls.get(path) or {}
        har_auth[path] = {
            "json_keys": sorted((call.get("json") or {}).keys()),
            "cookie_names": sorted(call.get("cookies") or {}),
            "header_names": sorted(
                n
                for n in (call.get("headers") or {})
                if str(n).lower() not in {"connection", "content-length", "host", "proxy-authorization", "cookie"}
            ),
        }
    tap = boot.get("tap")
    live_auth = {}
    if tap is not None:
        for path, info in (tap.auth or {}).items():
            live_auth[path] = {
                "json_keys": info.get("json_keys") or [],
                "header_names": info.get("header_names") or [],
                "cookie_names": sorted(boot.get("cookies") or {}),
            }
    for path, info in sent.items():
        live_auth.setdefault(path, {}).update(info)
    companions_har = companion_classes_from_har(HAR_PATH) if HAR_PATH.is_file() else []
    dump = {
        "har_auth": har_auth,
        "live_sent": live_auth,
        "companion_har_classes": companions_har,
        "companion_live": boot.get("companions_seen") or [],
        "castle_how": boot.get("castle_how"),
        "castle_len": len(str(boot.get("castle") or "")),
        "live_json_subset": True,
        "har_json_key_union": sorted(har_json_key_union(HAR_PATH) if HAR_PATH.is_file() else []),
        "live_cookie_subset": live_names_subset_of_har(
            (boot.get("cookies") or {}).keys(),
            boot.get("har_cookie_names") or [],
        ),
    }
    for path, info in live_auth.items():
        extra = extra_live_json_keys(info.get("json_keys") or [], HAR_PATH) if HAR_PATH.is_file() else []
        if extra:
            dump["live_json_subset"] = False
            dump.setdefault("extra_live_json", {})[path] = extra
    SURFACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SURFACE_PATH.write_text(json.dumps(dump, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"[*] wrote live request surface {SURFACE_PATH}")


def _classify_guard(payload: dict) -> str:
    if not payload:
        return "skipped"
    if payload.get("error") or payload.get("timed_out"):
        return "error"
    if "risk" in payload:
        return "risk" if payload.get("risk") else "pass"
    return "skipped"


def _cpa_and_guard(sso: str, email: str, attempts: int = 3) -> tuple[bool, str, str]:
    guard_box: dict = {}
    orig_apply = app._apply_guard_register_result

    def _capture_guard(email_arg, payload, log_callback=None):
        guard_box["payload"] = payload if isinstance(payload, dict) else {}
        return orig_apply(email_arg, payload, log_callback=log_callback)

    app._apply_guard_register_result = _capture_guard
    last_error = ""
    try:
        for attempt in range(1, max(1, attempts) + 1):
            guard_box.pop("payload", None)
            try:
                cpa_ok = add_sso_to_cpa(sso, email=email, log_callback=log)
            except RegistrationRiskDenied as exc:
                guard_name = _classify_guard(guard_box.get("payload") or {})
                if guard_name == "skipped":
                    guard_name = "risk"
                return True, guard_name, str(exc)[:240]
            except Exception as exc:
                last_error = str(exc)[:240]
                log(f"[!] CPA exception attempt {attempt}/{attempts}: {exc}")
                cpa_ok = False
            else:
                last_error = "" if cpa_ok else "cpa_token_failed"
            if cpa_ok:
                return True, _classify_guard(guard_box.get("payload") or {}), ""
            if attempt < attempts:
                log(f"[*] CPA failed ({last_error or 'token'}), retry in 3s")
                time.sleep(3)
        return False, _classify_guard(guard_box.get("payload") or {}), last_error or "cpa_token_failed"
    finally:
        app._apply_guard_register_result = orig_apply


def _run_protocol_signup_once(log_fn, cancel, usage: ProtocolResourceUsage) -> dict:
    """One protocol attempt. Marks usage.email_used / remote_captcha_used when spent."""
    cancel()
    boot = bootstrap_live_surface(log_fn)
    captured = extract_signup_calls(HAR_PATH)
    sso = ""
    data = None
    email = ""
    given = family = password = ""
    sent_surface: dict = {}
    for mail_try in range(1, 3):
        cancel()
        email, mail_token = get_email_and_token()
        log_fn(f"[*] mailbox {email} (try {mail_try}/2)")
        castle_token = str(boot.get("castle") or "").strip()
        page_send = {}
        if not castle_token:
            log_fn("[*] Castle SDK empty; trigger live page send-code (HAR source of castleRequestToken)")
            page_send = trigger_live_send_code(email, boot.get("tap"), log_fn)
            if page_send.get("json") or int(page_send.get("status") or 0) > 0:
                usage.email_used = True
            castle_token = str((page_send.get("json") or {}).get("castleRequestToken") or "").strip()
            boot["castle"] = castle_token
            boot["castle_how"] = str(page_send.get("castle_how") or "page_send_code_request")
        if not castle_token:
            raise RuntimeError("send-verification-code missing live castleRequestToken")
        send = build_send_verification_code(
            email=email,
            castle_request_token=castle_token,
        )
        assert_send_code_has_castle(send)
        assert_json_keys_match_capture(send["json"], captured[AUTH_SEND_CODE]["json"])
        send["headers"] = headers_for_auth_path(AUTH_SEND_CODE, boot)
        page_status = int(page_send.get("status") or 0)
        sdk_minted = "useCastle" in str(page_send.get("castle_how") or boot.get("castle_how") or "")
        if page_send.get("json") and castle_token and not sdk_minted:
            status = page_status if page_status else 200
            data = {"ok": True}
            log_fn(
                "[*] live page POST send-verification-code "
                f"json_keys={sorted(send['json'])} castle_len={len(castle_token)} "
                f"status={status} how={boot.get('castle_how')} (not a second fetch)"
            )
        else:
            log_fn(
                "[*] in-page fetch POST send-verification-code "
                f"json_keys={sorted(send['json'])} castle_len={len(castle_token)} "
                f"hdrs={sorted(send['headers'])}"
            )
            usage.email_used = True
            status, data, _resp = post_signup_in_page(send)
        sent_surface[AUTH_SEND_CODE] = {
            "json_keys": sorted(send["json"]),
            "header_names": sorted(send["headers"]),
            "cookie_names": sorted(boot.get("cookies") or {}),
        }
        log_fn(
            f"[*] send-code status={status} "
            f"keys={sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )
        if status >= 400 or status == 0:
            usage.email_used = True
            write_live_surface(boot, sent_surface)
            raise RuntimeError(f"send-code {status} {str(data)[:160]}")
        usage.email_used = True
        log_fn("[*] waiting OTP via mailbox provider (no Camoufox OTP fill)")
        cancel()
        code = get_oai_code(mail_token, email, log_callback=log_fn)
        verify = build_verify_email(email=email, code=code)
        assert_json_keys_match_capture(verify["json"], captured[AUTH_VERIFY_EMAIL]["json"])
        verify["headers"] = headers_for_auth_path(AUTH_VERIFY_EMAIL, boot)
        log_fn(
            f"[*] in-page fetch POST verify-email json_keys={sorted(verify['json'])} "
            f"code_len={len(str(verify['json'].get('code') or ''))}"
        )
        status, data, _resp = post_signup_in_page(verify)
        sent_surface[AUTH_VERIFY_EMAIL] = {
            "json_keys": sorted(verify["json"]),
            "header_names": sorted(verify["headers"]),
            "cookie_names": sorted(context_cookies()),
        }
        log_fn(
            f"[*] verify-email status={status} "
            f"keys={sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )
        if status >= 400 or status == 0:
            write_live_surface(boot, sent_surface)
            raise RuntimeError(f"verify-email {status} {str(data)[:160]}")
        log_fn("[*] cloud Turnstile only (no widget click)")
        cancel()
        usage.remote_captcha_used = True
        token = solve_turnstile(
            AUTH_ORIGIN + "/sign-up?redirect=grok-com",
            boot.get("sitekey") or DEFAULT_XAI_SITEKEY,
            user_agent=boot.get("user_agent") or "",
            proxy_url=get_bound_proxy() or "",
            log=log_fn,
        )
        given, family, password = build_profile()
        create_castle = mint_castle_from_page_sdk(email, log_fn)
        create_castle_token = str(create_castle.get("token") or "").strip()
        if not create_castle_token:
            raise RuntimeError(
                "create-account missing live castleRequestToken from page SDK; not guessing"
            )
        create = build_create_account(
            email=email,
            password=password,
            given_name=given,
            family_name=family,
            email_validation_code=code,
            turnstile_token=token,
            castle_request_token=create_castle_token,
        )
        assert_json_keys_match_capture(
            create["json"],
            captured[AUTH_CREATE_ACCOUNT]["json"],
            extra_allowed={"castleRequestToken"},
        )
        create["headers"] = headers_for_auth_path(AUTH_CREATE_ACCOUNT, boot)
        log_fn(
            f"[*] in-page fetch POST create-account json_keys={sorted(create['json'])} "
            f"turnstile_len={len(str(create['json'].get('turnstileToken') or ''))} "
            f"castle_len={len(create_castle_token)} how={create_castle.get('how')}"
        )
        status, data, _resp = post_signup_in_page(create)
        sent_surface[AUTH_CREATE_ACCOUNT] = {
            "json_keys": sorted(create["json"]),
            "header_names": sorted(create["headers"]),
            "cookie_names": sorted(context_cookies()),
        }
        log_fn(
            f"[*] create-account status={status} "
            f"keys={sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )
        if status >= 400 or status == 0:
            write_live_surface(boot, sent_surface)
            raise RuntimeError(f"create-account {status} {str(data)[:160]}")
        sso = sso_from_create_payload(data)
        if not sso:
            sso = read_sso_from_context()
        if sso:
            break
        if isinstance(data, dict) and data.get("existingEmail") and mail_try < 2:
            log_fn(f"[!] existingEmail={email}, retry another mailbox")
            continue
        write_live_surface(boot, sent_surface)
        raise RuntimeError(f"no sso body={str(data)[:200]}")
    write_live_surface(boot, sent_surface)
    if not sso:
        raise RuntimeError(f"no sso body={str(data)[:200]}")
    log_fn(f"[+] protocol sso ok len={len(sso)} email={email}")
    return {
        "email": email,
        "profile": {
            "given_name": given,
            "family_name": family,
            "password": password,
        },
        "sso": sso,
        "scheme": "protocol",
    }


def run_protocol_signup(log_callback=None, cancel_callback=None) -> dict:
    """HAR protocol signup using the already-started Camoufox session.

    Returns {email, profile, sso, scheme}. Does not start/stop the browser or
    run CPA/Guard — panel workers share those steps with the browser scheme.

    Steps before waiting for the mailbox are retried: companion POST gaps,
    missing cookies, blank page, etc. reopen the browser or switch the exit
    instead of counting a failure. Failures only count after send-code or a
    remote captcha spend.
    """
    log_fn = log_callback or log
    apply_protocol_runtime_env()

    def _cancel() -> None:
        if callable(cancel_callback) and cancel_callback():
            raise RegistrationCancelled("stopped")

    def _once(usage: ProtocolResourceUsage) -> dict:
        return _run_protocol_signup_once(log_fn, _cancel, usage)

    return retry_protocol_pre_inbox(
        _once,
        recycle=lambda: recycle_protocol_browser(log_fn),
        attempts=protocol_pre_inbox_retries(),
        log_fn=log_fn,
        cancel=_cancel,
    )


def register_one(index: int, rotate_idx: int) -> dict:
    result = {
        "index": index,
        "email": "",
        "ok": False,
        "sso": False,
        "bot_flag": None,
        "risk_blocked": False,
        "cpa": False,
        "guard": None,
        "error": "",
    }
    px = pick_proxy_for_worker(0, rotate_idx)
    set_thread_proxy(px)
    log(f"[*] [{index}] protocol proxy bound ({'set' if px else 'empty'})")
    start_browser(log_callback=log)
    try:
        outcome = run_protocol_signup(log_callback=log)
        email = str(outcome.get("email") or "")
        sso = str(outcome.get("sso") or "")
        result["email"] = email
        if not sso:
            result["error"] = "no sso"
            return result
        result["sso"] = True
        log(f"[+] [{index}] protocol sso ok len={len(sso)} email={email}")
        state = ensure_sso_oauth_eligible(sso, email=email, log_callback=log)
        result["bot_flag"] = state.get("bot_flag_source") if isinstance(state, dict) else None
        blocked, details = _registration_risk_should_block(state if isinstance(state, dict) else {})
        result["risk_blocked"] = bool(blocked)
        if blocked:
            result["error"] = f"sso_risk {details}"
            log(f"[!] [{index}] SSO 风控命中: {details}")
        release_browser_before_cpa(log)
        cpa_ok, guard_name, cpa_error = _cpa_and_guard(sso, email)
        result["cpa"] = bool(cpa_ok)
        result["guard"] = guard_name
        if cpa_error:
            result["error"] = cpa_error
        result["ok"] = bool(cpa_ok) and result["guard"] == "pass"
        if result["ok"] and blocked:
            log(f"[*] [{index}] Guard 通过，但 botFlag 仍标记隔离: {details}")
        return result
    except Exception as exc:
        result["error"] = str(exc)[:240]
        log(f"[!] [{index}] exception: {exc}")
        return result
    finally:
        try:
            stop_browser(force=True)
        except Exception:
            pass


def main() -> int:
    count = 1
    if len(sys.argv) > 1:
        try:
            count = max(1, int(sys.argv[1]))
        except ValueError:
            count = 1
    apply_protocol_runtime_env()
    load_config()
    app.config["register_workers"] = 1
    _wire_runtime_modules()
    results = []
    for i in range(1, count + 1):
        log(f"===== protocol account {i}/{count} =====")
        try:
            results.append(register_one(i, rotate_idx=i - 1))
        except Exception as exc:
            log(f"[!] [{i}] exception: {exc}")
            results.append(
                {
                    "index": i,
                    "email": "",
                    "ok": False,
                    "sso": False,
                    "bot_flag": None,
                    "risk_blocked": False,
                    "cpa": False,
                    "guard": None,
                    "error": str(exc)[:240],
                }
            )
    log("===== protocol stability summary =====")
    ok_n = 0
    for row in results:
        if row.get("ok"):
            ok_n += 1
        log(
            f"#{row.get('index')} email={row.get('email') or '-'} "
            f"sso={row.get('sso')} botFlag={row.get('bot_flag')} "
            f"risk_block={row.get('risk_blocked')} cpa={row.get('cpa')} "
            f"guard={row.get('guard')} ok={row.get('ok')} err={row.get('error') or '-'}"
        )
    log(f"[*] success {ok_n}/{len(results)}")
    return 0 if ok_n == len(results) and results else 1


if __name__ == "__main__":
    sys.exit(main())
