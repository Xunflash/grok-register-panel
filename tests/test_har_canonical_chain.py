# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    SIGNUP_PATHS,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    canonical_signup_chain,
    companion_classes_from_har,
    diff_canonical_chain_vs_protocol,
    extract_signup_calls,
    protocol_replay_chain,
)
from signup_scheme import ENV_NAME, SCHEME_BROWSER, SCHEME_PROTOCOL  # noqa: E402
from webui import monitor  # noqa: E402
import browser_session  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "signup_har_excerpt.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _drop_path(har: dict, path: str) -> dict:
    cloned = copy.deepcopy(har)
    log = cloned.get("log") or cloned
    entries = log.get("entries") or []
    kept = []
    for entry in entries:
        url = str(((entry.get("request") or {}).get("url") or ""))
        if path in url:
            continue
        kept.append(entry)
    log["entries"] = kept
    return cloned


def _add_cookie(har: dict, path: str, name: str, value: str) -> dict:
    cloned = copy.deepcopy(har)
    log = cloned.get("log") or cloned
    for entry in log.get("entries") or []:
        req = entry.get("request") or {}
        url = str(req.get("url") or "")
        if path not in url:
            continue
        cookies = list(req.get("cookies") or [])
        cookies.append({"name": name, "value": value})
        req["cookies"] = cookies
        entry["request"] = req
    return cloned


def test_canonical_chain_from_three_har_shaped_fixtures():
    a = _load_fixture()
    b = _add_cookie(a, AUTH_CREATE_ACCOUNT, "gb_anon_id", "b-only")
    c = _add_cookie(a, AUTH_SEND_CODE, "xai_anon_id", "c-only")
    chain = canonical_signup_chain([a, b, c])
    paths = [step["path"] for step in chain["auth_steps"]]
    assert paths == list(SIGNUP_PATHS)
    assert paths == [
        AUTH_SEND_CODE,
        AUTH_VERIFY_EMAIL,
        AUTH_CREATE_ACCOUNT,
    ]
    assert chain["har_count"] == 3
    assert all(step["present_in"] == 3 for step in chain["auth_steps"])
    send = chain["auth_steps"][0]
    create = chain["auth_steps"][2]
    assert "email" in send["json_keys"]
    assert "castleRequestToken" in send["json_keys"]
    assert "turnstileToken" in create["json_keys"]
    assert "gb_anon_id" in create["cookie_names"]
    assert "xai_anon_id" in send["cookie_names"]
    assert set(chain["companion_classes"]) >= {
        "mp_track",
        "cf_jsd",
        "cf_rum",
        "observability",
    }
    assert set(companion_classes_from_har(a)) <= set(chain["companion_classes"])


def test_protocol_replay_chain_uses_shipped_builders():
    replay = protocol_replay_chain()
    send = build_send_verification_code(
        email="fixture@example.com", castle_request_token="castle-fixture"
    )
    verify = build_verify_email(email="fixture@example.com", code="123456")
    create = build_create_account(
        email="fixture@example.com",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="123456",
        turnstile_token="ts",
        castle_request_token="castle-fixture",
    )
    by_path = {step["path"]: step for step in replay["auth_steps"]}
    assert by_path[AUTH_SEND_CODE]["json_keys"] == sorted(send["json"])
    assert by_path[AUTH_VERIFY_EMAIL]["json_keys"] == sorted(verify["json"])
    assert by_path[AUTH_CREATE_ACCOUNT]["json_keys"] == sorted(create["json"])
    assert replay["companion_classes"] == []
    assert replay["replay_paths"] == list(SIGNUP_PATHS)


def test_diff_marks_missing_create_account_and_keeps_other_matches():
    stripped = _drop_path(_load_fixture(), AUTH_CREATE_ACCOUNT)
    chain = canonical_signup_chain([stripped, stripped, stripped])
    create_step = [s for s in chain["auth_steps"] if s["path"] == AUTH_CREATE_ACCOUNT][0]
    assert create_step["present_in"] == 0
    rows = diff_canonical_chain_vs_protocol(chain)
    by_step = {row["step"]: row for row in rows}
    assert by_step[AUTH_CREATE_ACCOUNT]["kind"] == "missing"
    assert by_step[AUTH_SEND_CODE]["kind"] in {"match", "value-drift"}
    assert by_step[AUTH_VERIFY_EMAIL]["kind"] in {"match", "value-drift"}
    for row in rows:
        if str(row["step"]).startswith("companion:"):
            assert row["kind"] in {"match", "missing"}
            assert "invent Mixpanel/Castle/jsd bodies" in row["evidence"]


def test_diff_full_fixture_vs_protocol_builders_no_invented_companion_posts():
    chain = canonical_signup_chain([_load_fixture(), _load_fixture(), _load_fixture()])
    rows = diff_canonical_chain_vs_protocol(chain)
    kinds = {row["step"]: row["kind"] for row in rows}
    assert kinds[AUTH_SEND_CODE] == "match"
    assert kinds[AUTH_VERIFY_EMAIL] == "match"
    # HAR create-account capture omits castleRequestToken; the shipped builder
    # adds it from the live page SDK. That is value-drift, not a new companion body.
    assert kinds[AUTH_CREATE_ACCOUNT] in {"match", "value-drift"}
    companion_rows = [row for row in rows if str(row["step"]).startswith("companion:")]
    assert companion_rows
    assert all(row["kind"] == "match" for row in companion_rows)
    replay = protocol_replay_chain()
    assert replay["companion_classes"] == []


def test_save_control_dispatches_browser_vs_protocol_and_records_har_dir(
    tmp_path, monkeypatch
):
    control_file = tmp_path / "monitor_control.json"
    cfg = tmp_path / "config.json"
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    cfg.write_text(
        json.dumps({"turnstile_provider": "capsolver", "proxy": "http://127.0.0.1:9"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(monitor, "CONTROL_FILE", control_file)
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    monkeypatch.setattr(monitor, "LOG_DIR", log_dir)
    monkeypatch.delenv(ENV_NAME, raising=False)
    monkeypatch.delenv("GROK_RECORD_HAR_DIR", raising=False)
    monkeypatch.delenv("GROK_RECORD_HAR", raising=False)

    saved_protocol = monitor.save_control({"registration_scheme": "协议注册", "workers": 2})
    assert saved_protocol["registration_scheme"] == SCHEME_PROTOCOL
    env_protocol = monitor._registration_env()
    assert env_protocol[ENV_NAME] == SCHEME_PROTOCOL
    assert env_protocol.get("GROK_FP_ENHANCE") == "0"
    assert "GROK_RECORD_HAR_DIR" not in env_protocol

    saved_browser = monitor.save_control({"registration_scheme": "浏览器注册"})
    assert saved_browser["registration_scheme"] == SCHEME_BROWSER
    env_browser = monitor._registration_env()
    assert env_browser[ENV_NAME] == SCHEME_BROWSER
    assert "GROK_RECORD_HAR_DIR" not in env_browser
    assert env_browser.get("GROK_DEBUG_MODE") in (None, "")

    cfg.write_text(
        json.dumps({"debug_mode": True, "turnstile_provider": "capsolver"}),
        encoding="utf-8",
    )
    env_debug = monitor._registration_env()
    assert env_debug.get("GROK_DEBUG_MODE") == "1"
    assert env_debug.get("GROK_RECORD_HAR_DIR") == str(log_dir / "browser-hars")
    assert (log_dir / "browser-hars").is_dir()


def test_resolve_record_har_path_unique_under_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_RECORD_HAR", raising=False)
    monkeypatch.setenv("GROK_DEBUG_MODE", "1")
    monkeypatch.setenv("GROK_RECORD_HAR_DIR", str(tmp_path))
    first = browser_session.resolve_record_har_path()
    second = browser_session.resolve_record_har_path()
    assert first != second
    assert first.endswith(".har")
    assert Path(first).parent == tmp_path
    assert browser_session.current_record_har_path() == second
    monkeypatch.delenv("GROK_RECORD_HAR_DIR", raising=False)
    explicit = tmp_path / "one.har"
    monkeypatch.setenv("GROK_RECORD_HAR", str(explicit))
    got = browser_session.resolve_record_har_path()
    assert got == str(explicit)


def test_resolve_record_har_path_off_without_debug(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_RECORD_HAR", raising=False)
    monkeypatch.setenv("GROK_DEBUG_MODE", "0")
    monkeypatch.setenv("GROK_RECORD_HAR_DIR", str(tmp_path))
    assert browser_session.resolve_record_har_path() == ""
    monkeypatch.delenv("GROK_DEBUG_MODE", raising=False)
    monkeypatch.setattr(browser_session, "_is_debug", lambda: False)
    assert browser_session.resolve_record_har_path() == ""
    explicit = tmp_path / "capture.har"
    monkeypatch.setenv("GROK_RECORD_HAR", str(explicit))
    assert browser_session.resolve_record_har_path() == str(explicit)


def test_prune_record_har_dir_drops_empty_and_session_files(tmp_path):
    empty = tmp_path / "1-2-abcdef12.har"
    empty.write_bytes(b"{" + b" " * 300 + b"}")
    session = tmp_path / "9-8-deadbeef.har"
    session.write_bytes(b"x" * 2048)
    success = tmp_path / "success-user@example.com.har"
    success.write_bytes(b"keep-success")
    named = tmp_path / "signup-protocol-full.har"
    named.write_bytes(b"keep-named")
    stats = browser_session.prune_record_har_dir(tmp_path)
    assert not empty.exists()
    assert not session.exists()
    assert success.exists()
    assert named.exists()
    assert stats["removed"] == 2
    assert stats["kept"] == 2
