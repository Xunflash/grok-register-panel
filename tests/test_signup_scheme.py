# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signup_scheme import (  # noqa: E402
    ENV_NAME,
    SCHEME_BROWSER,
    SCHEME_PROTOCOL,
    load_registration_scheme,
    maybe_run_protocol_signup,
    persist_registration_scheme,
    resolve_registration_scheme,
)


def test_resolve_registration_scheme_aliases():
    assert resolve_registration_scheme("browser") == SCHEME_BROWSER
    assert resolve_registration_scheme("Camoufox") == SCHEME_BROWSER
    assert resolve_registration_scheme("浏览器注册") == SCHEME_BROWSER
    assert resolve_registration_scheme("protocol") == SCHEME_PROTOCOL
    assert resolve_registration_scheme("协议") == SCHEME_PROTOCOL
    assert resolve_registration_scheme("HAR") == SCHEME_PROTOCOL
    assert resolve_registration_scheme("") == SCHEME_BROWSER
    assert resolve_registration_scheme("nope") == SCHEME_BROWSER
    assert resolve_registration_scheme("nope", default=SCHEME_PROTOCOL) == SCHEME_PROTOCOL


def test_load_prefers_env_then_control_then_config(monkeypatch):
    monkeypatch.delenv(ENV_NAME, raising=False)
    monkeypatch.delenv("REGISTRATION_SCHEME", raising=False)
    assert (
        load_registration_scheme(
            env={},
            control={"registration_scheme": "protocol"},
            config={"registration_scheme": "browser"},
        )
        == SCHEME_PROTOCOL
    )
    monkeypatch.setenv(ENV_NAME, "browser")
    assert (
        load_registration_scheme(
            control={"registration_scheme": "protocol"},
            config={"registration_scheme": "protocol"},
        )
        == SCHEME_BROWSER
    )
    monkeypatch.delenv(ENV_NAME, raising=False)
    assert (
        load_registration_scheme(
            env={},
            control={},
            config={"registration_scheme": "协议注册"},
        )
        == SCHEME_PROTOCOL
    )
    assert load_registration_scheme(env={}, control={}, config={}) == SCHEME_BROWSER


def test_persist_registration_scheme_keeps_other_keys(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"proxy": "http://127.0.0.1:9", "turnstile_provider": "local"}),
        encoding="utf-8",
    )
    resolved = persist_registration_scheme("协议", cfg)
    assert resolved == SCHEME_PROTOCOL
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["registration_scheme"] == SCHEME_PROTOCOL
    assert data["proxy"] == "http://127.0.0.1:9"
    assert data["turnstile_provider"] == "local"


def test_maybe_run_protocol_returns_none_for_browser(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "browser")
    called = []

    def boom(*_args, **_kwargs):
        called.append("protocol")
        raise AssertionError("protocol runner must not run on browser scheme")

    monkeypatch.setattr("signup_scheme._load_protocol_signup", boom)
    monkeypatch.setattr("signup_scheme._prepare_protocol_browser", boom)
    out = maybe_run_protocol_signup(lambda _m: None, lambda: False)
    assert out is None
    assert called == []


def test_maybe_run_protocol_drives_shipped_loader(monkeypatch):
    monkeypatch.setenv(ENV_NAME, "protocol")
    events = []

    def fake_prepare(log_fn):
        events.append("prepare")
        log_fn("prepared")

    def fake_close(log_fn):
        events.append("close")
        log_fn("closed")

    def fake_run(log_callback=None, cancel_callback=None):
        events.append("run")
        assert cancel_callback() is False
        log_callback("ran")
        return {
            "email": "scheme-test@example.com",
            "profile": {"password": "pw", "given_name": "A", "family_name": "B"},
            "sso": "sso-token",
        }

    monkeypatch.setattr("signup_scheme._prepare_protocol_browser", fake_prepare)
    monkeypatch.setattr("signup_scheme._close_protocol_browser", fake_close)
    monkeypatch.setattr("signup_scheme._load_protocol_signup", lambda: fake_run)
    logs = []
    out = maybe_run_protocol_signup(logs.append, lambda: False)
    assert events == ["prepare", "run", "close"]
    assert out["scheme"] == SCHEME_PROTOCOL
    assert out["email"] == "scheme-test@example.com"
    assert out["sso"] == "sso-token"
    assert any("协议注册" in line for line in logs)


def test_loader_source_imports_shipped_protocol_entry():
    text = (ROOT / "signup_scheme.py").read_text(encoding="utf-8")
    assert "from protocol_register_once import run_protocol_signup" in text
    proto = (ROOT / "protocol_register_once.py").read_text(encoding="utf-8")
    assert "def run_protocol_signup(" in proto
