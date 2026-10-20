"""Bounded retry policy shared by browser, batch, and orchestrator layers."""

from __future__ import annotations

import os
from typing import Mapping


BROWSER_START_ATTEMPTS_DEFAULT = 2
PROXY_BOOT_ROTATIONS_DEFAULT = 3
SLOT_RETRIES_DEFAULT = 1
BATCH_MAX_RESTARTS_DEFAULT = 2
ORCH_MAX_CONSECUTIVE_FAILURES_DEFAULT = 2
PROTOCOL_PRE_INBOX_RETRIES_DEFAULT = 3
PRECHECK_EXIT_CODE = 78


class ProtocolResourceUsage:
    """Track whether a protocol/Patchright attempt spent mailbox or remote captcha."""

    __slots__ = ("email_used", "remote_captcha_used")

    def __init__(self) -> None:
        self.email_used = False
        self.remote_captcha_used = False


class ReusableResourceError(Exception):
    """Pre-inbox failure that did not consume mailbox or remote captcha.

    Workers should reopen the browser / switch exit and retry. Do not increment
    fail_count: the mailbox and cloud solver budget are still reusable.
    """

    def __init__(
        self,
        message: str = "",
        *,
        email_used: bool = False,
        remote_captcha_used: bool = False,
    ) -> None:
        super().__init__(message)
        self.email_used = bool(email_used)
        self.remote_captcha_used = bool(remote_captcha_used)


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
        except Exception as exc:
            if exc.__class__.__name__ == "RegistrationCancelled":
                raise
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


def is_mailbox_code_miss(exc: BaseException | None) -> bool:
    """Outlook/inbox never produced a code. Switch mailbox; do not count a failure."""
    msg = str(exc or "")
    return "未收到验证码" in msg or "收件箱为空" in msg


def is_unscored_attempt(exc: BaseException | None) -> bool:
    """Proxy bounce or an exhausted send-code switch. Keep going; do not score a failure."""
    msg = str(exc or "")
    if "ERR_NETWORK_CHANGED" in msg:
        return True
    if "已换" not in msg:
        return False
    return (
        "account:email-signup-unavailable" in msg
        or "email:send-validation-code-rate-limited" in msg
    )


def should_count_as_failure(exc: BaseException | None) -> bool:
    """Fail stats only after mailbox send-code or a paid remote captcha spend."""
    if exc is None:
        return False
    if is_mailbox_code_miss(exc) or is_unscored_attempt(exc):
        return False
    if getattr(exc, "email_used", False) or getattr(exc, "remote_captcha_used", False):
        return True
    if isinstance(exc, ReusableResourceError):
        return False
    return True


def bounded_env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int,
    environ: Mapping[str, str] | None = None,
) -> int:
    source = os.environ if environ is None else environ
    try:
        value = int(str(source.get(name, default)).strip())
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


def browser_start_attempts(environ: Mapping[str, str] | None = None) -> int:
    return bounded_env_int(
        "GROK_BROWSER_START_ATTEMPTS",
        BROWSER_START_ATTEMPTS_DEFAULT,
        minimum=1,
        maximum=4,
        environ=environ,
    )


def proxy_boot_rotations(environ: Mapping[str, str] | None = None) -> int:
    return bounded_env_int(
        "GROK_PROXY_BOOT_ROTATIONS",
        PROXY_BOOT_ROTATIONS_DEFAULT,
        minimum=0,
        maximum=10,
        environ=environ,
    )


def slot_retries(environ: Mapping[str, str] | None = None) -> int:
    return bounded_env_int(
        "GROK_SLOT_RETRIES",
        SLOT_RETRIES_DEFAULT,
        minimum=0,
        maximum=3,
        environ=environ,
    )


def orchestrator_failure_limit(environ: Mapping[str, str] | None = None) -> int:
    return bounded_env_int(
        "GROK_ORCH_MAX_CONSECUTIVE_FAILURES",
        ORCH_MAX_CONSECUTIVE_FAILURES_DEFAULT,
        minimum=1,
        maximum=10,
        environ=environ,
    )


def protocol_pre_inbox_retries(environ: Mapping[str, str] | None = None) -> int:
    return bounded_env_int(
        "GROK_PROTOCOL_PRE_INBOX_RETRIES",
        PROTOCOL_PRE_INBOX_RETRIES_DEFAULT,
        minimum=1,
        maximum=8,
        environ=environ,
    )
