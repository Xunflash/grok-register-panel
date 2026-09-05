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


def should_count_as_failure(exc: BaseException | None) -> bool:
    """Fail stats only after mailbox send-code or a paid remote captcha spend."""
    if exc is None:
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
