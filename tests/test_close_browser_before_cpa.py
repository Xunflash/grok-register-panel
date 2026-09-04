# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grok_register_ttk as register  # noqa: E402
import protocol_register_once as proto  # noqa: E402


def test_release_browser_before_cpa_closes_unless_device_browser():
    previous = {
        "cpa_token_mode": register.config.get("cpa_token_mode"),
        "stop_browser": register.stop_browser,
        "_active_page": register._active_page,
    }
    closed = []
    register.stop_browser = lambda force=False: closed.append(force)
    register._active_page = lambda: object()
    logs = []
    try:
        register.config["cpa_token_mode"] = "device_protocol"
        assert register.release_browser_before_cpa(log_callback=logs.append) is True
        assert closed == [True]
        closed.clear()
        register.config["cpa_token_mode"] = "device_browser"
        assert register.release_browser_before_cpa(log_callback=logs.append) is False
        assert closed == []
        register._active_page = lambda: None
        assert register.release_browser_before_cpa() is True
        assert closed == [True]
    finally:
        register.stop_browser = previous["stop_browser"]
        register._active_page = previous["_active_page"]
        if previous["cpa_token_mode"] is None:
            register.config.pop("cpa_token_mode", None)
        else:
            register.config["cpa_token_mode"] = previous["cpa_token_mode"]


def test_gui_cli_close_browser_before_add_sso_to_cpa():
    ttk_src = Path(register.__file__).read_text(encoding="utf-8")
    proto_src = Path(proto.__file__).read_text(encoding="utf-8")
    assert ttk_src.count("release_browser_before_cpa(") >= 3
    assert "add_sso_to_cpa(" in ttk_src
    gui = ttk_src.find("release_browser_before_cpa(log_callback=wlog)")
    gui_cpa = ttk_src.find("cpa_ok = add_sso_to_cpa(sso, email=email, log_callback=wlog)")
    assert gui != -1 and gui_cpa != -1 and gui < gui_cpa
    assert "release_browser_before_cpa(log)" in proto_src
    assert proto_src.find("release_browser_before_cpa(log)") < proto_src.find(
        "cpa_ok, guard_name, cpa_error = _cpa_and_guard"
    )


def test_wait_path_does_not_hold_browser_for_device_protocol():
    previous = {
        "cpa_token_mode": register.config.get("cpa_token_mode"),
        "stop_browser": register.stop_browser,
        "_active_page": register._active_page,
        "active": object(),
    }
    session = {"page": previous["active"]}
    register._active_page = lambda: session.get("page")
    register.stop_browser = lambda force=False: session.pop("page", None)
    register.config["cpa_token_mode"] = "device_protocol"
    try:
        register.release_browser_before_cpa()
        assert session.get("page") is None
        assert register._active_page() is None
    finally:
        register.stop_browser = previous["stop_browser"]
        register._active_page = previous["_active_page"]
        if previous["cpa_token_mode"] is None:
            register.config.pop("cpa_token_mode", None)
        else:
            register.config["cpa_token_mode"] = previous["cpa_token_mode"]


if __name__ == "__main__":
    test_release_browser_before_cpa_closes_unless_device_browser()
    test_gui_cli_close_browser_before_add_sso_to_cpa()
    test_wait_path_does_not_hold_browser_for_device_protocol()
    print("OK close browser before CPA")
