# -*- coding: utf-8 -*-
"""Patchright Turnstile: origin-preload sitekey, click, then JS getToken.

Single solve: open the target origin, inject an explicit widget with that
sitekey, click the checkbox, then read turnstile.getResponse().

Pool: keep minting tokens in Chromium. Default is headed on a private Xvfb
so nothing pops on the user DISPLAY. Chrome --headless is opt-in and often
blocked by Turnstile. Tokens expire in 5 minutes; the pool drops them 30s
early so a consumer still has time to POST.
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from nobrowser_protocol.patchright_engine import (
    PATCHRIGHT_WEBGL_ARGS,
    _apply_patchright_fingerprint,
    _chrome_path,
    _proxy_for_patchright,
    run_patchright_sync,
)


def _resolve_chrome() -> str:
    explicit = _chrome_path()
    if explicit:
        return explicit
    cache = Path.home() / ".cache" / "ms-playwright"
    matches = sorted(cache.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
    for path in matches:
        if path.is_file():
            return str(path)
    return ""

DEFAULT_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_ORIGIN = "https://accounts.x.ai/sign-up?redirect=grok-com"
TOKEN_TTL_SECONDS = 5 * 60
DEFAULT_POOL_SIZE = 5
POOL_FILE_ENV = "GROK_TURNSTILE_POOL_FILE"
TOKEN_SKEW_SECONDS = 30
TURNSTILE_JS = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=__tsOnload"
TURNSTILE_HOST = "challenges.cloudflare.com"

GET_TOKEN_JS = """
() => {
  const out = { token: '', how: '', widget: false, error: '', widgetId: null };
  const pick = (value, how) => {
    const token = String(value || '').trim();
    if (token) { out.token = token; out.how = how; }
  };
  try {
    pick(window.__tsToken, 'callback');
    const input = document.querySelector(
      'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    );
    if (!out.token && input) pick(input.value, 'input');
    out.widget = !!(window.turnstile);
    out.widgetId = window.__tsWidgetId == null ? null : window.__tsWidgetId;
    out.error = String(window.__tsError || '');
    if (!out.token && window.turnstile && typeof turnstile.getResponse === 'function') {
      try { pick(turnstile.getResponse(window.__tsWidgetId), 'getResponse'); }
      catch (e) { try { pick(turnstile.getResponse(), 'getResponse'); } catch (e2) {} }
    }
  } catch (e) {
    out.error = String(e && e.message ? e.message : e);
  }
  return out;
}
"""

CLICK_WIDGET_JS = """
() => {
  const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]');
  if (iframe) {
    iframe.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return 'iframe';
  }
  return 'miss';
}
"""


def widget_html(sitekey: str, origin: str) -> str:
    key = json.dumps(str(sitekey or DEFAULT_SITEKEY))
    page = json.dumps(str(origin or DEFAULT_ORIGIN))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>turnstile</title>
  <script>
    window.__tsToken = "";
    window.__tsError = "";
    window.__tsWidgetId = null;
    window.__tsPage = {page};
    window.__tsOnToken = function (token) {{
      window.__tsToken = String(token || "");
      var input = document.querySelector('input[name="cf-turnstile-response"]');
      if (!input) {{
        input = document.createElement("input");
        input.type = "hidden";
        input.name = "cf-turnstile-response";
        document.body.appendChild(input);
      }}
      input.value = window.__tsToken;
    }};
    window.__tsOnload = function () {{
      try {{
        window.__tsWidgetId = turnstile.render("#cf-widget", {{
          sitekey: {key},
          theme: "light",
          size: "normal",
          appearance: "always",
          retry: "auto",
          callback: window.__tsOnToken,
          "error-callback": function (err) {{ window.__tsError = String(err || "error"); }},
          "expired-callback": function () {{ window.__tsToken = ""; }}
        }});
      }} catch (e) {{
        window.__tsError = String(e && e.message ? e.message : e);
      }}
    }};
  </script>
  <script src="{TURNSTILE_JS}" async defer onerror="window.__tsError='script_load_failed'"></script>
</head>
<body>
  <form action="#" method="post">
    <div id="cf-widget" class="cf-turnstile" data-sitekey={key}></div>
    <input type="hidden" name="cf-turnstile-response" value="" />
  </form>
</body>
</html>
"""


def origin_host(origin: str) -> str:
    return (urlparse(origin).netloc or "accounts.x.ai").lower()


def origin_document_route_glob(origin: str) -> str:
    """Intercept only the target origin. Never bind challenges.cloudflare.com.

    Playwright globs like ``**://accounts.x.ai/**`` often fail to match
    ``https://accounts.x.ai/sign-up?...``, so the real document (and its
    nonce CSP) loads instead of the widget HTML.
    """
    return f"https://{origin_host(origin)}/**"


def origin_preload_fulfill_headers() -> dict[str, str]:
    """Headers for the injected origin document.

    Deliberately omit Content-Security-Policy. ``page.set_content`` /
    ``document.write`` keep x.ai's nonce CSP from the first goto, which
    blocks ``turnstile/v0/api.js``: ``window.turnstile`` never appears
    (widget=0) and the widget error-callback never fires (error='').
    """
    return {
        "content-type": "text/html; charset=utf-8",
        "cache-control": "no-store",
    }


def should_fulfill_origin_document(
    resource_type: str,
    url: str,
    origin: str = DEFAULT_ORIGIN,
) -> bool:
    """Replace x.ai signup documents with widget HTML; leave scripts alone."""
    if str(resource_type or "").lower() != "document":
        return False
    parsed = urlparse(str(url or ""))
    host = (parsed.netloc or "").lower()
    if TURNSTILE_HOST in host or TURNSTILE_HOST in str(url or "").lower():
        return False
    return host == origin_host(origin)


def mint_failure_message(got: dict[str, str], page_url: str) -> str:
    """Distinguish script-never-loaded (widget=0) from loaded-but-empty token."""
    widget = "1" if str(got.get("widget") or "") == "1" else "0"
    error = str(got.get("error") or "")
    if widget != "1":
        return (
            f"turnstile token empty widget=0 error={error!r} url={page_url}"[:400]
        )
    return (
        f"turnstile token empty widget=1 error={error!r} url={page_url}"[:400]
    )


def token_deadline(minted_at: float, ttl: float = TOKEN_TTL_SECONDS, skew: float = TOKEN_SKEW_SECONDS) -> float:
    return float(minted_at) + float(ttl) - float(skew)


def token_alive(minted_at: float, now: float | None = None, ttl: float = TOKEN_TTL_SECONDS, skew: float = TOKEN_SKEW_SECONDS) -> bool:
    stamp = time.time() if now is None else float(now)
    return token_deadline(minted_at, ttl=ttl, skew=skew) > stamp


@dataclass
class SolvedToken:
    token: str
    minted_at: float
    sitekey: str
    origin: str
    how: str = ""
    headless: bool = True

    def remaining(self, now: float | None = None, skew: float = TOKEN_SKEW_SECONDS) -> float:
        stamp = time.time() if now is None else float(now)
        return token_deadline(self.minted_at, skew=skew) - stamp

    def alive(self, now: float | None = None, skew: float = TOKEN_SKEW_SECONDS) -> bool:
        return self.remaining(now=now, skew=skew) > 0


def _log(emit: Callable[[str], None] | None, message: str) -> None:
    if emit:
        emit(message)


def _page_eval(page, expression: str, arg: Any = None) -> Any:
    """Run in the page world. Patchright's evaluate defaults to an isolated world."""
    if arg is None:
        return page.evaluate(expression, isolated_context=False)
    return page.evaluate(expression, arg, isolated_context=False)


def _free_display() -> int:
    for display in range(90, 250):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 6000 + display))
        except OSError:
            continue
        finally:
            probe.close()
        lock = Path(f"/tmp/.X{display}-lock")
        if lock.exists():
            continue
        return display
    raise RuntimeError("no free Xvfb display")


@contextlib.contextmanager
def _private_xvfb() -> Iterator[str]:
    """Headed Chrome on a private X server; does not use the user DISPLAY."""
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise RuntimeError("Xvfb missing; install xvfb or pass headed=True")
    display_n = _free_display()
    display = f":{display_n}"
    proc = subprocess.Popen(
        [xvfb, display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if Path(f"/tmp/.X{display_n}-lock").exists():
                break
            if proc.poll() is not None:
                raise RuntimeError(f"Xvfb exited {proc.returncode}")
            time.sleep(0.05)
        yield display
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _chrome_launch_kwargs(
    proxy: str = "",
    headed: bool = False,
    display: str = "",
    fp: dict | None = None,
) -> dict[str, Any]:
    profile = fp or {}
    screen = profile.get("screen") or (1920, 1080)
    width, height = int(screen[0]), int(screen[1])
    args = [
        "--disable-blink-features=AutomationControlled",
        f"--window-size={width},{height}",
        *PATCHRIGHT_WEBGL_ARGS,
    ]
    if not headed:
        args.append("--headless=new")
    launch: dict[str, Any] = {
        "headless": not headed,
        "no_viewport": True,
        "locale": str(profile.get("locale") or "en-US"),
        "args": args,
    }
    if profile.get("user_agent"):
        launch["user_agent"] = str(profile["user_agent"])
    if profile.get("timezone_id"):
        launch["timezone_id"] = str(profile["timezone_id"])
    if profile.get("geolocation"):
        launch["geolocation"] = profile["geolocation"]
        launch["permissions"] = ["geolocation"]
    proxy_cfg = _proxy_for_patchright(proxy)
    if proxy_cfg:
        launch["proxy"] = proxy_cfg
    chrome = _resolve_chrome()
    if chrome:
        launch["executable_path"] = chrome
    if display:
        env = dict(os.environ)
        env["DISPLAY"] = display
        launch["env"] = env
    return launch


def _click_challenge(page, emit: Callable[[str], None] | None) -> str:
    how = ""
    try:
        how = str(_page_eval(page, CLICK_WIDGET_JS) or "")
    except Exception as exc:
        _log(emit, f"click js: {exc}")
    for frame in page.frames:
        url = str(getattr(frame, "url", "") or "")
        if "challenges.cloudflare.com" not in url and "turnstile" not in url:
            continue
        for selector in (
            "input[type=checkbox]",
            ".cb-lb",
            "#success",
            "body",
        ):
            try:
                loc = frame.locator(selector).first
                loc.click(timeout=250, force=True)
                how = how or f"frame:{selector}"
                break
            except Exception:
                continue
        if how.startswith("frame:"):
            break
        try:
            box = frame.locator("body").bounding_box(timeout=200)
            if box:
                page.mouse.click(box["x"] + 28, box["y"] + 28)
                how = how or "frame-xy"
        except Exception:
            pass
    try:
        iframe = page.locator('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').first
        box = iframe.bounding_box(timeout=200)
        if box:
            page.mouse.click(box["x"] + 28, box["y"] + 32)
            how = how or "iframe-xy"
    except Exception:
        pass
    if how:
        _log(emit, f"click how={how}")
    return how or "miss"


def _read_token(page) -> dict[str, str]:
    try:
        data = _page_eval(page, GET_TOKEN_JS)
    except Exception as exc:
        return {"token": "", "how": "", "error": str(exc), "widget": "0"}
    if not isinstance(data, dict):
        return {"token": "", "how": "", "error": "bad_eval", "widget": "0"}
    return {
        "token": str(data.get("token") or "").strip(),
        "how": str(data.get("how") or ""),
        "error": str(data.get("error") or ""),
        "widget": "1" if data.get("widget") else "0",
    }


def _turnstile_api_ready(page) -> bool:
    try:
        return bool(
            _page_eval(page,
                "() => !!(window.turnstile && typeof turnstile.render === 'function')"
            )
        )
    except Exception:
        return False


def _eval_turnstile_api(page, emit: Callable[[str], None] | None) -> None:
    """Load api.js as a real script tag. Evaluating the source leaves currentScript null."""
    if _turnstile_api_ready(page):
        return
    try:
        page.add_script_tag(url=TURNSTILE_JS)
    except Exception as exc:
        _log(emit, f"api.js tag: {exc}")
        try:
            _page_eval(page,
                """(src) => {
                  if (document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) return;
                  const script = document.createElement("script");
                  script.src = src;
                  script.async = true;
                  document.head.appendChild(script);
                }""",
                TURNSTILE_JS,
            )
        except Exception as exc2:
            _log(emit, f"api.js dom: {exc2}")


def _ensure_widget_rendered(page, sitekey: str) -> None:
    _page_eval(page,
        """(sitekey) => {
          if (!window.turnstile || typeof turnstile.render !== 'function') return;
          if (window.__tsWidgetId != null) return;
          const host = document.querySelector('#cf-widget') || document.body;
          try {
            window.__tsWidgetId = turnstile.render(host, {
              sitekey: sitekey,
              theme: 'light',
              size: 'normal',
              appearance: 'always',
              retry: 'auto',
              callback: window.__tsOnToken,
              'error-callback': function (err) { window.__tsError = String(err || 'error'); },
              'expired-callback': function () { window.__tsToken = ''; }
            });
          } catch (e) {
            window.__tsError = String(e && e.message ? e.message : e);
          }
        }""",
        sitekey,
    )


def _wait_turnstile_global(page, timeout_ms: int) -> None:
    page.wait_for_function(
        "() => !!(window.turnstile && typeof turnstile.render === 'function')",
        timeout=timeout_ms,
    )


def _preload_widget(page, sitekey: str, origin: str, emit: Callable[[str], None] | None) -> None:
    parsed = urlparse(origin)
    warmup = origin if parsed.scheme and parsed.netloc else DEFAULT_ORIGIN
    html = widget_html(sitekey, origin)
    host = origin_host(origin)

    def matches(url: str) -> bool:
        return (urlparse(str(url or "")).netloc or "").lower() == host

    def on_origin_document(route) -> None:
        request = route.request
        try:
            rtype = str(getattr(request, "resource_type", "") or "")
        except Exception:
            rtype = ""
        url = str(getattr(request, "url", "") or "")
        if should_fulfill_origin_document(rtype, url, origin):
            route.fulfill(
                status=200,
                body=html,
                headers=origin_preload_fulfill_headers(),
            )
            return
        route.continue_()

    def on_response(response) -> None:
        url = str(getattr(response, "url", "") or "")
        if "turnstile/v0/api.js" not in url:
            return
        status = getattr(response, "status", "")
        ctype = ""
        looks_js = False
        try:
            headers = response.headers or {}
            ctype = str(headers.get("content-type") or "")[:40]
            body = response.text()[:80]
            looks_js = "function" in body or "var " in body or body.strip().startswith("/*")
        except Exception:
            body = ""
        _log(emit, f"ts_net status={status} js={looks_js} ctype={ctype}")

    page.on("response", on_response)
    page.route(matches, on_origin_document)
    try:
        page.goto(warmup, wait_until="domcontentloaded", timeout=45000)
        try:
            doc = _page_eval(page,
                """() => ({
                  title: document.title || "",
                  widget: !!document.getElementById("cf-widget"),
                  scripts: [...document.scripts].filter((s) => String(s.src || "").includes("turnstile")).length
                })"""
            )
        except Exception:
            doc = {}
        _log(
            emit,
            f"origin={page.url} doc={doc.get('title','')[:24]} widget={doc.get('widget')} ts_scripts={doc.get('scripts')}",
        )
        try:
            _wait_turnstile_global(page, 20000)
        except Exception:
            _eval_turnstile_api(page, emit)
            try:
                _wait_turnstile_global(page, 10000)
            except Exception as exc:
                got = _read_token(page)
                raise RuntimeError(
                    mint_failure_message(got, page.url) + f" wait={exc}"
                ) from exc
        _ensure_widget_rendered(page, sitekey)
    finally:
        try:
            page.unroute(matches, on_origin_document)
        except Exception:
            pass


def solve_turnstile_once(
    *,
    sitekey: str = DEFAULT_SITEKEY,
    origin: str = DEFAULT_ORIGIN,
    proxy: str = "",
    headed: bool | None = None,
    visible: bool = False,
    timeout: float = 90.0,
    log: Callable[[str], None] | None = None,
) -> SolvedToken:
    """Mint one token on the target origin with an explicit Turnstile widget.

    Default is headed Chromium on a private Xvfb. That is not Chrome
    --headless and does not reuse the user DISPLAY.
    """
    if headed is None:
        headed = str(os.environ.get("PATCHRIGHT_HEADED") or "").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "headless",
        }
        if str(os.environ.get("PATCHRIGHT_HEADLESS") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            headed = False
    if _asyncio_needed():
        return run_patchright_sync(
            lambda: solve_turnstile_once(
                sitekey=sitekey,
                origin=origin,
                proxy=proxy,
                headed=headed,
                visible=visible,
                timeout=timeout,
                log=log,
            )
        )

    def _mint(display: str = "") -> SolvedToken:
        from browser_session import patchright_fingerprint
        from patchright.sync_api import sync_playwright

        user_dir = tempfile.mkdtemp(prefix="patchright-ts-")
        minted_at = time.time()
        token = ""
        how = ""
        if str(os.environ.get("TURNSTILE_NO_FP") or "").strip() == "1":
            fp = {}
        else:
            fp = patchright_fingerprint(proxy, _resolve_chrome())
        screen = fp.get("screen") or (0, 0)
        _log(
            log,
            "fp "
            f"screen={int(screen[0])}x{int(screen[1])} "
            f"tz={fp.get('timezone_id') or 'server'} "
            f"hw={fp.get('hardware_concurrency')}/{fp.get('device_memory')}",
        )
        with _MINT_GATE:
          with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_dir,
                **_chrome_launch_kwargs(proxy, headed=bool(headed), display=display, fp=fp),
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if fp.get("user_agent"):
                    _apply_patchright_fingerprint(context, page, fp, log or (lambda _m: None))
                parsed = urlparse(origin)
                warmup = origin if parsed.scheme and parsed.netloc else DEFAULT_ORIGIN
                page.goto(warmup, wait_until="domcontentloaded", timeout=45000)
                _log(log, f"origin={page.url}")
                token = token_from_existing_page(
                    page, log, timeout=timeout, sitekey=sitekey
                )
                how = "script-tag"
                minted_at = time.time()
            finally:
                context.close()
                shutil.rmtree(user_dir, ignore_errors=True)
        _log(log, f"token how={how} len={len(token)}")
        return SolvedToken(
            token=token,
            minted_at=minted_at,
            sitekey=sitekey,
            origin=origin,
            how=how,
            headless=not headed,
        )

    if headed and not visible:
        existing = str(os.environ.get("DISPLAY") or "").strip()
        if existing:
            # Already under xvfb-run / a private server. Reuse it so pool
            # mint does not exhaust :90-120 and raise "no free Xvfb display".
            return _mint(existing)
        with _private_xvfb() as display:
            return _mint(display)
    return _mint()


_MINT_GATE = threading.BoundedSemaphore(1)


def _asyncio_needed() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class TurnstileTokenPool:
    """Headless Patchright loop that keeps fresh Turnstile tokens ready."""

    def __init__(
        self,
        *,
        size: int = DEFAULT_POOL_SIZE,
        sitekey: str = DEFAULT_SITEKEY,
        origin: str = DEFAULT_ORIGIN,
        proxy: str = "",
        headed: bool = True,
        visible: bool = False,
        timeout: float = 90.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.size = max(1, int(size or 1))
        self.sitekey = sitekey
        self.origin = origin
        self.proxy = proxy
        self.headed = headed
        self.visible = visible
        self.timeout = timeout
        self._log = log
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._tokens: deque[SolvedToken] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="turnstile-pool", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def snapshot(self, now: float | None = None) -> list[SolvedToken]:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            return [item for item in self._tokens if item.alive(now=stamp)]

    def acquire(self, timeout: float = 60.0) -> SolvedToken:
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            with self._cv:
                self._drop_expired_locked()
                if self._tokens:
                    token = self._tokens.popleft()
                    self._cv.notify_all()
                    return token
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError(self.last_error or "turnstile pool empty")
                self._cv.wait(timeout=min(1.0, remaining))

    def _drop_expired_locked(self, now: float | None = None) -> None:
        stamp = time.time() if now is None else float(now)
        alive = deque(item for item in self._tokens if item.alive(now=stamp))
        self._tokens = alive

    def _need_more(self) -> bool:
        with self._lock:
            self._drop_expired_locked()
            return len(self._tokens) < self.size

    def _put(self, token: SolvedToken) -> None:
        with self._cv:
            self._drop_expired_locked()
            if token.alive():
                self._tokens.append(token)
            self._cv.notify_all()
        publish_pool_snapshot()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._need_more():
                self._stop.wait(0.4)
                continue
            try:
                solved = solve_turnstile_once(
                    sitekey=self.sitekey,
                    origin=self.origin,
                    proxy=self.proxy,
                    headed=self.headed,
                    visible=self.visible,
                    timeout=self.timeout,
                    log=self._log,
                )
                self.last_error = ""
                self._put(solved)
            except Exception as exc:
                self.last_error = str(exc)
                shown = str(exc)
                delay = 1.5
                if "widget=0" in shown:
                    shown = "origin script not installed"
                    delay = 12.0
                _log(self._log, f"pool mint failed: {shown}")
                self._stop.wait(delay)


_pools: dict[tuple[str, str, str], TurnstileTokenPool] = {}
_pools_lock = threading.Lock()


def _config_bool(name: str, default: bool) -> bool:
    try:
        data = json.loads(Path("config.json").read_text(encoding="utf-8"))
    except Exception:
        return default
    if not isinstance(data, dict) or name not in data:
        return default
    return str(data.get(name)).strip().lower() not in {"0", "false", "no", "off"}


def pool_enabled() -> bool:
    raw = os.environ.get("GROK_TURNSTILE_POOL")
    if raw is None or not str(raw).strip():
        return _config_bool("turnstile_pool", False)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def pool_fallback_enabled() -> bool:
    raw = os.environ.get("GROK_TURNSTILE_POOL_FALLBACK")
    if raw is None or not str(raw).strip():
        return _config_bool("turnstile_pool_fallback", True)
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _pool_depth(size: int | None = None) -> int:
    try:
        depth = int(str(os.environ.get("GROK_TURNSTILE_POOL_SIZE") or "").strip() or "0")
    except ValueError:
        depth = 0
    if size is not None:
        return max(1, int(size))
    return depth if depth > 0 else DEFAULT_POOL_SIZE


def pool_snapshot_path() -> Path:
    raw = str(os.environ.get(POOL_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    return Path("log") / "turnstile_pool.json"


def live_pool_snapshot(now: float | None = None) -> dict[str, Any]:
    """Read-only view of in-process pools for the panel status feed."""
    stamp = time.time() if now is None else float(now)
    rows: list[dict[str, Any]] = []
    with _pools_lock:
        pools = list(_pools.values())
    total_ready = 0
    oldest: float | None = None
    depth = _pool_depth()
    for pool in pools:
        alive = pool.snapshot(now=stamp)
        remaining = [item.remaining(now=stamp) for item in alive]
        row_oldest = min(remaining) if remaining else None
        total_ready += len(alive)
        if row_oldest is not None:
            oldest = row_oldest if oldest is None else min(oldest, row_oldest)
        depth = max(depth, int(pool.size or 0))
        rows.append(
            {
                "depth": int(pool.size),
                "ready": len(alive),
                "oldest_remaining_s": None
                if row_oldest is None
                else round(float(row_oldest), 1),
                "proxy": bool(pool.proxy),
                "last_error": str(pool.last_error or "")[:180],
            }
        )
    return {
        "depth": int(depth),
        "ready": int(total_ready),
        "ttl_s": int(TOKEN_TTL_SECONDS),
        "oldest_remaining_s": None if oldest is None else round(float(oldest), 1),
        "pools": rows[:8],
        "updated_at": stamp,
    }


def publish_pool_snapshot() -> None:
    try:
        from secure_files import atomic_write_json

        path = pool_snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, live_pool_snapshot())
    except Exception:
        return


def read_published_pool_snapshot(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    empty = {
        "depth": _pool_depth(),
        "ready": 0,
        "ttl_s": int(TOKEN_TTL_SECONDS),
        "oldest_remaining_s": None,
        "pools": [],
        "updated_at": None,
    }
    target = Path(path) if path is not None else pool_snapshot_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return empty
    if not isinstance(raw, dict):
        return empty
    out = dict(empty)
    for key in ("depth", "ready", "ttl_s"):
        try:
            out[key] = int(raw.get(key) if raw.get(key) is not None else empty[key])
        except (TypeError, ValueError):
            pass
    remain = raw.get("oldest_remaining_s")
    try:
        out["oldest_remaining_s"] = None if remain is None else round(float(remain), 1)
    except (TypeError, ValueError):
        out["oldest_remaining_s"] = None
    pools = raw.get("pools")
    if isinstance(pools, list):
        cleaned = []
        for row in pools[:8]:
            if not isinstance(row, dict):
                continue
            cleaned.append(
                {
                    "depth": int(row.get("depth") or 0),
                    "ready": int(row.get("ready") or 0),
                    "oldest_remaining_s": row.get("oldest_remaining_s"),
                    "proxy": bool(row.get("proxy")),
                    "last_error": str(row.get("last_error") or "")[:180],
                }
            )
        out["pools"] = cleaned
    out["updated_at"] = raw.get("updated_at")
    return out


def _pool_key(sitekey: str, origin: str, proxy: str = "") -> tuple[str, str]:
    """One in-process pool per sitekey+origin so 就绪 maps to a single cap of 5."""
    return (str(sitekey or DEFAULT_SITEKEY), str(origin or DEFAULT_ORIGIN))


def _default_mint_proxy(proxy: str = "") -> str:
    raw = str(proxy or "").strip()
    if raw:
        return raw
    env = str(os.environ.get("GROK_TURNSTILE_PROXY") or "").strip()
    if env:
        return env
    try:
        data = json.loads(Path("config.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("proxy") or "").strip()
    return ""


def ensure_token_pool(
    *,
    sitekey: str = DEFAULT_SITEKEY,
    origin: str = DEFAULT_ORIGIN,
    proxy: str = "",
    size: int | None = None,
    log: Callable[[str], None] | None = None,
) -> TurnstileTokenPool:
    """Start background minting. Does not wait for a token."""
    key = _pool_key(sitekey, origin, proxy)
    mint_proxy = _default_mint_proxy(proxy)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is not None:
            return pool
        pool = TurnstileTokenPool(
            size=_pool_depth(size),
            sitekey=key[0],
            origin=key[1],
            proxy=mint_proxy,
            headed=True,
            visible=False,
            timeout=90.0,
            log=None,
        )
        pool.start()
        _pools[key] = pool
    publish_pool_snapshot()
    _log(
        log,
        f"[*] Patchright token pool start size={pool.size} proxy={'yes' if pool.proxy else 'no'} "
        f"(background mint, register will dequeue)",
    )
    return pool


def try_dequeue_token(
    *,
    sitekey: str = DEFAULT_SITEKEY,
    origin: str = DEFAULT_ORIGIN,
    proxy: str = "",
    log: Callable[[str], None] | None = None,
) -> SolvedToken | None:
    """Return a ready token or None. Does not wait on a dead mint."""
    pool = ensure_token_pool(
        sitekey=sitekey,
        origin=origin,
        proxy=proxy,
        log=log,
    )
    ready = pool.snapshot()
    if not ready:
        _log(log, "[*] Patchright token pool empty; use live-page Turnstile")
        return None
    solved = pool.acquire(timeout=2.0)
    publish_pool_snapshot()
    _log(
        log,
        f"[*] Patchright token pool dequeue how={solved.how} "
        f"len={len(solved.token)} remaining={solved.remaining():.0f}s ready={len(pool.snapshot())}",
    )
    return solved


def wait_pool_at_cap(
    *,
    sitekey: str = DEFAULT_SITEKEY,
    origin: str = DEFAULT_ORIGIN,
    proxy: str = "",
    timeout: float = 900.0,
    log: Callable[[str], None] | None = None,
) -> int:
    """Block until the shared pool has `size` live tokens (or timeout)."""
    pool = ensure_token_pool(
        sitekey=sitekey, origin=origin, proxy=proxy, log=log
    )
    deadline = time.time() + max(1.0, float(timeout))
    last_emit = 0.0
    while time.time() < deadline:
        ready = len(pool.snapshot())
        publish_pool_snapshot()
        if ready >= pool.size:
            _log(log, f"[*] Patchright token pool at cap ready={ready} size={pool.size}")
            return ready
        now = time.time()
        if now - last_emit >= 15.0:
            _log(log, f"[*] Patchright token pool filling ready={ready}/{pool.size}")
            last_emit = now
        time.sleep(1.0)
    ready = len(pool.snapshot())
    publish_pool_snapshot()
    _log(log, f"[!] Patchright token pool wait timeout ready={ready} size={pool.size}")
    return ready


def _inject_explicit_widget(page, sitekey: str, emit: Callable[[str], None] | None) -> None:
    """Put an explicit Turnstile widget on the current origin without replacing the document.

    Protocol signup verifies email via in-page fetch, so the UI often stays on
    the OTP step and never mounts x.ai's own widget. Replacing the document
    keeps nonce CSP and yields widget=0 / error=''.
    """
    _page_eval(page,
        """(sitekey) => {
          window.__tsToken = window.__tsToken || "";
          window.__tsError = window.__tsError || "";
          if (window.__tsWidgetId == null) window.__tsWidgetId = null;
          window.__tsOnToken = function (token) {
            window.__tsToken = String(token || "");
            var input = document.querySelector('input[name="cf-turnstile-response"]');
            if (!input) {
              input = document.createElement("input");
              input.type = "hidden";
              input.name = "cf-turnstile-response";
              document.body.appendChild(input);
            }
            input.value = window.__tsToken;
          };
          if (!document.getElementById("cf-widget")) {
            var host = document.createElement("div");
            host.id = "cf-widget";
            host.className = "cf-turnstile";
            host.setAttribute("data-sitekey", sitekey);
            document.body.appendChild(host);
          }
          if (!document.querySelector('input[name="cf-turnstile-response"]')) {
            var hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "cf-turnstile-response";
            document.body.appendChild(hidden);
          }
        }""",
        sitekey,
    )
    _eval_turnstile_api(page, emit)
    try:
        _wait_turnstile_global(page, 15000)
    except Exception as exc:
        _log(emit, f"live-page inject wait: {exc}")
    _ensure_widget_rendered(page, sitekey)


TOKEN_READY_JS = """
() => {
  const token = String(window.__tsToken || '').trim();
  if (token) return true;
  const input = document.querySelector(
    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
  );
  return !!(input && String(input.value || '').trim());
}
"""


def _reset_widget(page, sitekey: str) -> None:
    _page_eval(page,
        """() => {
          window.__tsToken = "";
          window.__tsError = "";
          try {
            if (window.turnstile && window.__tsWidgetId != null
                && typeof turnstile.reset === 'function') {
              turnstile.reset(window.__tsWidgetId);
            }
          } catch (e) {}
          window.__tsWidgetId = null;
        }"""
    )
    _ensure_widget_rendered(page, sitekey)


def token_from_existing_page(
    page,
    log: Callable[[str], None] | None = None,
    timeout: float = 90.0,
    sitekey: str = DEFAULT_SITEKEY,
) -> str:
    """Click/getToken on a page that already loaded x.ai (native or injected widget)."""
    key = sitekey or DEFAULT_SITEKEY
    try:
        state = _page_eval(page,
            """() => ({
              ready: !!(window.turnstile && typeof turnstile.render === 'function'),
              err: String(window.__tsError || ''),
              scripts: [...document.scripts].filter((s) => String(s.src || '').includes('turnstile')).length
            })"""
        )
    except Exception:
        state = {}
    _log(
        log,
        f"ts_state ready={state.get('ready')} scripts={state.get('scripts')} err={str(state.get('err') or '')[:80]}",
    )
    native = bool(state.get("ready"))
    if not native:
        try:
            page.wait_for_selector(
                'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], script[src*="turnstile"]',
                timeout=15000,
            )
            _log(log, "live-page found site turnstile")
        except Exception:
            _log(log, "live-page site turnstile absent")
        try:
            state = _page_eval(page,
                """() => ({
                  ready: !!(window.turnstile && typeof turnstile.render === 'function'),
                  err: String(window.__tsError || ''),
                  scripts: [...document.scripts].filter((s) => String(s.src || '').includes('turnstile')).length
                })"""
            )
        except Exception:
            state = {}
        native = bool(state.get("ready"))
        _log(
            log,
            f"ts_state2 ready={state.get('ready')} scripts={state.get('scripts')} err={str(state.get('err') or '')[:80]}",
        )
    if not native and int(state.get("scripts") or 0) > 0:
        try:
            _wait_turnstile_global(page, 20000)
            native = True
        except Exception as exc:
            _log(log, f"script-tag wait: {exc}")
    if native:
        _log(log, "live-page use site turnstile")
        try:
            page.locator('input[type="email"], input[name="email"]').first.click(timeout=3000)
            _log(log, "live-page focused email field")
        except Exception:
            _log(log, "live-page email field absent")
    else:
        _log(log, "live-page native widget missing; inject script tag")
        _inject_explicit_widget(page, key, log)
    deadline = time.time() + max(5.0, float(timeout))
    clicked = False
    while time.time() < deadline:
        got = _read_token(page)
        if got["token"]:
            _log(log, f"[*] live-page Turnstile how={got['how']} len={len(got['token'])}")
            return got["token"]
        if got.get("widget") != "1":
            _log(log, "live-page widget still missing; inject script tag")
            _inject_explicit_widget(page, key, log)
        elif got.get("error"):
            _log(log, f"live-page widget error={got.get('error')!r}")
            break
        elif not clicked:
            how = _click_challenge(page, log)
            if how.startswith("frame") or how in {"iframe", "iframe-xy"}:
                clicked = True
        remaining_ms = int(max(0.0, deadline - time.time()) * 1000)
        if remaining_ms < 200:
            break
        try:
            page.wait_for_function(TOKEN_READY_JS, timeout=min(12000, remaining_ms))
        except Exception:
            pass
    got = _read_token(page)
    raise RuntimeError(
        f"live-page turnstile empty widget={got.get('widget')} "
        f"error={got.get('error')!r} url={getattr(page, 'url', '')}"[:400]
    )


def acquire_pooled_token(
    *,
    sitekey: str = DEFAULT_SITEKEY,
    origin: str = DEFAULT_ORIGIN,
    proxy: str = "",
    timeout: float = 90.0,
    size: int | None = None,
    log: Callable[[str], None] | None = None,
) -> SolvedToken:
    """Dequeue a live token. Pool must already be minting in the background."""
    pool = ensure_token_pool(
        sitekey=sitekey,
        origin=origin,
        proxy=proxy,
        size=size,
        log=log,
    )
    ready = len(pool.snapshot())
    if ready == 0:
        _log(log, "[*] Patchright token pool empty; waiting for background mint")
    solved = pool.acquire(timeout=timeout)
    publish_pool_snapshot()
    _log(
        log,
        f"[*] Patchright token pool dequeue how={solved.how} "
        f"len={len(solved.token)} remaining={solved.remaining():.0f}s ready={len(pool.snapshot())}",
    )
    return solved


def stop_all_pools(timeout: float = 3.0) -> None:
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.stop(timeout=timeout)


atexit.register(stop_all_pools)
