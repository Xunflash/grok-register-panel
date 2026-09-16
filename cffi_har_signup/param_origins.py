# -*- coding: utf-8 -*-
"""Where each 协议 HAR auth field actually comes from. Never invent values."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import unquote, urlparse

from signup_protocol import (
    OPTIONAL_LIVE_COOKIE_NAMES,
    auth_cookie_names_from_har,
    companion_class_for_url,
    har_entries,
)

# JSON body keys on the HAR auth trio. Values are origins, not recipes.
JSON_FIELD_ORIGINS = {
    "email": "mailbox_inventory",
    "castleRequestToken": "page_castle_sdk",
    "code": "outlook_otp",
    "password": "generated_at_signup",
    "givenName": "generated_at_signup",
    "familyName": "generated_at_signup",
    "emailValidationCode": "outlook_otp",
    "turnstileToken": "capsolver_live",
}

# Header *values* on HAR auth POSTs. Names may still be listed in the HAR contract.
HEADER_VALUE_ORIGINS = {
    "User-Agent": "browser_navigator",
    "Accept": "har_path_constant",
    "Accept-Language": "browser_navigator",
    "Accept-Encoding": "browser_tls_stack",
    "Content-Type": "har_path_constant",
    "Origin": "har_path_constant",
    "Referer": "har_path_constant",
    "Sec-Fetch-Dest": "browser_fetch",
    "Sec-Fetch-Mode": "browser_fetch",
    "Sec-Fetch-Site": "browser_fetch",
    "sentry-trace": "page_sentry_sdk",
    "baggage": "page_sentry_sdk",
    "Cookie": "browser_cookie_jar",
}

# Replay of captured sentry-trace/baggage is a stale page session, not a live SDK.
STALE_HAR_TRACE_HEADERS = frozenset({"sentry-trace", "baggage"})

CASTLE_PAGE_SOURCES = frozenset(
    {
        "Castle.createRequestToken",
        "_castle",
        "useCastle.createRequestToken",
        "page_sdk",
        "page_send_code_request",
    }
)


def _header_items(block: dict) -> list[dict]:
    items = (block or {}).get("headers") or []
    return [x for x in items if isinstance(x, dict)]


def set_cookie_pairs(entry: dict) -> list[tuple[str, str]]:
    """(name, value) from a HAR response Set-Cookie list. Empty if none."""
    out: list[tuple[str, str]] = []
    for item in _header_items(entry.get("response") or {}):
        if str(item.get("name") or "").lower() != "set-cookie":
            continue
        raw = str(item.get("value") or "")
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name = name.strip()
        if name:
            out.append((name, value))
    return out


def _entry_url(entry: dict) -> str:
    return str(((entry.get("request") or {}).get("url")) or "")


def _entry_method(entry: dict) -> str:
    return str(((entry.get("request") or {}).get("method")) or "").upper()


def html_signup_set_cookies(har: Any) -> dict[str, str]:
    """Set-Cookie pairs on GET /sign-up HTML. Server-issued, not JS."""
    found: dict[str, str] = {}
    for entry in har_entries(har):
        if _entry_method(entry) != "GET":
            continue
        url = _entry_url(entry)
        path = urlparse(url).path
        if path not in {"/sign-up", "/sign-up/"} and not path.endswith("/sign-up"):
            continue
        if any(part in path for part in ("/api/", "/_next/", "/cdn-cgi/")):
            continue
        for name, value in set_cookie_pairs(entry):
            found.setdefault(name, value)
    return found


def jsd_set_cookies(har: Any) -> dict[str, str]:
    """Set-Cookie pairs on Cloudflare jsd oneshot POST (issues cf_clearance)."""
    found: dict[str, str] = {}
    for entry in har_entries(har):
        if _entry_method(entry) != "POST":
            continue
        url = _entry_url(entry)
        if companion_class_for_url(url) != "cf_jsd" and "challenge-platform" not in url:
            continue
        for name, value in set_cookie_pairs(entry):
            found[name] = value
    return found


def classify_cookie_origins(har: Any) -> dict[str, str]:
    """Map each HAR auth cookie name to html_set_cookie | cf_jsd_set_cookie | not_in_set_cookie.

    not_in_set_cookie means the HAR request sent it but no response Set-Cookie issued it;
    the protocol panel waits for the live page jar (JS / OneTrust) instead of guessing.
    """
    html = {k.lower(): k for k in html_signup_set_cookies(har)}
    jsd = {k.lower(): k for k in jsd_set_cookies(har)}
    out: dict[str, str] = {}
    for name in auth_cookie_names_from_har(har):
        low = str(name).lower()
        if low in html:
            out[name] = "html_set_cookie"
        elif low in jsd:
            out[name] = "cf_jsd_set_cookie"
        else:
            out[name] = "not_in_set_cookie"
    return out


def redirect_cookie_shape(value: str) -> str:
    decoded = unquote(str(value or ""))
    if decoded.startswith("{") and "redirectDestination" in decoded:
        return "json_redirectDestination"
    if decoded in {"grok-com", "grok"}:
        return "bare_destination"
    return "other"


def strip_unlive_trace_headers(
    headers: dict[str, str],
    extra: Optional[dict] = None,
) -> dict[str, str]:
    """Drop HAR sentry-trace/baggage unless extra supplies a live SDK value."""
    live_names = {str(k).lower() for k, v in (extra or {}).items() if v}
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if str(name).lower() in STALE_HAR_TRACE_HEADERS and str(name).lower() not in live_names:
            continue
        out[name] = value
    return out


def castle_origin_allowed(source: str) -> bool:
    return str(source or "").strip() in CASTLE_PAGE_SOURCES


def preflight_live_origins(
    *,
    har: Any,
    http_cookies: Optional[dict] = None,
    castle_source: str = "",
    sentry_source: str = "",
) -> list[str]:
    """Problems that would require guessing. Empty list means HTTP-only fields are present
    and Castle/Sentry came from the page SDK. curl_cffi without a browser cannot pass this.
    """
    problems: list[str] = []
    cookies = {str(k): str(v) for k, v in (http_cookies or {}).items() if k}
    have = {k.lower() for k in cookies}
    if not castle_origin_allowed(castle_source):
        problems.append(
            "castleRequestToken origin is live page Castle SDK "
            "(window.Castle.createRequestToken / _castle / useCastle.createRequestToken); "
            f"got {castle_source or 'missing'}"
        )
    if str(sentry_source or "").strip() not in {"page_sdk", "intercepted_live_headers"}:
        problems.append(
            "sentry-trace/baggage origin is live Sentry.getTraceData or intercepted page headers; "
            "HAR traces are a different session and must not be copied; synthetic fixture keys are forbidden"
        )
    origins = classify_cookie_origins(har)
    optional = {n.lower() for n in OPTIONAL_LIVE_COOKIE_NAMES}
    for name, origin in origins.items():
        low = name.lower()
        present = low in have
        if origin == "html_set_cookie" and not present:
            problems.append(f"cookie {name}: HAR origin is GET /sign-up Set-Cookie; missing from HTTP jar")
        elif origin == "cf_jsd_set_cookie" and not present:
            if low in optional:
                continue
            problems.append(
                f"cookie {name}: HAR origin is CF jsd oneshot Set-Cookie; not guessing the 13kB challenge body"
            )
        elif origin == "not_in_set_cookie" and not present:
            problems.append(
                f"cookie {name}: HAR request sent it but no Set-Cookie issued it "
                "(page JS / OneTrust); not guessing"
            )
    return problems
