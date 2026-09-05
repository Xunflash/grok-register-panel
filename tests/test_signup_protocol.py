# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signup_protocol import (  # noqa: E402
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    assert_captured_contract,
    assert_json_keys_match_capture,
    assert_send_code_has_castle,
    auth_cookie_names_from_har,
    auth_header_names_from_har,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    companion_class_for_url,
    companion_classes_from_har,
    extra_live_json_keys,
    hard_missing_live_cookie_names,
    missing_live_names,
    required_companion_classes_from_har,
    extract_companion_posts,
    extract_signup_calls,
    fetch_settable_headers,
    har_json_key_union,
    fingerprint_headers_from_call,
    fingerprint_headers_from_har,
    json_keys_for_path,
    live_names_subset_of_har,
    overlay_live_on_har_names,
    parse_inpage_post_result,
    post_signup_json,
    sso_from_create_payload,
    sso_from_response,
)

FIXTURE = ROOT / "tests" / "fixtures" / "signup_har_excerpt.json"


def test_extract_three_signup_posts_from_har_excerpt():
    calls = extract_signup_calls(FIXTURE)
    assert_captured_contract(calls)
    send = calls[AUTH_SEND_CODE]
    verify = calls[AUTH_VERIFY_EMAIL]
    create = calls[AUTH_CREATE_ACCOUNT]
    assert send["method"] == "POST"
    assert send["path"] == AUTH_SEND_CODE
    assert "email" in send["json"]
    assert "castleRequestToken" in send["json"]
    assert verify["method"] == "POST"
    assert set(verify["json"]) >= {"email", "code"}
    assert create["method"] == "POST"
    assert set(create["json"]) >= {
        "email",
        "password",
        "givenName",
        "familyName",
        "emailValidationCode",
        "turnstileToken",
    }
    assert "cf_clearance" in create["cookies"]
    ua = fingerprint_headers_from_call(create)
    assert any(k.lower() == "user-agent" for k in ua)


def test_builders_match_captured_json_field_names():
    calls = extract_signup_calls(FIXTURE)
    captured_send = calls[AUTH_SEND_CODE]["json"]
    built_send = build_send_verification_code(
        email=captured_send["email"],
        castle_request_token=captured_send["castleRequestToken"],
    )
    assert built_send["method"] == "POST"
    assert built_send["path"] == AUTH_SEND_CODE
    assert built_send["json"]["email"] == captured_send["email"]
    assert "castleRequestToken" in built_send["json"]

    captured_verify = calls[AUTH_VERIFY_EMAIL]["json"]
    built_verify = build_verify_email(email=captured_verify["email"], code=captured_verify["code"])
    assert built_verify["path"] == AUTH_VERIFY_EMAIL
    assert set(built_verify["json"]) == {"email", "code"}

    captured_create = calls[AUTH_CREATE_ACCOUNT]["json"]
    built_create = build_create_account(
        email=captured_create["email"],
        password=captured_create["password"],
        given_name=captured_create["givenName"],
        family_name=captured_create["familyName"],
        email_validation_code=captured_create["emailValidationCode"],
        turnstile_token=captured_create["turnstileToken"],
    )
    assert built_create["path"] == AUTH_CREATE_ACCOUNT
    assert built_create["json"]["givenName"] == captured_create["givenName"]
    assert built_create["json"]["familyName"] == captured_create["familyName"]
    assert built_create["json"]["emailValidationCode"] == captured_create["emailValidationCode"]
    assert built_create["json"]["turnstileToken"] == captured_create["turnstileToken"]
    assert "turnstileToken" in built_create["json"]
    assert "castleRequestToken" not in built_create["json"]
    live_castle = captured_send["castleRequestToken"]
    built_create_castle = build_create_account(
        email=captured_create["email"],
        password=captured_create["password"],
        given_name=captured_create["givenName"],
        family_name=captured_create["familyName"],
        email_validation_code=captured_create["emailValidationCode"],
        turnstile_token=captured_create["turnstileToken"],
        castle_request_token=live_castle,
    )
    assert built_create_castle["json"]["castleRequestToken"] == live_castle
    assert_json_keys_match_capture(
        built_create_castle["json"],
        captured_create,
        extra_allowed={"castleRequestToken"},
    )
    dump_keys = har_json_key_union(FIXTURE)
    assert "castleRequestToken" in dump_keys
    assert extra_live_json_keys(built_create_castle["json"].keys(), FIXTURE) == []
    assert extra_live_json_keys(["inventedField"], FIXTURE) == ["inventedField"]


def test_fingerprint_headers_from_har_keep_user_agent():
    headers = fingerprint_headers_from_har(FIXTURE)
    assert any(k.lower() == "user-agent" for k in headers)
    assert "Firefox" in "".join(headers.values())


class _FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.cookies = {"sso": "sso-fixture-token"}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=30):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(200, {"ok": True})


def test_post_signup_json_uses_builder_url_and_body():
    session = _FakeSession()
    built = build_verify_email(email="a@b.c", code="123-456")
    status, data, resp = post_signup_json(session, built)
    assert status == 200
    assert data == {"ok": True}
    assert session.calls[0]["url"].endswith(AUTH_VERIFY_EMAIL)
    assert session.calls[0]["json"]["code"] == "123456"
    assert session.calls[0]["json"]["email"] == "a@b.c"


def test_sso_from_create_payload_and_response():
    token = "s" * 40
    assert sso_from_create_payload({"session": token, "redirectUrl": "/account"}) == token
    assert sso_from_create_payload({"session": {"sso": token}}) == token
    resp = _FakeResponse(200, {"session": token})
    assert sso_from_response(resp, {"session": "x" * 8}) == "sso-fixture-token"
    empty = type("R", (), {"cookies": {}})()
    assert sso_from_response(empty, {"session": token}) == token


def test_parse_inpage_post_result():
    status, data, raw = parse_inpage_post_result(
        {"status": 200, "json": {"ok": True, "session": "s" * 40}, "text": ""}
    )
    assert status == 200
    assert data["ok"] is True
    assert data["session"].startswith("s")
    status, data, raw = parse_inpage_post_result({"status": "403", "text": "blocked"})
    assert status == 403
    assert data == "blocked"
    status, data, raw = parse_inpage_post_result("boom")
    assert status == 0
    assert "boom" in str(data)


def test_har_excerpt_cookie_and_header_names_from_capture():
    cookies = auth_cookie_names_from_har(FIXTURE)
    assert "cf_clearance" in cookies
    headers = auth_header_names_from_har(FIXTURE)
    header_l = {h.lower() for h in headers}
    assert "user-agent" in header_l
    assert "sentry-trace" in header_l
    assert "baggage" in header_l
    assert "sec-fetch-mode" in header_l
    assert "proxy-authorization" not in header_l
    assert "host" not in header_l
    send_headers = fingerprint_headers_from_call(extract_signup_calls(FIXTURE)[AUTH_SEND_CODE])
    assert any(k.lower() == "sentry-trace" for k in send_headers)
    assert json_keys_for_path(FIXTURE, AUTH_SEND_CODE) == ["email", "castleRequestToken"] or set(
        json_keys_for_path(FIXTURE, AUTH_SEND_CODE)
    ) == {"email", "castleRequestToken"}


def test_send_code_builder_requires_captured_castle_key():
    captured = extract_signup_calls(FIXTURE)[AUTH_SEND_CODE]["json"]
    built = build_send_verification_code(
        email=captured["email"],
        castle_request_token=captured["castleRequestToken"],
    )
    assert_send_code_has_castle(built)
    assert_json_keys_match_capture(built["json"], captured)
    empty = build_send_verification_code(email="a@b.c", castle_request_token="")
    try:
        assert_send_code_has_castle(empty)
    except AssertionError:
        pass
    else:
        raise AssertionError("empty castle should fail")


def test_overlay_live_values_only_for_har_names():
    har_names = ["cf_clearance", "OptanonConsent", "__cuid"]
    live = {"cf_clearance": "live-cf", "OptanonConsent": "live-optanon", "invented": "nope"}
    overlay = overlay_live_on_har_names(har_names, live)
    assert overlay == {"cf_clearance": "live-cf", "OptanonConsent": "live-optanon"}
    assert "invented" not in overlay
    missing = missing_live_names(har_names, {"cf_clearance": "x"})
    assert "__cuid" in missing
    assert "OptanonConsent" in missing
    hard = hard_missing_live_cookie_names(
        har_names, {"OptanonConsent": "x", "__cuid": "y"}
    )
    assert hard == []
    still_hard = hard_missing_live_cookie_names(har_names, {"cf_clearance": "x"})
    assert "__cuid" in still_hard
    assert "OptanonConsent" in still_hard
    assert live_names_subset_of_har(overlay.keys(), har_names)
    assert not live_names_subset_of_har(["invented"], har_names)
    fetch_h = fetch_settable_headers(
        ["Accept", "sentry-trace", "User-Agent", "baggage"],
        {"Accept": "*/*", "sentry-trace": "live-st", "User-Agent": "Mozilla/5.0", "baggage": "live-bag"},
    )
    assert set(k.lower() for k in fetch_h) <= {"accept", "sentry-trace", "baggage"}
    assert "User-Agent" not in fetch_h


def test_companion_posts_extracted_from_fixture_field_names():
    classes = companion_classes_from_har(FIXTURE)
    assert "mp_track" in classes
    assert "cf_jsd" in classes
    assert "cf_rum" in classes
    assert "observability" in classes
    assert companion_class_for_url("https://accounts.x.ai/mp/track/?verbose=1") == "mp_track"
    assert companion_class_for_url(
        "https://accounts.x.ai/cdn-cgi/challenge-platform/h/b/jsd/oneshot/abc"
    ) == "cf_jsd"
    posts = extract_companion_posts(FIXTURE)
    by_class = {p["class"]: p for p in posts}
    assert "data" in by_class["mp_track"]["form_keys"]
    assert "account_signup_flow_started" in by_class["mp_track"]["event_names"]
    assert "events" in by_class["observability"]["json_keys"]
    assert "eventType" in by_class["cf_rum"]["json_keys"]
    assert "location" in by_class["cf_rum"]["json_keys"]
    assert set(required_companion_classes_from_har(FIXTURE)) == set(classes)


def test_har_json_key_union_covers_create_castle_from_send_code():
    """castleRequestToken is a HAR dump name (send-code). Using it on create-account is not invented."""
    dump_keys = har_json_key_union(FIXTURE)
    send_keys = set(json_keys_for_path(FIXTURE, AUTH_SEND_CODE))
    create_keys = set(json_keys_for_path(FIXTURE, AUTH_CREATE_ACCOUNT))
    assert "castleRequestToken" in send_keys
    assert "castleRequestToken" in dump_keys
    assert extra_live_json_keys(send_keys | create_keys | {"castleRequestToken"}, FIXTURE) == []
    assert extra_live_json_keys(["notInHar"], FIXTURE) == ["notInHar"]


def test_required_companions_skip_incomplete_har_status():
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "https://accounts.x.ai/cdn-cgi/rum?",
                        "postData": {"mimeType": "application/json", "text": "{}"},
                    },
                    "response": {"status": -1},
                },
                {
                    "request": {
                        "method": "POST",
                        "url": "https://accounts.x.ai/cdn-cgi/challenge-platform/h/b/jsd/oneshot/x",
                        "postData": {"mimeType": "text/plain", "text": "jsd"},
                    },
                    "response": {"status": 200},
                },
            ]
        }
    }
    assert "cf_rum" in companion_classes_from_har(har)
    assert required_companion_classes_from_har(har) == ["cf_jsd"]


if __name__ == "__main__":
    test_extract_three_signup_posts_from_har_excerpt()
    test_builders_match_captured_json_field_names()
    test_fingerprint_headers_from_har_keep_user_agent()
    test_post_signup_json_uses_builder_url_and_body()
    test_sso_from_create_payload_and_response()
    test_parse_inpage_post_result()
    test_har_excerpt_cookie_and_header_names_from_capture()
    test_send_code_builder_requires_captured_castle_key()
    test_overlay_live_values_only_for_har_names()
    test_companion_posts_extracted_from_fixture_field_names()
    test_required_companions_skip_incomplete_har_status()
    test_har_json_key_union_covers_create_castle_from_send_code()
    print("OK signup protocol")

