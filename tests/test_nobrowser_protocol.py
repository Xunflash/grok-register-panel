# -*- coding: utf-8 -*-
"""HAR auth trio + no-Camoufox adapters. Drives shipped builders, not copies."""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cffi_har_signup"))

from cffi_headers import assert_no_grpc_signup_paths, har_auth_name_contract, request_json_keys
from signup_protocol import (
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    SIGNUP_PATHS,
    assert_send_code_has_castle,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    json_keys_for_path,
    load_har,
)

FIXTURE = ROOT / "cffi_har_signup" / "tests" / "fixtures" / "signup_har_excerpt.json"
if not FIXTURE.is_file():
    FIXTURE = ROOT / "tests" / "fixtures" / "signup_har_excerpt.json"


def test_shipped_builders_match_har_auth_name_contract():
    har = load_har(FIXTURE)
    names = har_auth_name_contract(har)
    send = build_send_verification_code(email="a@b.c", castle_request_token="live-castle")
    verify = build_verify_email(email="a@b.c", code="123-456")
    create = build_create_account(
        email="a@b.c",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="123-456",
        turnstile_token="ts",
        castle_request_token="live-castle",
    )
    assert_send_code_has_castle(send)
    assert_no_grpc_signup_paths([send["url"], verify["url"], create["url"]])
    assert set(request_json_keys(send)) == {"castleRequestToken", "email"}
    assert set(request_json_keys(verify)) == {"code", "email"}
    assert set(request_json_keys(create)) == {
        "email",
        "emailValidationCode",
        "familyName",
        "givenName",
        "password",
        "turnstileToken",
        "castleRequestToken",
    }
    assert set(names["calls"][AUTH_SEND_CODE]["json_keys"]) == {"email", "castleRequestToken"}
    assert set(names["calls"][AUTH_VERIFY_EMAIL]["json_keys"]) == {"code", "email"}
    har_create = set(json_keys_for_path(har, AUTH_CREATE_ACCOUNT) or [])
    assert {
        "email",
        "password",
        "givenName",
        "familyName",
        "emailValidationCode",
        "turnstileToken",
    } <= har_create
    extra = set(request_json_keys(create)) - har_create
    assert extra <= {"castleRequestToken"}
    assert send["path"] == AUTH_SEND_CODE
    assert verify["path"] == AUTH_VERIFY_EMAIL
    assert create["path"] == AUTH_CREATE_ACCOUNT
    blob = json.dumps({"send": send, "verify": verify, "create": create})
    assert "CreateEmailValidationCode" not in blob
    assert "CreateUserAndSessionV2" not in blob


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def test_no_camoufox_on_cffi_or_playwright_engines():
    cffi = ROOT / "nobrowser_protocol" / "cffi_engine.py"
    pw = ROOT / "nobrowser_protocol" / "playwright_engine.py"
    run = ROOT / "nobrowser_protocol" / "run_live.py"
    for path in (cffi, pw, run):
        text = path.read_text(encoding="utf-8")
        assert "camoufox_adapter" not in text
        assert "from camoufox" not in text
        assert "import camoufox" not in text
        assert "start_browser" not in text
        imported = _imported_names(path)
        assert "camoufox_adapter" not in imported
        assert "camoufox" not in imported
        assert "browser_session" not in imported
    pw_text = pw.read_text(encoding="utf-8")
    assert "playwright.chromium.launch" in pw_text
    assert "camoufox" not in pw_text.lower() or "camoufox=no" in pw_text
    from nobrowser_protocol.cffi_engine import camoufox_imports
    from nobrowser_protocol.playwright_engine import camoufox_imports as pw_camoufox

    assert camoufox_imports() == ()
    assert pw_camoufox() == ()


if __name__ == "__main__":
    test_shipped_builders_match_har_auth_name_contract()
    test_no_camoufox_on_cffi_or_playwright_engines()
    print("OK nobrowser protocol")
