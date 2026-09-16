# -*- coding: utf-8 -*-
"""Build curl_cffi request headers/cookies from 协议 HAR name sets."""
from __future__ import annotations

import secrets
import uuid
from typing import Any, Optional

from param_origins import strip_unlive_trace_headers
from signup_protocol import (
    DEFAULT_USER_AGENT,
    HOP_BY_HOP_HEADERS,
    SIGNUP_PATHS,
    auth_cookie_names_from_har,
    auth_header_names_from_har,
    extract_signup_calls,
    fingerprint_headers_from_call,
    fingerprint_headers_from_har,
)


GRPC_FORBIDDEN = (
    "/auth_mgmt.AuthManagement/CreateEmailValidationCode",
    "/auth_mgmt.AuthManagement/CreateUserAndSessionV2",
)


def har_auth_name_contract(har: Any) -> dict[str, Any]:
    calls = extract_signup_calls(har)
    out: dict[str, Any] = {"paths": list(SIGNUP_PATHS), "calls": {}}
    for path in SIGNUP_PATHS:
        call = calls.get(path) or {}
        headers = call.get("headers") or {}
        out["calls"][path] = {
            "method": call.get("method"),
            "json_keys": sorted(call.get("json") or {}),
            "cookie_names": sorted(call.get("cookies") or {}),
            "header_names": sorted(
                k
                for k in headers
                if str(k).lower() not in HOP_BY_HOP_HEADERS
                and str(k).lower() != "cookie"
            ),
        }
    out["cookie_names"] = auth_cookie_names_from_har(har)
    out["header_names"] = auth_header_names_from_har(har)
    return out


def cffi_fingerprint_headers(har: Any) -> dict[str, str]:
    """HAR client headers for curl_cffi (not fetch-only subset).

    sentry-trace/baggage are omitted: those values are a captured page session.
    """
    headers = fingerprint_headers_from_har(har)
    headers = strip_unlive_trace_headers(headers)
    if not headers.get("User-Agent") and not headers.get("user-agent"):
        headers["User-Agent"] = DEFAULT_USER_AGENT
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    headers.setdefault("Origin", "https://accounts.x.ai")
    headers.setdefault("Referer", "https://accounts.x.ai/sign-up?redirect=grok-com")
    headers.setdefault("Content-Type", "application/json")
    return headers


def headers_for_auth_path(har: Any, path: str, extra: Optional[dict] = None) -> dict[str, str]:
    calls = extract_signup_calls(har)
    call = calls.get(path) or {}
    headers = fingerprint_headers_from_call(call) or cffi_fingerprint_headers(har)
    headers = strip_unlive_trace_headers(headers, extra=extra)
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    # Accept value is HAR-specific (send uses */*, create uses application/json).
    accept = None
    for name, value in (call.get("headers") or {}).items():
        if str(name).lower() == "accept" and value:
            accept = value
            break
    if accept:
        headers["Accept"] = accept
    return headers


def seed_har_cookie_names(existing: dict[str, str], har_names: list[str]) -> dict[str, str]:
    """Historical seeder. Produces guessed values that do not match HAR shapes.

    Do not call on the live path. Kept so tests can prove the mismatch.
    """
    out = dict(existing or {})
    have = {k.lower() for k in out}
    for name in har_names:
        if not name or name.lower() in have:
            continue
        low = name.lower()
        if low == "__cuid":
            out[name] = str(uuid.uuid4())
        elif low in {"gb_anon_id", "xai_anon_id"}:
            out[name] = str(uuid.uuid4())
        elif low == "redirect":
            out[name] = "grok-com"
        elif low == "referrer":
            out[name] = ""
        elif low == "optanonconsent":
            out[name] = "isGpcEnabled=0&datestamp=now&version=202401.1.0&isIABGlobal=false"
        elif "mixpanel" in low:
            out[name] = "$device:" + uuid.uuid4().hex
        elif low in {"cf_clearance", "__cf_bm"}:
            continue
        else:
            out[name] = secrets.token_urlsafe(12)
        have.add(low)
    return out


def synthetic_sentry_headers() -> dict[str, str]:
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return {
        "sentry-trace": f"{trace_id}-{span_id}-0",
        "baggage": "sentry-environment=production,sentry-public_key=fixture",
    }


def assert_no_grpc_signup_paths(urls: list[str]) -> None:
    joined = " ".join(urls)
    for forbidden in GRPC_FORBIDDEN:
        if forbidden in joined:
            raise AssertionError(f"signup chain used forbidden path {forbidden}")


def request_json_keys(built: dict) -> list[str]:
    return sorted((built.get("json") or {}).keys())


def cookie_names_from_session(session) -> list[str]:
    names: list[str] = []
    try:
        names = [str(k) for k in session.cookies.keys()]
    except Exception:
        try:
            names = [str(k) for k, _v in session.cookies.items()]
        except Exception:
            pass
    return sorted(set(names))
