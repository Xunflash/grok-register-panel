# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from register_flow import (  # noqa: E402
    SSO_ACCOUNTS_HOLD_SECONDS,
    SSO_FINAL_NO_SUBMIT_TIMEOUT,
    SSO_GROK_NUDGE_MIN_ELAPSED,
    SSO_WAIT_TIMEOUT_DEFAULT,
    SSO_WAIT_TIMEOUT_FLOOR,
    sso_wait_seconds,
    wait_for_sso_cookie,
)


def test_sso_wait_seconds_default_is_long_enough(monkeypatch):
    monkeypatch.delenv("SSO_WAIT_TIMEOUT", raising=False)
    assert sso_wait_seconds() == SSO_WAIT_TIMEOUT_DEFAULT
    assert sso_wait_seconds() >= 90
    assert sso_wait_seconds(None) >= 90
    # explicit short timeout still respects the floor
    assert sso_wait_seconds(20) == SSO_WAIT_TIMEOUT_FLOOR
    assert sso_wait_seconds(20) >= 60
    assert sso_wait_seconds(120) == 120


def test_sso_wait_seconds_env_overrides(monkeypatch):
    monkeypatch.setenv("SSO_WAIT_TIMEOUT", "150")
    assert sso_wait_seconds(20) == 150
    monkeypatch.setenv("SSO_WAIT_TIMEOUT", "0")
    assert sso_wait_seconds() == SSO_WAIT_TIMEOUT_DEFAULT


def test_wait_for_sso_cookie_uses_shipped_budget():
    source = inspect.getsource(wait_for_sso_cookie)
    assert "sso_wait_seconds(timeout)" in source
    assert "SSO_ACCOUNTS_HOLD_SECONDS" in source
    assert "SSO_GROK_NUDGE_MIN_ELAPSED" in source
    assert SSO_ACCOUNTS_HOLD_SECONDS >= 40
    assert SSO_GROK_NUDGE_MIN_ELAPSED >= 55
    assert SSO_FINAL_NO_SUBMIT_TIMEOUT >= 20
    assert SSO_GROK_NUDGE_MIN_ELAPSED < SSO_WAIT_TIMEOUT_DEFAULT
    sig = inspect.signature(wait_for_sso_cookie)
    assert sig.parameters["timeout"].default is None


def test_browser_signup_calls_wait_for_sso_cookie():
    ttk = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    flow = (ROOT / "register_flow.py").read_text(encoding="utf-8")
    assert "sso = wait_for_sso_cookie(" in ttk
    assert "def wait_for_sso_cookie(" in flow
    assert "def sso_wait_seconds(" in flow


def test_sso_nudge_cannot_fire_before_accounts_hold(monkeypatch):
    """grok.com nudge must stay behind the accounts.x.ai hold window."""
    monkeypatch.delenv("SSO_WAIT_TIMEOUT", raising=False)
    assert SSO_GROK_NUDGE_MIN_ELAPSED > SSO_ACCOUNTS_HOLD_SECONDS
    assert sso_wait_seconds() > SSO_GROK_NUDGE_MIN_ELAPSED
    assert sso_wait_seconds() > SSO_ACCOUNTS_HOLD_SECONDS
    source = inspect.getsource(wait_for_sso_cookie)
    assert "accounts_hold_seconds = SSO_ACCOUNTS_HOLD_SECONDS" in source
    assert "grok_nudge_min_elapsed = SSO_GROK_NUDGE_MIN_ELAPSED" in source


def test_empty_scheme_selects_browser_wait_for_sso(monkeypatch):
    from signup_scheme import SCHEME_BROWSER, load_registration_scheme

    monkeypatch.delenv("GROK_REGISTRATION_SCHEME", raising=False)
    monkeypatch.delenv("REGISTRATION_SCHEME", raising=False)
    assert load_registration_scheme(env={}, config={}, control={}) == SCHEME_BROWSER
    assert (
        load_registration_scheme(env={}, config={"registration_scheme": ""}, control={})
        == SCHEME_BROWSER
    )
    ttk = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    browser_fn = ttk.split("def run_browser_signup(", 1)[1].split("def signup_until_sso(", 1)[0]
    assert "sso = wait_for_sso_cookie(" in browser_fn
    assert "fill_profile_and_submit(" in browser_fn
    assert "in-page fetch POST create-account" not in browser_fn
    assert "protocol sso ok" not in browser_fn
