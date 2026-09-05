# -*- coding: utf-8 -*-
"""Protocol replay must follow the HAR canonical chain without invented bodies."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    FETCH_SETTABLE_HEADERS,
    build_create_account,
    fetch_settable_headers,
    protocol_replay_chain,
)
from signup_scheme import ENV_NAME, SCHEME_BROWSER, SCHEME_PROTOCOL  # noqa: E402
from webui import monitor  # noqa: E402
import protocol_register_once as proto  # noqa: E402
import grok_register_ttk as ttk  # noqa: E402


def test_protocol_replay_json_keys_include_create_castle():
    replay = protocol_replay_chain()
    by_path = {step["path"]: step for step in replay["auth_steps"]}
    assert set(by_path[AUTH_SEND_CODE]["json_keys"]) == {"email", "castleRequestToken"}
    assert set(by_path[AUTH_VERIFY_EMAIL]["json_keys"]) == {"email", "code"}
    create_keys = set(by_path[AUTH_CREATE_ACCOUNT]["json_keys"])
    assert {
        "email",
        "password",
        "givenName",
        "familyName",
        "emailValidationCode",
        "turnstileToken",
        "castleRequestToken",
    } <= create_keys
    assert replay["companion_classes"] == []
    assert replay["companion_wait"] is True


def test_fetch_settable_headers_are_har_subset_not_invented_ua():
    har_names = [
        "Accept",
        "Content-Type",
        "sentry-trace",
        "baggage",
        "User-Agent",
        "Origin",
        "Referer",
    ]
    live = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "sentry-trace": "live-st",
        "baggage": "live-bag",
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://accounts.x.ai",
        "invented": "nope",
    }
    out = fetch_settable_headers(har_names, live)
    assert set(k.lower() for k in out) <= FETCH_SETTABLE_HEADERS
    assert "invented" not in out
    assert "User-Agent" not in out
    assert "Origin" not in out


def test_run_protocol_signup_mints_castle_for_create_account():
    source = Path(proto.__file__).read_text(encoding="utf-8")
    assert "mint_castle_from_page_sdk" in source
    assert "castle_request_token=create_castle_token" in source or "castle_request_token=create_castle" in source
    built = build_create_account(
        email="a@b.c",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="123456",
        turnstile_token="ts",
        castle_request_token="castle-live",
    )
    assert built["json"]["castleRequestToken"] == "castle-live"
    assert built["path"] == AUTH_CREATE_ACCOUNT


def test_both_schemes_share_signup_until_sso_and_outlook_settle():
    ttk_src = Path(ttk.__file__).read_text(encoding="utf-8")
    assert "def signup_until_sso(" in ttk_src
    assert "outlook_rt_settle_attempt(" in ttk_src
    assert "maybe_run_protocol_signup" in ttk_src
    proto_src = Path(proto.__file__).read_text(encoding="utf-8")
    assert "def run_protocol_signup(" in proto_src
    assert "retry_protocol_pre_inbox" in proto_src
    assert "recycle_protocol_browser" in proto_src
    assert "ReusableResourceError" in proto_src


def test_registration_env_disables_fp_enhance_only_for_protocol(tmp_path, monkeypatch):
    control_file = tmp_path / "monitor_control.json"
    cfg = tmp_path / "config.json"
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(monitor, "CONTROL_FILE", control_file)
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.setattr(monitor, "LOG_DIR", log_dir)
    monkeypatch.delenv(ENV_NAME, raising=False)
    monkeypatch.delenv("GROK_FP_ENHANCE", raising=False)
    monitor.save_control({"registration_scheme": "protocol"})
    env_p = monitor._registration_env()
    assert env_p[ENV_NAME] == SCHEME_PROTOCOL
    assert env_p.get("GROK_FP_ENHANCE") == "0"
    monitor.save_control({"registration_scheme": "browser"})
    env_b = monitor._registration_env()
    assert env_b[ENV_NAME] == SCHEME_BROWSER
    assert env_b.get("GROK_FP_ENHANCE") != "0"
