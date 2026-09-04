# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import grok_register_ttk as register


def test_unknown_bfs_is_queued_when_skip_enabled():
    config_keys = (
        "cpa_auto_add",
        "cpa_auth_dir",
        "cpa_remote_url",
        "cpa_management_key",
        "grok2api_auth_dir",
        "bfs_check",
        "bfs_skip_cpa",
        "cpa_token_mode",
    )
    previous_config = {key: register.config.get(key) for key in config_keys}
    previous_functions = (
        register._resolve_cpa_proxy,
        register._s2cpa.sso_to_token,
        register._s2cpa.inspect_token_bundle_bfs,
        register._s2cpa.token_to_cpa_record,
        register._append_sso_pending,
    )
    queued = []
    record_calls = []
    with tempfile.TemporaryDirectory() as temp:
        register.config.update(
            {
                "cpa_auto_add": True,
                "cpa_auth_dir": str(Path(temp) / "cpa_auth"),
                "cpa_remote_url": "",
                "cpa_management_key": "",
                "grok2api_auth_dir": "",
                "bfs_check": True,
                "bfs_skip_cpa": True,
                "cpa_token_mode": "device_protocol",
            }
        )
        register._resolve_cpa_proxy = lambda: ""
        register._s2cpa.sso_to_token = lambda *_args, **_kwargs: {
            "access_token": "opaque-access-token",
            "refresh_token": "opaque-refresh-token",
        }
        register._s2cpa.inspect_token_bundle_bfs = lambda **_kwargs: {
            "ok": False,
            "has_bfs": False,
            "bfs": None,
            "source": "",
        }
        register._s2cpa.token_to_cpa_record = (
            lambda *_args, **kwargs: record_calls.append(kwargs) or {}
        )
        register._append_sso_pending = (
            lambda email, sso, **_kwargs: queued.append((email, sso))
        )
        try:
            result = register.add_sso_to_cpa(
                "sso=test-sso-token",
                email="unknown@example.test",
            )
        finally:
            (
                register._resolve_cpa_proxy,
                register._s2cpa.sso_to_token,
                register._s2cpa.inspect_token_bundle_bfs,
                register._s2cpa.token_to_cpa_record,
                register._append_sso_pending,
            ) = previous_functions
            for key, value in previous_config.items():
                if value is None:
                    register.config.pop(key, None)
                else:
                    register.config[key] = value

    assert result is False
    assert queued == [("unknown@example.test", "test-sso-token")]
    assert record_calls == []


def test_bfs_check_off_skips_jwt_inspect():
    config_keys = (
        "cpa_auto_add",
        "cpa_auth_dir",
        "cpa_remote_url",
        "cpa_management_key",
        "grok2api_auth_dir",
        "bfs_check",
        "cpa_token_mode",
        "grok2api_auto_import",
    )
    previous_config = {key: register.config.get(key) for key in config_keys}
    previous_functions = (
        register._resolve_cpa_proxy,
        register._s2cpa.sso_to_token,
        register._s2cpa.inspect_token_bundle_bfs,
        register._s2cpa.token_to_cpa_record,
        register._s2cpa.write_cpa_auth,
        register._s2cpa.decode_jwt_payload,
        register._guard_register_check,
        register._apply_guard_register_result,
    )
    inspected = []
    record_kwargs = []
    with tempfile.TemporaryDirectory() as temp:
        register.config.update(
            {
                "cpa_auto_add": True,
                "cpa_auth_dir": str(Path(temp) / "cpa_auth"),
                "cpa_remote_url": "",
                "cpa_management_key": "",
                "grok2api_auth_dir": "",
                "bfs_check": False,
                "cpa_token_mode": "device_protocol",
                "grok2api_auto_import": False,
            }
        )
        register._resolve_cpa_proxy = lambda: ""
        register._s2cpa.sso_to_token = lambda *_args, **_kwargs: {
            "access_token": "opaque-access-token",
            "refresh_token": "opaque-refresh-token",
        }
        register._s2cpa.inspect_token_bundle_bfs = (
            lambda **_kwargs: inspected.append(True) or {"ok": True, "has_bfs": True}
        )
        register._s2cpa.token_to_cpa_record = (
            lambda *_args, **kwargs: record_kwargs.append(kwargs)
            or {"access_token": "opaque-access-token"}
        )
        register._s2cpa.write_cpa_auth = lambda *_args, **_kwargs: Path(temp) / "written.json"
        register._s2cpa.decode_jwt_payload = lambda *_args, **_kwargs: {}
        register._guard_register_check = lambda *_args, **_kwargs: {}
        register._apply_guard_register_result = lambda *_args, **_kwargs: None
        try:
            result = register.add_sso_to_cpa("sso=test-sso-token", email="off@example.test")
        finally:
            (
                register._resolve_cpa_proxy,
                register._s2cpa.sso_to_token,
                register._s2cpa.inspect_token_bundle_bfs,
                register._s2cpa.token_to_cpa_record,
                register._s2cpa.write_cpa_auth,
                register._s2cpa.decode_jwt_payload,
                register._guard_register_check,
                register._apply_guard_register_result,
            ) = previous_functions
            for key, value in previous_config.items():
                if value is None:
                    register.config.pop(key, None)
                else:
                    register.config[key] = value
    assert result is True
    assert inspected == []
    assert record_kwargs and record_kwargs[0].get("check_bfs") is False


if __name__ == "__main__":
    test_unknown_bfs_is_queued_when_skip_enabled()
    test_bfs_check_off_skips_jwt_inspect()
    print("OK bfs worker integration")
