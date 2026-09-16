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
    SCHEME_PROTOCOL_PATCHRIGHT,
)


def test_monitor_html_exposes_registration_scheme_select():
    mon = (ROOT / "webui/monitor.py").read_text(encoding="utf-8")
    html = mon.split('HTML = r"""', 1)[1].split('"""', 1)[0]
    assert 'id="registration_scheme"' in html
    assert 'value="browser">浏览器注册</option>' in html
    assert 'value="protocol">协议注册</option>' in html
    assert 'value="protocol_patchright">协议注册（patchright）</option>' in html
    assert "registration_scheme:" in mon
    assert "fillControl" in mon
    assert 'document.getElementById("registration_scheme")' in mon


def test_save_control_and_registration_env_use_panel_scheme(tmp_path, monkeypatch):
    from webui import monitor

    control_file = tmp_path / "monitor_control.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"turnstile_provider": "yescaptcha", "proxy": "http://127.0.0.1:9"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "CONTROL_FILE", control_file)
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.delenv(ENV_NAME, raising=False)

    saved = monitor.save_control({"registration_scheme": "协议注册", "workers": 2})
    assert saved["registration_scheme"] == SCHEME_PROTOCOL
    disk = json.loads(control_file.read_text(encoding="utf-8"))
    assert disk["registration_scheme"] == SCHEME_PROTOCOL
    cfg_data = json.loads(cfg.read_text(encoding="utf-8"))
    assert cfg_data["registration_scheme"] == SCHEME_PROTOCOL
    assert cfg_data["proxy"] == "http://127.0.0.1:9"

    env = monitor._registration_env()
    assert env[ENV_NAME] == SCHEME_PROTOCOL
    assert env.get("GROK_FP_ENHANCE") == "0"

    saved_browser = monitor.save_control({"registration_scheme": "browser"})
    assert saved_browser["registration_scheme"] == SCHEME_BROWSER
    env_browser = monitor._registration_env()
    assert env_browser[ENV_NAME] == SCHEME_BROWSER

    saved_pr = monitor.save_control({"registration_scheme": "协议注册（patchright）"})
    assert saved_pr["registration_scheme"] == SCHEME_PROTOCOL_PATCHRIGHT
    env_pr = monitor._registration_env()
    assert env_pr[ENV_NAME] == SCHEME_PROTOCOL_PATCHRIGHT
    assert env_pr.get("GROK_FP_ENHANCE") == "0"
    assert env_pr.get("PATCHRIGHT_HEADED") == "1"


def test_signup_until_sso_reads_config_when_env_missing(monkeypatch):
    import grok_register_ttk as register

    monkeypatch.delenv(ENV_NAME, raising=False)
    previous = register.config.get("registration_scheme")
    register.config["registration_scheme"] = SCHEME_PROTOCOL
    calls = []

    def fake_protocol(**_kwargs):
        calls.append(os.environ.get(ENV_NAME))
        return {
            "email": "from-config@example.com",
            "profile": {"password": "pw"},
            "sso": "sso",
            "scheme": SCHEME_PROTOCOL,
        }

    monkeypatch.setattr(register, "maybe_run_protocol_signup", fake_protocol)
    monkeypatch.setattr(
        register,
        "run_browser_signup",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("browser path")),
    )
    try:
        out = register.signup_until_sso(lambda _m: None, lambda: False)
        assert calls == [SCHEME_PROTOCOL]
        assert out["email"] == "from-config@example.com"
    finally:
        if previous is None:
            register.config.pop("registration_scheme", None)
        else:
            register.config["registration_scheme"] = previous
        monkeypatch.delenv(ENV_NAME, raising=False)


def test_signup_until_sso_protocol_skips_browser_path(monkeypatch):
    import grok_register_ttk as register

    calls = []

    def fake_protocol(**_kwargs):
        calls.append("protocol")
        return {
            "email": "proto@example.com",
            "profile": {"password": "secret", "given_name": "P", "family_name": "Q"},
            "sso": "proto-sso",
            "scheme": SCHEME_PROTOCOL,
        }

    def fake_browser(*_args, **_kwargs):
        calls.append("browser")
        raise AssertionError("browser signup must not run when protocol returns an outcome")

    monkeypatch.setattr(register, "maybe_run_protocol_signup", fake_protocol)
    monkeypatch.setattr(register, "run_browser_signup", fake_browser)
    out = register.signup_until_sso(lambda _m: None, lambda: False)
    assert calls == ["protocol"]
    assert out["email"] == "proto@example.com"
    assert out["sso"] == "proto-sso"
    assert out["scheme"] == SCHEME_PROTOCOL


def test_signup_until_sso_browser_when_protocol_returns_none(monkeypatch):
    import grok_register_ttk as register

    calls = []

    def fake_protocol(**_kwargs):
        calls.append("protocol-check")
        return None

    def fake_browser(*_args, **_kwargs):
        calls.append("browser")
        return {
            "email": "browser@example.com",
            "profile": {"password": "pw"},
            "sso": "browser-sso",
            "scheme": SCHEME_BROWSER,
        }

    monkeypatch.setattr(register, "maybe_run_protocol_signup", fake_protocol)
    monkeypatch.setattr(register, "run_browser_signup", fake_browser)
    logs = []
    out = register.signup_until_sso(logs.append, lambda: False, mail_retries=3)
    assert calls == ["protocol-check", "browser"]
    assert out["email"] == "browser@example.com"
    assert out["scheme"] == SCHEME_BROWSER
    assert any("浏览器注册" in line for line in logs)


def test_workers_call_signup_until_sso():
    text = (ROOT / "grok_register_ttk.py").read_text(encoding="utf-8")
    assert "def signup_until_sso(" in text
    assert "def run_browser_signup(" in text
    assert text.count("signup_until_sso(") >= 3
    assert "registration_scheme_var" in text
    assert 'SCHEME_PROTOCOL' in text or '"protocol"' in text
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert example["registration_scheme"] in {
        SCHEME_BROWSER,
        SCHEME_PROTOCOL,
        SCHEME_PROTOCOL_PATCHRIGHT,
    }
    assert "SCHEME_PROTOCOL_PATCHRIGHT" in text or "protocol_patchright" in text
    assert '"registration_scheme": "browser"' in text


if __name__ == "__main__":
    test_monitor_html_exposes_registration_scheme_select()
    test_workers_call_signup_until_sso()
    print("OK registration scheme panel")
