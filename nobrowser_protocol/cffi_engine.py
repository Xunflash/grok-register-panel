# -*- coding: utf-8 -*-
"""curl_cffi protocol adapter. Does not start Camoufox or any browser."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
CFFI = ROOT / "cffi_har_signup"


def _ensure_cffi_path() -> None:
    import sys

    if str(CFFI) not in sys.path:
        sys.path.insert(0, str(CFFI))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def camoufox_imports() -> tuple[str, ...]:
    return ()


def run_cffi_one(
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
    """Live curl_cffi signup. Castle/Sentry must come from a live page; this engine
    does not invent them and will fail preflight if they are missing."""
    _ensure_cffi_path()
    from run_cffi_signup import one_account

    emit = log or (lambda _msg: None)
    emit("engine=cffi camoufox=no")
    return one_account(
        har=har,
        proxy=proxy,
        account=account,
        capsolver_key=capsolver_key,
        capsolver_api=capsolver_api,
        guard_url=guard_url,
        guard_token=guard_token,
        grok2api=grok2api,
    )
