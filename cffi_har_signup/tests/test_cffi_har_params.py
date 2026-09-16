#!/usr/bin/env python3
"""Drive shipped HAR extractors and curl_cffi request builders. No live tokens."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cffi_headers import (  # noqa: E402
    GRPC_FORBIDDEN,
    assert_no_grpc_signup_paths,
    har_auth_name_contract,
    headers_for_auth_path,
    request_json_keys,
    seed_har_cookie_names,
)
from param_origins import (  # noqa: E402
    HEADER_VALUE_ORIGINS,
    JSON_FIELD_ORIGINS,
    classify_cookie_origins,
    html_signup_set_cookies,
    jsd_set_cookies,
    preflight_live_origins,
    redirect_cookie_shape,
)
from grok2api_import import build_import_document, web_import_document  # noqa: E402
from grok_onboard import birth_date_payload, tos_grpc_body  # noqa: E402
from guard_check import apply_streak, classify, should_retry_guard  # noqa: E402
from outlook_min import apply_live_otp, code_from_messages, extract_code  # noqa: E402
from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    SIGNUP_PATHS,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    extract_signup_calls,
    json_keys_for_path,
)

FIXTURE = ROOT / "tests" / "fixtures" / "signup_har_excerpt.json"
CONTRACT = ROOT / "tests" / "fixtures" / "protocol_har_auth_contract.json"


def _load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_builders_follow_har_auth_paths_and_json_keys():
    har = json.loads(FIXTURE.read_text(encoding="utf-8"))
    contract = _load_contract()
    calls = extract_signup_calls(har)
    assert list(SIGNUP_PATHS) == contract["paths"]
    send = build_send_verification_code(
        email="a@b.c", castle_request_token="castle-token-fixture"
    )
    verify = build_verify_email(email="a@b.c", code="123456")
    create = build_create_account(
        email="a@b.c",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="123456",
        turnstile_token="ts",
        castle_request_token="castle-token-fixture",
    )
    built = {AUTH_SEND_CODE: send, AUTH_VERIFY_EMAIL: verify, AUTH_CREATE_ACCOUNT: create}
    urls = [row["url"] for row in built.values()]
    assert_no_grpc_signup_paths(urls)
    for path in SIGNUP_PATHS:
        assert built[path]["path"] == path
        assert built[path]["url"].endswith(path)
        assert "CreateEmailValidationCode" not in built[path]["url"]
        assert "CreateUserAndSessionV2" not in built[path]["url"]
        har_keys = set(json_keys_for_path(har, path) or contract["calls"][path]["json_keys"])
        live_keys = set(request_json_keys(built[path]))
        missing = har_keys - live_keys
        assert not missing, f"{path} missing HAR json keys {sorted(missing)}"
        extra = live_keys - har_keys - {"castleRequestToken"}
        assert not extra, f"{path} invented json keys {sorted(extra)}"
        assert calls[path]["method"] == "POST"


def test_chain_is_not_grpc_console_signup():
    contract = _load_contract()
    for path in contract["forbidden_paths"]:
        assert path in GRPC_FORBIDDEN
        assert path not in SIGNUP_PATHS
    send = build_send_verification_code(email="a@b.c", castle_request_token="x")
    create = build_create_account(
        email="a@b.c",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="1",
        turnstile_token="t",
    )
    blob = json.dumps({"send": send, "create": create, "contract": har_auth_name_contract(
        json.loads(FIXTURE.read_text(encoding="utf-8"))
    )})
    for forbidden in GRPC_FORBIDDEN:
        assert forbidden not in blob


def test_har_contract_fixture_matches_excerpt_extractors():
    har = json.loads(FIXTURE.read_text(encoding="utf-8"))
    names = har_auth_name_contract(har)
    assert names["paths"] == list(SIGNUP_PATHS)
    assert set(names["calls"][AUTH_SEND_CODE]["json_keys"]) == {"email", "castleRequestToken"}
    assert set(names["calls"][AUTH_VERIFY_EMAIL]["json_keys"]) == {"email", "code"}
    create_keys = set(names["calls"][AUTH_CREATE_ACCOUNT]["json_keys"])
    assert {
        "email",
        "password",
        "givenName",
        "familyName",
        "emailValidationCode",
        "turnstileToken",
    } <= create_keys


def test_apply_live_otp_keeps_har_keys_and_hyphenated_value():
    verify = build_verify_email(email="a@b.c", code="427-599")
    assert verify["json"]["code"] == "427599"
    live = apply_live_otp(verify, "427-599", "code")
    assert set(live["json"]) == {"email", "code"}
    assert live["json"]["code"] == "427-599"
    create = build_create_account(
        email="a@b.c",
        password="pw",
        given_name="A",
        family_name="B",
        email_validation_code="427-599",
        turnstile_token="ts",
    )
    live_c = apply_live_otp(create, "427-599", "emailValidationCode")
    assert live_c["json"]["emailValidationCode"] == "427-599"
    assert "emailValidationCode" in live_c["json"]


def test_code_from_messages_ignores_otp_older_than_send():
    from datetime import datetime, timezone

    messages = [
        {
            "id": "old",
            "subject": "SpaceXAI confirmation code: 196-796",
            "bodyPreview": "Validate your email",
            "receivedDateTime": "2026-09-04T03:11:19Z",
        },
        {
            "id": "new",
            "subject": "SpaceXAI confirmation code: 427-599",
            "bodyPreview": "Validate your email",
            "receivedDateTime": "2026-09-04T03:32:50Z",
        },
    ]
    after = datetime(2026, 9, 4, 3, 32, 40, tzinfo=timezone.utc).timestamp()
    assert code_from_messages(messages, after_ts=after) == "427-599"
    assert code_from_messages(messages[:1], after_ts=after) == ""


def test_account_not_found_empty_classification_is_not_normal_and_does_not_increment_streak():
    payload = {
        "classification": "",
        "risk": False,
        "thinking_seen": False,
        "reason": "account_not_found",
        "timed_out": False,
        "error": None,
    }
    kind = classify(payload)
    assert kind != "normal"
    assert kind == "no_verdict"
    assert apply_streak(0, kind) == 0
    assert apply_streak(2, kind) == 2
    assert classify({"classification": "normal", "risk": False, "reason": "normal"}) == "normal"
    assert apply_streak(2, "normal") == 3
    assert apply_streak(2, "risk_controlled") == 0
    assert apply_streak(1, "suspicious") == 0
    assert apply_streak(3, "timeout") == 3
    assert should_retry_guard("timeout") is True
    assert should_retry_guard("no_verdict") is True
    assert should_retry_guard("normal") is False
    assert should_retry_guard("risk_controlled") is False
    # Guard often returns timed_out=true even after last_classification is set.
    delayed = {
        "classification": "risk_controlled",
        "risk": False,
        "reason": "timeout",
        "timed_out": True,
        "error": None,
    }
    assert classify(delayed) == "risk_controlled"
    assert apply_streak(2, classify(delayed)) == 0
    assert classify({"classification": "normal", "timed_out": True, "risk": False}) == "normal"


def test_web_import_document_has_email_and_sso_token():
    doc = web_import_document("a@b.c", "sso-token-fixture", user_id="user-1")
    assert doc["provider"] == "grok_web"
    acc = doc["accounts"][0]
    assert acc["email"] == "a@b.c"
    assert acc["sso_token"] == "sso-token-fixture"
    assert acc["user_id"] == "user-1"


def test_build_import_document_is_grok_build_with_email_and_access_token():
    doc = build_import_document(
        "a@b.c",
        "access-token-fixture",
        refresh_token="rt-fixture",
        user_id="user-1",
    )
    assert "accounts" in doc
    acc = doc["accounts"][0]
    assert acc["provider"] == "grok_build"
    assert acc["email"] == "a@b.c"
    assert acc["access_token"] == "access-token-fixture"
    assert acc["refresh_token"] == "rt-fixture"
    assert acc["user_id"] == "user-1"


def test_onboard_payloads_are_http_not_browser():
    payload = birth_date_payload("1995-06-15T16:00:00.000Z")
    assert payload == {"birthDate": "1995-06-15T16:00:00.000Z"}
    live = birth_date_payload()
    assert "birthDate" in live
    assert "T16:00:00.000Z" in live["birthDate"]
    body = tos_grpc_body()
    assert isinstance(body, (bytes, bytearray))
    assert body[:1] == b"\x00"
    assert len(body) >= 6


def test_extract_code_hyphenated_spacexai_not_six_digit_false_positive():
    html = (
        '<div class="per-100">ignore</div>'
        "SpaceXAI confirmation code: 427-599\n"
        "Your login code is 333333"
    )
    assert extract_code(html, subject="SpaceXAI confirmation code: 427-599") == "427-599"
    assert extract_code("random 333333 digits only", subject="Hello") == ""
    assert extract_code("QO7-TUD xAI", subject="QO7-TUD xAI") == "QO7-TUD"


def test_cookie_origins_come_from_har_set_cookie_not_guesses():
    har = json.loads(FIXTURE.read_text(encoding="utf-8"))
    origins = classify_cookie_origins(har)
    html = html_signup_set_cookies(har)
    jsd = jsd_set_cookies(har)
    assert origins["redirect"] == "html_set_cookie"
    assert origins["gb_anon_id"] == "html_set_cookie"
    assert origins["referrer"] == "html_set_cookie"
    assert origins["mp_0b4055a12491884bcb6f34a5aa2718b6_mixpanel"] == "html_set_cookie"
    assert origins["__cf_bm"] == "html_set_cookie"
    assert origins["cf_clearance"] == "cf_jsd_set_cookie"
    assert origins["__cuid"] == "not_in_set_cookie"
    assert origins["xai_anon_id"] == "not_in_set_cookie"
    assert origins["OptanonConsent"] == "not_in_set_cookie"
    assert redirect_cookie_shape(html["redirect"]) == "json_redirectDestination"
    assert "cf_clearance" in jsd
    seeded = seed_har_cookie_names({}, ["redirect", "referrer", "OptanonConsent"])
    assert redirect_cookie_shape(seeded["redirect"]) != "json_redirectDestination"
    assert seeded["referrer"] == ""
    assert "landingPath" not in seeded["OptanonConsent"]


def test_live_headers_do_not_copy_stale_har_sentry():
    har = json.loads(FIXTURE.read_text(encoding="utf-8"))
    headers = headers_for_auth_path(har, AUTH_SEND_CODE)
    names = {k.lower() for k in headers}
    assert "sentry-trace" not in names
    assert "baggage" not in names
    live = headers_for_auth_path(
        har,
        AUTH_SEND_CODE,
        extra={"sentry-trace": "live-trace", "baggage": "sentry-environment=production"},
    )
    assert live["sentry-trace"] == "live-trace"


def test_preflight_refuses_jsdom_castle_and_seeded_js_cookies():
    har = json.loads(FIXTURE.read_text(encoding="utf-8"))
    html = html_signup_set_cookies(har)
    problems = preflight_live_origins(
        har=har,
        http_cookies=html,
        castle_source="jsdom",
        sentry_source="synthetic",
    )
    blob = " ".join(problems)
    assert "castleRequestToken" in blob
    assert "sentry-trace" in blob
    assert "__cuid" in blob
    assert "xai_anon_id" in blob
    assert "OptanonConsent" in blob
    assert "not guessing" in blob
    ok = preflight_live_origins(
        har=har,
        http_cookies={
            **html,
            "cf_clearance": "from-jsd",
            "__cuid": "from-page",
            "xai_anon_id": "from-page",
            "OptanonConsent": "from-page",
        },
        castle_source="useCastle.createRequestToken",
        sentry_source="page_sdk",
    )
    assert ok == []


def test_json_and_header_origin_table_covers_har_auth_fields():
    har = json.loads(FIXTURE.read_text(encoding="utf-8"))
    names = har_auth_name_contract(har)
    for path in SIGNUP_PATHS:
        for key in names["calls"][path]["json_keys"]:
            assert key in JSON_FIELD_ORIGINS, f"missing json origin for {path} {key}"
            if key == "castleRequestToken":
                assert JSON_FIELD_ORIGINS[key] == "page_castle_sdk"
            if key == "turnstileToken":
                assert JSON_FIELD_ORIGINS[key] == "capsolver_live"
    for header in ("sentry-trace", "baggage", "User-Agent", "Cookie"):
        assert HEADER_VALUE_ORIGINS[header]
    assert HEADER_VALUE_ORIGINS["sentry-trace"] == "page_sentry_sdk"
    assert HEADER_VALUE_ORIGINS["User-Agent"] == "browser_navigator"


def test_live_path_source_does_not_seed_or_jsdom_mint():
    client = (ROOT / "cffi_client.py").read_text(encoding="utf-8")
    runner = (ROOT / "run_cffi_signup.py").read_text(encoding="utf-8")
    assert "seed_har_cookie_names" not in client
    assert "synthetic_sentry_headers" not in client
    assert "mint_castle_token" not in runner
    assert "preflight_live_origins" in runner


if __name__ == "__main__":
    test_builders_follow_har_auth_paths_and_json_keys()
    test_chain_is_not_grpc_console_signup()
    test_har_contract_fixture_matches_excerpt_extractors()
    test_apply_live_otp_keeps_har_keys_and_hyphenated_value()
    test_code_from_messages_ignores_otp_older_than_send()
    test_account_not_found_empty_classification_is_not_normal_and_does_not_increment_streak()
    test_web_import_document_has_email_and_sso_token()
    test_build_import_document_is_grok_build_with_email_and_access_token()
    test_onboard_payloads_are_http_not_browser()
    test_extract_code_hyphenated_spacexai_not_six_digit_false_positive()
    test_cookie_origins_come_from_har_set_cookie_not_guesses()
    test_live_headers_do_not_copy_stale_har_sentry()
    test_preflight_refuses_jsdom_castle_and_seeded_js_cookies()
    test_json_and_header_origin_table_covers_har_auth_fields()
    test_live_path_source_does_not_seed_or_jsdom_mint()
    print("OK cffi har params")
