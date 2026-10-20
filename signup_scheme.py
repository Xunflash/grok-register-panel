# -*- coding: utf-8 -*-
"""Registration scheme: original Camoufox browser flow vs HAR protocol replay."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

SCHEME_BROWSER = "browser"
SCHEME_PROTOCOL = "protocol"
SCHEME_PROTOCOL_PATCHRIGHT = "protocol_patchright"
ENV_NAME = "GROK_REGISTRATION_SCHEME"
VALID_SCHEMES = (SCHEME_BROWSER, SCHEME_PROTOCOL, SCHEME_PROTOCOL_PATCHRIGHT)

SCHEME_ALIASES = {
    "browser": SCHEME_BROWSER,
    "camoufox": SCHEME_BROWSER,
    "click": SCHEME_BROWSER,
    "gui": SCHEME_BROWSER,
    "form": SCHEME_BROWSER,
    "local": SCHEME_BROWSER,
    "浏览器": SCHEME_BROWSER,
    "浏览器方案": SCHEME_BROWSER,
    "浏览器注册": SCHEME_BROWSER,
    "protocol": SCHEME_PROTOCOL,
    "http": SCHEME_PROTOCOL,
    "api": SCHEME_PROTOCOL,
    "har": SCHEME_PROTOCOL,
    "协议": SCHEME_PROTOCOL,
    "协议方案": SCHEME_PROTOCOL,
    "协议注册": SCHEME_PROTOCOL,
    "protocol_patchright": SCHEME_PROTOCOL_PATCHRIGHT,
    "protocol-patchright": SCHEME_PROTOCOL_PATCHRIGHT,
    "patchright": SCHEME_PROTOCOL_PATCHRIGHT,
    "patchwright": SCHEME_PROTOCOL_PATCHRIGHT,
    "协议注册（patchright）": SCHEME_PROTOCOL_PATCHRIGHT,
    "协议注册(patchright)": SCHEME_PROTOCOL_PATCHRIGHT,
}

SCHEME_LABELS = {
    SCHEME_BROWSER: "浏览器注册",
    SCHEME_PROTOCOL: "协议注册",
    SCHEME_PROTOCOL_PATCHRIGHT: "协议注册（patchright）",
}


def resolve_registration_scheme(value: Any, default: str = SCHEME_BROWSER) -> str:
    raw = str(value or "").strip()
    if not raw:
        fallback = str(default or SCHEME_BROWSER).strip() or SCHEME_BROWSER
        return fallback if fallback in VALID_SCHEMES else SCHEME_BROWSER
    mapped = SCHEME_ALIASES.get(raw) or SCHEME_ALIASES.get(raw.lower())
    if mapped:
        return mapped
    fallback = str(default or SCHEME_BROWSER).strip() or SCHEME_BROWSER
    return fallback if fallback in VALID_SCHEMES else SCHEME_BROWSER


def load_registration_scheme(
    *,
    env: Optional[dict] = None,
    config: Optional[dict] = None,
    control: Optional[dict] = None,
) -> str:
    """Resolve scheme. Env wins, then control, then config, else browser."""
    env_map = os.environ if env is None else env
    for key in (ENV_NAME, "REGISTRATION_SCHEME"):
        raw = None
        if hasattr(env_map, "get"):
            raw = env_map.get(key)
        if raw is not None and str(raw).strip():
            return resolve_registration_scheme(raw)
    if isinstance(control, dict) and control.get("registration_scheme"):
        return resolve_registration_scheme(control.get("registration_scheme"))
    if isinstance(config, dict) and config.get("registration_scheme"):
        return resolve_registration_scheme(config.get("registration_scheme"))
    return SCHEME_BROWSER


def persist_registration_scheme(scheme: str, config_path) -> str:
    """Write registration_scheme into config.json without dropping other keys."""
    resolved = resolve_registration_scheme(scheme)
    path = Path(config_path)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    data["registration_scheme"] = resolved
    from secure_files import atomic_write_json

    atomic_write_json(path, data)
    return resolved


def _load_protocol_signup() -> Callable[..., dict]:
    from protocol_register_once import run_protocol_signup

    return run_protocol_signup


def _load_patchright_signup() -> Callable[..., dict]:
    return _run_patchright_signup


def retry_patchright_mailboxes(
    *,
    take_mailbox: Callable[[], tuple[str, str]],
    signup: Callable[[str, str], dict],
    mark_used: Callable[[str], None],
    log: Callable[[str], Any],
    cancel: Callable[[], None],
    max_tries: int = 3,
) -> dict:
    """Take a new mailbox when create-account reports existingEmail; mark used."""
    from nobrowser_protocol.patchright_engine import ExistingEmailError

    last_existing = ""
    missed_code = False
    tries = max(1, int(max_tries or 3))
    for mail_try in range(1, tries + 1):
        cancel()
        email, mail_token = take_mailbox()
        log(f"[*] patchright mailbox {email} (try {mail_try}/{tries})")
        try:
            outcome = signup(email, mail_token)
        except ExistingEmailError as exc:
            last_existing = str(exc.email or email)
            mark_used(last_existing)
            log(f"[!] existingEmail={last_existing}, retry another mailbox")
            continue
        except Exception as exc:
            from retry_policy import is_mailbox_code_miss

            if not is_mailbox_code_miss(exc):
                raise
            missed_code = True
            last_existing = email
            try:
                mark_used(email)
            except Exception:
                pass
            log(f"[!] 验证码未到，换邮箱重试（不计失败）: {exc}")
            continue
        if not isinstance(outcome, dict) or not outcome.get("sso"):
            raise RuntimeError("patchright signup returned no sso")
        return outcome
    if missed_code:
        raise RuntimeError(f"未收到验证码，已换 {tries} 个邮箱 last={last_existing}")
    raise RuntimeError(f"existingEmail after mailbox retries last={last_existing}")


def recycle_patchright_exit(log_fn: Callable[[str], Any]) -> None:
    """Switch exit for an unspent Patchright retry. Do not start Camoufox."""
    try:
        from browser_session import try_rotate_proxy

        rotated = try_rotate_proxy("patchright pre-inbox retry", log_fn)
        if rotated:
            log_fn("[*] 协议预检：已换出口")
    except Exception as exc:
        log_fn(f"[!] patchright 换出口失败: {exc}")


def run_patchright_pre_inbox(
    run_once,
    *,
    recycle,
    log_fn,
    cancel=None,
    attempts: int | None = None,
):
    """Same pre-inbox retry as 协议注册: unspent errors are ReusableResourceError."""
    from retry_policy import protocol_pre_inbox_retries, retry_protocol_pre_inbox

    return retry_protocol_pre_inbox(
        run_once,
        recycle=recycle,
        attempts=protocol_pre_inbox_retries() if attempts is None else attempts,
        log_fn=log_fn,
        cancel=cancel,
    )


def _run_patchright_signup(
    log_callback: Optional[Callable[[str], Any]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
) -> dict:
    """Panel worker entry: panel mailbox/proxy/Turnstile + Patchright HAR trio."""
    from browser_session import get_bound_proxy
    from email_providers.outlook_rt import release_reservation
    from grok_register_ttk import get_email_and_token, get_oai_code, mark_outlook_rt_used
    from nobrowser_protocol.patchright_engine import SIGNUP_URL, signup_with_patchright
    from signup_protocol import load_har
    from turnstile_solver import solve_turnstile as panel_solve_turnstile

    emit = log_callback or (lambda _m: None)

    def _cancel() -> None:
        if callable(cancel_callback) and cancel_callback():
            from grok_register_ttk import RegistrationCancelled

            raise RegistrationCancelled("stopped")

    _cancel()
    har_path = None
    root = Path(__file__).resolve().parent
    for candidate in (
        root / "log" / "signup-protocol-full.har",
        root / "log" / "signup-protocol.har",
        root / "tests" / "fixtures" / "signup_har_excerpt.json",
    ):
        if candidate.is_file():
            har_path = candidate
            break
    if har_path is None:
        raise RuntimeError("protocol HAR missing for patchright signup")
    har = load_har(har_path)

    def _once(usage) -> dict:
        proxy = get_bound_proxy()

        def signup(email: str, mail_token: str) -> dict:
            def wait_otp(_after_ts: float, _email=email, _token=mail_token) -> str:
                _cancel()
                return get_oai_code(
                    _token,
                    _email,
                    log_callback=emit,
                    cancel_callback=cancel_callback,
                )

            def solve_ts(sitekey: str) -> str:
                _cancel()
                return panel_solve_turnstile(
                    SIGNUP_URL,
                    sitekey or "0x4AAAAAAAhr9JGVDZbrZOo0",
                    log=emit,
                    cancel=_cancel,
                )

            try:
                return signup_with_patchright(
                    har=har,
                    proxy=proxy,
                    email=email,
                    wait_otp=wait_otp,
                    solve_ts=solve_ts,
                    log=emit,
                    usage=usage,
                )
            except Exception:
                if not getattr(usage, "email_used", False) and not getattr(
                    usage, "remote_captcha_used", False
                ):
                    try:
                        release_reservation(mail_token, email)
                    except Exception:
                        pass
                raise

        def mark_used(email: str) -> None:
            if usage is not None:
                usage.email_used = True
            mark_outlook_rt_used(email, reason="existing_email", log_callback=emit)

        return retry_patchright_mailboxes(
            take_mailbox=get_email_and_token,
            signup=signup,
            mark_used=mark_used,
            log=emit,
            cancel=_cancel,
            max_tries=3,
        )

    outcome = run_patchright_pre_inbox(
        _once,
        recycle=lambda: recycle_patchright_exit(emit),
        log_fn=emit,
        cancel=_cancel,
    )
    return {
        "email": outcome["email"],
        "profile": {
            "password": outcome["password"],
            "given_name": outcome["given_name"],
            "family_name": outcome["family_name"],
        },
        "sso": outcome["sso"],
        "scheme": SCHEME_PROTOCOL_PATCHRIGHT,
    }


def _prepare_protocol_browser(log_fn: Callable[[str], Any]) -> None:
    from browser_session import active_browser, restart_browser, start_browser

    if active_browser() is None:
        start_browser(log_callback=log_fn)
        return
    restart_browser(log_callback=log_fn)


def _close_protocol_browser(log_fn: Callable[[str], Any]) -> None:
    from browser_session import stop_browser

    stop_browser(force=True)
    log_fn("[*] 协议方案已关闭浏览器，避免 CPA 与 Resin CONNECT 争用")


def maybe_run_protocol_signup(
    log_callback: Optional[Callable[[str], Any]] = None,
    cancel_callback: Optional[Callable[[], bool]] = None,
    *,
    stop_browser_after: bool = True,
) -> Optional[dict]:
    """If scheme is protocol, run HAR replay signup and return email/profile/sso.

    Browser scheme returns None so the caller keeps the original Camoufox path.
    """
    log_fn = log_callback or (lambda _m: None)
    scheme = load_registration_scheme()
    if scheme == SCHEME_PROTOCOL_PATCHRIGHT:
        log_fn(
            f"[*] 注册方案: {SCHEME_LABELS[SCHEME_PROTOCOL_PATCHRIGHT]}"
            "（Patchright Chromium HAR 回放，Turnstile 走云打码，不启动 Camoufox）"
        )
        runner = _load_patchright_signup()
        result = runner(log_callback=log_fn, cancel_callback=cancel_callback)
        if not isinstance(result, dict) or not result.get("sso"):
            raise RuntimeError("patchright signup returned no sso")
        result.setdefault("scheme", SCHEME_PROTOCOL_PATCHRIGHT)
        return result
    if scheme != SCHEME_PROTOCOL:
        return None
    log_fn(f"[*] 注册方案: {SCHEME_LABELS[SCHEME_PROTOCOL]}（HAR 回放，Turnstile 走云打码）")
    _prepare_protocol_browser(log_fn)
    runner = _load_protocol_signup()
    result = runner(log_callback=log_fn, cancel_callback=cancel_callback)
    if stop_browser_after:
        try:
            _close_protocol_browser(log_fn)
        except Exception as exc:
            log_fn(f"[!] 关闭浏览器失败（继续 CPA）: {exc}")
    if not isinstance(result, dict) or not result.get("sso"):
        raise RuntimeError("protocol signup returned no sso")
    result.setdefault("scheme", SCHEME_PROTOCOL)
    return result
