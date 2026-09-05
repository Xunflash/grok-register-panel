# -*- coding: utf-8 -*-
"""Turnstile 打码：Capsolver / YesCaptcha 云打码，或本地点击。

本地点击仍走 register_flow.getTurnstileToken 原路径，这里只负责云打码。
TURNSTILE_PROVIDER=local 时即使留着云打码 key 也不走云。
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_YESCAPTCHA_API = "https://cn.yescaptcha.com"
YESCAPTCHA_API_PEERS = (
    "https://cn.yescaptcha.com",
    "https://api.yescaptcha.com",
)
DEFAULT_CAPSOLVER_API = "https://api.capsolver.com"
DEFAULT_XAI_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
CLOUD_PROVIDERS = ("yescaptcha", "capsolver")
PROVIDER_ALIASES = {
    "local": "local",
    "solver": "local",
    "camoufox": "local",
    "click": "local",
    "本地点击": "local",
    "本地": "local",
    "本地打码": "local",
    "yescaptcha": "yescaptcha",
    "yes": "yescaptcha",
    "cloud": "yescaptcha",
    "云": "yescaptcha",
    "云打码": "yescaptcha",
    "capsolver": "capsolver",
    "cap": "capsolver",
    "capsolver.com": "capsolver",
    "auto": "auto",
    "config": "auto",
    "default": "auto",
    "默认": "auto",
    "": "auto",
}


def resolve_turnstile_provider(
    setting: str = "",
    yescaptcha_key: str = "",
    capsolver_key: str = "",
) -> str:
    """Resolve TURNSTILE_PROVIDER to 'local', 'yescaptcha', or 'capsolver'.

    auto: 有 CAPSOLVER_KEY 走 Capsolver，否则有 YESCAPTCHA_KEY 走 YesCaptcha，否则本地。
    显式 local 即使留着 key 也不走云。
    """
    raw = str(setting or "").strip().lower()
    resolved = PROVIDER_ALIASES.get(raw, "auto")
    if resolved == "auto":
        if str(capsolver_key or "").strip():
            return "capsolver"
        return "yescaptcha" if str(yescaptcha_key or "").strip() else "local"
    return resolved


def should_attach_browser_turnstile_hooks(
    provider: str = "",
    *,
    debug: bool = False,
    yescaptcha_key: str = "",
    capsolver_key: str = "",
) -> bool:
    """Cloud solvers need turnstile.render hooked so React gets the token.

    本地点击走真实 widget callback，不必挂钩。Capsolver / YesCaptcha 都必须挂：
    只给 YesCaptcha 挂钩时 Capsolver 会 inject 进隐藏域但 cbs=0，create-account 不发。
    """
    if debug:
        return True
    resolved = resolve_turnstile_provider(
        provider,
        yescaptcha_key=yescaptcha_key,
        capsolver_key=capsolver_key,
    )
    return resolved in CLOUD_PROVIDERS


def _env_get(*names: str, default: str = "") -> str:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and str(raw).strip():
            return str(raw).strip().strip("'\"")
    return default


def _read_config_json(path: Optional[Path] = None) -> dict:
    if path is None:
        env_path = str(os.environ.get("GROK_CONFIG") or "").strip()
        path = Path(env_path) if env_path else Path("config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_turnstile_settings(
    control: Optional[dict] = None,
    config_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Merge env + control.json + config.json. Env wins, then control, then file."""
    cfg = _read_config_json(config_path)
    control = control or {}
    setting = _env_get("TURNSTILE_PROVIDER", "GROK_TURNSTILE_PROVIDER")
    if not setting:
        setting = str(control.get("turnstile_provider") or cfg.get("turnstile_provider") or "local")
    yes_key = _env_get("YESCAPTCHA_KEY", "YESCAPTCHA_API_KEY", "GROK2API_YESCAPTCHA_KEY")
    if not yes_key:
        yes_key = str(cfg.get("yescaptcha_key") or cfg.get("yescaptcha_api_key") or "").strip()
    yes_api = _env_get("YESCAPTCHA_API", "YESCAPTCHA_API_BASE", "YESCAPTCHA_ENDPOINT")
    if not yes_api:
        yes_api = str(cfg.get("yescaptcha_api") or DEFAULT_YESCAPTCHA_API).strip()
    yes_api = (yes_api or DEFAULT_YESCAPTCHA_API).rstrip("/")
    cap_key = _env_get("CAPSOLVER_KEY", "CAPSOLVER_API_KEY", "CAP_KEY")
    if not cap_key:
        cap_key = str(cfg.get("capsolver_key") or cfg.get("capsolver_api_key") or "").strip()
    cap_api = _env_get("CAPSOLVER_API", "CAPSOLVER_API_BASE", "CAPSOLVER_ENDPOINT")
    if not cap_api:
        cap_api = str(cfg.get("capsolver_api") or DEFAULT_CAPSOLVER_API).strip()
    cap_api = (cap_api or DEFAULT_CAPSOLVER_API).rstrip("/")
    fallback_raw = _env_get("TURNSTILE_FALLBACK_LOCAL")
    if fallback_raw:
        fallback_local = fallback_raw.strip().lower() not in {"0", "false", "no", "off"}
    else:
        fallback_local = bool(cfg.get("turnstile_fallback_local", False))
    provider = resolve_turnstile_provider(
        setting=setting,
        yescaptcha_key=yes_key,
        capsolver_key=cap_key,
    )
    if provider == "capsolver":
        key, api = cap_key, cap_api
    else:
        key, api = yes_key, yes_api
    return {
        "setting": setting,
        "provider": provider,
        "key": key,
        "api": api,
        "yescaptcha_key": yes_key,
        "yescaptcha_api": yes_api,
        "capsolver_key": cap_key,
        "capsolver_api": cap_api,
        "fallback_local": fallback_local,
        "sitekey": str(cfg.get("turnstile_sitekey") or DEFAULT_XAI_SITEKEY).strip(),
    }


def persist_turnstile_provider(provider: str, config_path) -> str:
    """Write turnstile_provider into config.json without dropping other keys."""
    resolved = resolve_turnstile_provider(provider)
    if resolved == "auto":
        resolved = "yescaptcha"
    path = Path(config_path)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    data["turnstile_provider"] = resolved
    from secure_files import atomic_write_json

    atomic_write_json(path, data)
    return resolved


def parse_yescaptcha_proxy(proxy_url: str) -> Optional[dict[str, Any]]:
    """转成 YesCaptcha TurnstileTask 代理字段。

    云端 worker 到不了 loopback / 内网（本机 Resin 127.0.0.1:2260），这种返回 None。
    """
    raw = str(proxy_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = str(parsed.hostname or "").strip()
    if not host:
        return None
    lowered = host.lower()
    if lowered in {"localhost", "127.0.0.1", "::1"}:
        return None
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local:
            return None
    except ValueError:
        if lowered.endswith(".local") or lowered.endswith(".internal"):
            return None
    scheme = str(parsed.scheme or "http").lower()
    if scheme.startswith("socks"):
        proxy_type = "socks5" if "5" in scheme else "socks4"
    else:
        proxy_type = "http"
    port = int(parsed.port or (443 if scheme == "https" else 80))
    out: dict[str, Any] = {
        "proxyType": proxy_type,
        "proxyAddress": host,
        "proxyPort": port,
    }
    if parsed.username:
        out["proxyLogin"] = unquote(parsed.username)
    if parsed.password:
        out["proxyPassword"] = unquote(parsed.password)
    return out


def captcha_http_session() -> requests.Session:
    """HTTP client for Capsolver / YesCaptcha APIs.

    Never inherit HTTP(S)_PROXY / Resin 127.0.0.1:2260. Cloud solvers are
    proxyless; routing their API through the signup proxy yields SSL EOF / 502.
    """
    session = requests.Session()
    session.trust_env = False
    session.proxies = {}
    retry = Retry(
        total=3,
        connect=3,
        read=0,
        redirect=0,
        status=0,
        backoff_factor=0.4,
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _session() -> requests.Session:
    return captcha_http_session()


class YesCaptchaTurnstile:
    """YesCaptcha createTask / getTaskResult，协议对齐 proxyscrape-register-bot。"""

    def __init__(
        self,
        client_key: str,
        api_base: str = DEFAULT_YESCAPTCHA_API,
        poll_max: int = 30,
        initial_delay: float = 5.0,
        retry_delay: float = 2.0,
        log: Optional[Callable[[str], None]] = None,
        cancel: Optional[Callable[[], None]] = None,
    ):
        self.client_key = str(client_key or "").strip().strip("'\"")
        self.api_base = (api_base or DEFAULT_YESCAPTCHA_API).rstrip("/")
        self.poll_max = max(5, int(poll_max or 30))
        self.initial_delay = max(0.0, float(initial_delay or 0))
        self.retry_delay = max(0.5, float(retry_delay or 2))
        self._log = log
        self._cancel = cancel
        self.s = _session()

    def _peer_api(self) -> str:
        current = self.api_base.rstrip("/")
        for peer in YESCAPTCHA_API_PEERS:
            if peer.rstrip("/") != current:
                return peer
        return ""

    def _post(self, path: str, payload: dict, timeout: float = 30.0) -> dict:
        url = f"{self.api_base}{path}"
        try:
            response = self.s.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"YesCaptcha non-object response: {data!r}")
            return data
        except Exception as first_err:
            peer = self._peer_api()
            if not peer:
                raise
            self._emit(
                f"[!] YesCaptcha {self.api_base} 失败 ({type(first_err).__name__})，改用 {peer}"
            )
            self.api_base = peer
            response = self.s.post(f"{self.api_base}{path}", json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"YesCaptcha non-object response: {data!r}")
            return data

    def _emit(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    def _raise_if_cancelled(self) -> None:
        if self._cancel:
            self._cancel()

    def create_task(
        self,
        website_url: str,
        website_key: str,
        user_agent: str = "",
        proxy: Optional[dict] = None,
        action: str = "",
    ) -> str:
        if not self.client_key:
            raise RuntimeError("YESCAPTCHA_KEY empty")
        website_url = str(website_url or "").strip()
        website_key = str(website_key or "").strip()
        if not website_url or not website_key:
            raise RuntimeError("YesCaptcha 需要 websiteURL 和 websiteKey")
        use_proxy = bool(proxy)
        task: dict[str, Any] = {
            "type": "TurnstileTask" if use_proxy else "TurnstileTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        if use_proxy:
            task.update(proxy)
        if user_agent:
            task["userAgent"] = user_agent
        if action:
            task["action"] = action
        payload = {"clientKey": self.client_key, "task": task}
        data = self._post("/createTask", payload, timeout=30)
        if data.get("errorId") not in (0, None):
            raise RuntimeError(
                f"YesCaptcha创建任务失败: {data.get('errorDescription') or data}"
            )
        task_id = data.get("taskId") or data.get("task_id")
        if not task_id:
            raise RuntimeError(f"YesCaptcha createTask 无 taskId: {data}")
        kind = "TurnstileTask" if use_proxy else "TurnstileTaskProxyless"
        self._emit(f"[*] YesCaptcha task={task_id} type={kind} key={website_key[:12]}…")
        return str(task_id)

    def get_response(self, task_id: str) -> str:
        self._raise_if_cancelled()
        if self.initial_delay:
            time.sleep(self.initial_delay)
        last = None
        for _ in range(self.poll_max):
            self._raise_if_cancelled()
            data = self._post(
                "/getTaskResult",
                {"clientKey": self.client_key, "taskId": task_id},
                timeout=30,
            )
            last = data
            if data.get("errorId") not in (0, None):
                raise RuntimeError(
                    f"YesCaptcha获取结果失败: {data.get('errorDescription') or data}"
                )
            status = data.get("status")
            if status == "ready":
                token = (
                    (data.get("solution") or {}).get("token")
                    or (data.get("solution") or {}).get("gRecaptchaResponse")
                    or ""
                )
                token = str(token or "").strip()
                if not token:
                    raise RuntimeError("YesCaptcha返回结果中没有token")
                return token
            if status == "processing":
                time.sleep(self.retry_delay)
                continue
            time.sleep(self.retry_delay)
        raise RuntimeError(f"YesCaptcha 超时未出 token: {last}")

    def solve(
        self,
        website_url: str,
        website_key: str,
        user_agent: str = "",
        proxy: Optional[dict] = None,
        action: str = "",
    ) -> str:
        try:
            task_id = self.create_task(
                website_url,
                website_key,
                user_agent=user_agent,
                proxy=proxy,
                action=action,
            )
        except Exception as exc:
            if proxy:
                self._emit(f"[!] YesCaptcha 带代理建任务失败，改 Proxyless: {exc}")
                task_id = self.create_task(
                    website_url,
                    website_key,
                    user_agent=user_agent,
                    proxy=None,
                    action=action,
                )
            else:
                raise
        token = self.get_response(task_id)
        self._emit(f"[*] YesCaptcha 打码完成，token长度={len(token)}")
        return token


class CapSolverTurnstile:
    """Capsolver createTask / getTaskResult，AntiTurnstileTaskProxyLess（不走代理）。"""

    def __init__(
        self,
        client_key: str,
        api_base: str = DEFAULT_CAPSOLVER_API,
        poll_max: int = 30,
        initial_delay: float = 1.0,
        retry_delay: float = 1.0,
        log: Optional[Callable[[str], None]] = None,
        cancel: Optional[Callable[[], None]] = None,
    ):
        self.client_key = str(client_key or "").strip().strip("'\"")
        self.api_base = (api_base or DEFAULT_CAPSOLVER_API).rstrip("/")
        self.poll_max = max(5, int(poll_max or 30))
        self.initial_delay = max(0.0, float(initial_delay or 0))
        self.retry_delay = max(0.0, float(retry_delay if retry_delay is not None else 1))
        self._log = log
        self._cancel = cancel
        self.s = _session()

    def _emit(self, msg: str) -> None:
        if self._log:
            self._log(msg)

    def _raise_if_cancelled(self) -> None:
        if self._cancel:
            self._cancel()

    def _post(self, path: str, payload: dict, timeout: float = 30.0) -> dict:
        url = f"{self.api_base}{path}"
        response = self.s.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Capsolver non-object response: {data!r}")
        return data

    def create_task(
        self,
        website_url: str,
        website_key: str,
        action: str = "",
        cdata: str = "",
    ) -> str:
        if not self.client_key:
            raise RuntimeError("CAPSOLVER_KEY empty")
        website_url = str(website_url or "").strip()
        website_key = str(website_key or "").strip()
        if not website_url or not website_key:
            raise RuntimeError("Capsolver 需要 websiteURL 和 websiteKey")
        task: dict[str, Any] = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        metadata: dict[str, str] = {}
        if action:
            metadata["action"] = str(action)
        if cdata:
            metadata["cdata"] = str(cdata)
        if metadata:
            task["metadata"] = metadata
        payload = {"clientKey": self.client_key, "task": task}
        data = self._post("/createTask", payload, timeout=30)
        if data.get("errorId") not in (0, None):
            raise RuntimeError(
                f"Capsolver创建任务失败: {data.get('errorDescription') or data.get('errorCode') or data}"
            )
        task_id = data.get("taskId") or data.get("task_id")
        if not task_id:
            raise RuntimeError(f"Capsolver createTask 无 taskId: {data}")
        self._emit(
            f"[*] Capsolver task={task_id} type=AntiTurnstileTaskProxyLess key={website_key[:12]}…"
        )
        return str(task_id)

    def get_response(self, task_id: str) -> str:
        self._raise_if_cancelled()
        if self.initial_delay:
            time.sleep(self.initial_delay)
        last = None
        for _ in range(self.poll_max):
            self._raise_if_cancelled()
            data = self._post(
                "/getTaskResult",
                {"clientKey": self.client_key, "taskId": task_id},
                timeout=30,
            )
            last = data
            if data.get("errorId") not in (0, None):
                raise RuntimeError(
                    f"Capsolver获取结果失败: {data.get('errorDescription') or data.get('errorCode') or data}"
                )
            status = data.get("status")
            if status == "ready":
                solution = data.get("solution") or {}
                token = (
                    solution.get("token")
                    or solution.get("gRecaptchaResponse")
                    or ""
                )
                token = str(token or "").strip()
                if not token:
                    raise RuntimeError("Capsolver返回结果中没有token")
                return token
            if status in {"failed", "error"}:
                raise RuntimeError(f"Capsolver solve failed: {data}")
            time.sleep(self.retry_delay)
        raise RuntimeError(f"Capsolver 超时未出 token: {last}")

    def solve(
        self,
        website_url: str,
        website_key: str,
        action: str = "",
        cdata: str = "",
    ) -> str:
        task_id = self.create_task(
            website_url,
            website_key,
            action=action,
            cdata=cdata,
        )
        token = self.get_response(task_id)
        self._emit(f"[*] Capsolver 打码完成，token长度={len(token)}")
        return token


def solve_turnstile_yescaptcha(
    website_url: str,
    website_key: str,
    *,
    user_agent: str = "",
    proxy_url: str = "",
    action: str = "",
    settings: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel: Optional[Callable[[], None]] = None,
) -> str:
    settings = settings or load_turnstile_settings()
    proxy = parse_yescaptcha_proxy(proxy_url)
    if proxy_url and not proxy and log:
        log("[*] YesCaptcha 代理是本地/内网，云端到不了，改用 Proxyless")
    elif proxy and log:
        log(
            f"[*] YesCaptcha 使用带代理 TurnstileTask "
            f"{proxy.get('proxyType')}://{proxy.get('proxyAddress')}:{proxy.get('proxyPort')}"
        )
    solver = YesCaptchaTurnstile(
        client_key=str(settings.get("yescaptcha_key") or settings.get("key") or ""),
        api_base=str(settings.get("yescaptcha_api") or settings.get("api") or DEFAULT_YESCAPTCHA_API),
        log=log,
        cancel=cancel,
    )
    return solver.solve(
        website_url,
        website_key,
        user_agent=user_agent,
        proxy=proxy,
        action=action,
    )


def solve_turnstile_capsolver(
    website_url: str,
    website_key: str,
    *,
    action: str = "",
    cdata: str = "",
    settings: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel: Optional[Callable[[], None]] = None,
) -> str:
    settings = settings or load_turnstile_settings()
    if log:
        log("[*] Capsolver AntiTurnstileTaskProxyLess（不使用代理 / User-Agent）")
    solver = CapSolverTurnstile(
        client_key=str(settings.get("capsolver_key") or settings.get("key") or ""),
        api_base=str(settings.get("capsolver_api") or settings.get("api") or DEFAULT_CAPSOLVER_API),
        log=log,
        cancel=cancel,
    )
    return solver.solve(
        website_url,
        website_key,
        action=action,
        cdata=cdata,
    )


def solve_turnstile(
    website_url: str,
    website_key: str,
    *,
    user_agent: str = "",
    proxy_url: str = "",
    action: str = "",
    cdata: str = "",
    settings: Optional[dict] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel: Optional[Callable[[], None]] = None,
) -> str:
    settings = settings or load_turnstile_settings()
    if settings.get("provider") == "capsolver":
        return solve_turnstile_capsolver(
            website_url,
            website_key,
            action=action,
            cdata=cdata,
            settings=settings,
            log=log,
            cancel=cancel,
        )
    return solve_turnstile_yescaptcha(
        website_url,
        website_key,
        user_agent=user_agent,
        proxy_url=proxy_url,
        action=action,
        settings=settings,
        log=log,
        cancel=cancel,
    )


_live_token_tls = threading.local()
_CAPTCHA_KEY_RE = re.compile(r"turnstile|captcha|cf[-_]?token|cfTurnstile", re.I)

_FETCH_INIT_SCRIPT = r"""
(() => {
  if (window.__grokCfHooked) return;
  window.__grokCfHooked = true;
  window.__grokCfToken = window.__grokCfToken || '';
  window.__grokTsCallbacks = window.__grokTsCallbacks || [];
  const wrapTurnstile = () => {
    const ts = window.turnstile;
    if (!ts || ts.__grokWrapped) return !!ts;
    const stealCb = (opts) => {
      try {
        if (opts && typeof opts.callback === 'function') {
          window.__grokTsCallbacks.push(opts.callback);
        }
      } catch (e) {}
    };
    if (typeof ts.render === 'function') {
      const orig = ts.render.bind(ts);
      ts.render = function (el, opts) {
        stealCb(opts);
        return orig(el, opts);
      };
    }
    if (typeof ts.execute === 'function') {
      const origEx = ts.execute.bind(ts);
      ts.execute = function (el, opts) {
        stealCb(opts);
        return origEx(el, opts);
      };
    }
    ts.__grokWrapped = true;
    return true;
  };
  wrapTurnstile();
  const hookTimer = setInterval(() => {
    if (wrapTurnstile()) clearInterval(hookTimer);
  }, 5);
  setTimeout(() => clearInterval(hookTimer), 45000);
  const patchBody = (body) => {
    const token = window.__grokCfToken;
    if (!token || typeof body !== 'string' || body.length < 2) return body;
    try {
      const j = JSON.parse(body);
      if (!j || typeof j !== 'object' || Array.isArray(j)) return body;
      let changed = false;
      const walk = (obj) => {
        if (!obj || typeof obj !== 'object') return;
        if (Array.isArray(obj)) { obj.forEach(walk); return; }
        Object.keys(obj).forEach((k) => {
          if (/turnstile|captcha|cf[-_]?token|cfTurnstile/i.test(k)) {
            if (!obj[k] || String(obj[k]).length < 80) { obj[k] = token; changed = true; }
          } else walk(obj[k]);
        });
      };
      walk(j);
      return changed ? JSON.stringify(j) : body;
    } catch (e) { return body; }
  };
  if (typeof window.fetch === 'function') {
    const orig = window.fetch.bind(window);
    window.fetch = function (input, init) {
      try {
        if (init && init.body) init = Object.assign({}, init, { body: patchBody(init.body) });
      } catch (e) {}
      return orig(input, init);
    };
  }
  if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
    const origSend = window.XMLHttpRequest.prototype.send;
    window.XMLHttpRequest.prototype.send = function (body) {
      return origSend.call(this, patchBody(body));
    };
  }
})();
"""


def set_live_token(token: str) -> None:
    value = str(token or "").strip()
    _live_token_tls.token = value
    _live_token_tls.at = time.time() if value else 0.0


def get_live_token(max_age: float = 110.0) -> str:
    token = str(getattr(_live_token_tls, "token", "") or "").strip()
    at = float(getattr(_live_token_tls, "at", 0) or 0)
    if len(token) >= 80 and (time.time() - at) < max_age:
        return token
    return ""


def patch_post_body(body: str, token: str) -> str:
    """把 token 填进 JSON/表单里已有的 captcha 字段；没有匹配字段则原样返回。"""
    raw = body if isinstance(body, str) else ""
    token = str(token or "").strip()
    if not raw or not token:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        changed = False

        def walk(obj):
            nonlocal changed
            if isinstance(obj, dict):
                for key, value in list(obj.items()):
                    if _CAPTCHA_KEY_RE.search(str(key)):
                        if not value or (isinstance(value, str) and len(value) < 80):
                            obj[key] = token
                            changed = True
                    else:
                        walk(value)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(data)
        if changed:
            return json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return raw
    if "cf-turnstile-response=" in raw:
        return re.sub(
            r"cf-turnstile-response=[^&]*",
            "cf-turnstile-response=" + token,
            raw,
            count=1,
        )
    return raw


def ensure_page_turnstile_hooks(evaluator) -> dict:
    """Install widget/fetch hooks on the *current* document.

    add_init_script only runs on the next navigation. Cloud inject must wrap
    turnstile.render on the already-loaded sign-up page so React's callback
    is captured (cbs>=1). evaluator is CamoufoxPage.run_js / similar.
    """
    try:
        evaluator(_FETCH_INIT_SCRIPT)
    except Exception:
        pass
    try:
        meta = evaluator(
            "return {cbs:(window.__grokTsCallbacks||[]).length, hasTs:!!window.turnstile, hooked:!!window.__grokCfHooked};"
        )
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def attach_browser_turnstile_hooks(raw_page, context=None, log_callback=None) -> None:
    """在 Camoufox page 上装 fetch 钩子 + 注册 POST 改写 + 可选请求日志。"""
    debug = str(os.environ.get("GROK_CF_DEBUG") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ctx = context
    try:
        ctx = ctx or raw_page.context
    except Exception:
        ctx = context
    already = bool(getattr(raw_page, "_grok_ts_hooks", False))
    try:
        raw_page._grok_ts_hooks = True
    except Exception:
        already = already
    for target in (ctx, raw_page):
        if target is None:
            continue
        try:
            target.add_init_script(_FETCH_INIT_SCRIPT)
        except Exception:
            pass
    # Current document (about:blank or already-open sign-up) also needs the wrap.
    try:
        raw_page.evaluate(_FETCH_INIT_SCRIPT)
    except Exception:
        pass
    if already:
        return

    def _on_request(req):
        if not debug:
            return
        try:
            if str(req.method or "") != "POST":
                return
            url = str(req.url or "")
            if "x.ai" not in url and "grok.com" not in url:
                return
            body = ""
            try:
                body = req.post_data or ""
            except Exception:
                body = ""
            ctype = ""
            try:
                ctype = (req.headers or {}).get("content-type") or ""
            except Exception:
                ctype = ""
            if log_callback:
                log_callback(
                    f"[cf-debug] POST {url[:180]} ctype={ctype[:60]} body={str(body)[:700]}"
                )
        except Exception:
            return

    try:
        raw_page.on("request", _on_request)
    except Exception:
        pass

    def _handle_route(route):
        req = route.request
        try:
            token = get_live_token()
            if (
                token
                and str(req.method or "") == "POST"
                and ("accounts.x.ai" in str(req.url) or "auth.x.ai" in str(req.url))
            ):
                body = req.post_data
                if body:
                    new_body = patch_post_body(body, token)
                    if new_body != body:
                        if log_callback:
                            log_callback(f"[*] 已把 Turnstile token 写入请求 {str(req.url)[:140]}")
                        route.continue_(post_data=new_body)
                        return
        except Exception as exc:
            if debug and log_callback:
                log_callback(f"[cf-debug] route patch fail: {exc}")
        route.continue_()

    for pattern in ("**://accounts.x.ai/**", "**://auth.x.ai/**"):
        try:
            raw_page.route(pattern, _handle_route)
        except Exception:
            continue
        if log_callback and debug:
            log_callback(f"[cf-debug] routed {pattern}")

