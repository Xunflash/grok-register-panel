# -*- coding: utf-8 -*-
"""curl_cffi session that replays 协议 HAR auth POSTs. No browser."""
from __future__ import annotations

import re
import uuid
from typing import Any, Optional
from cffi_headers import (
    cffi_fingerprint_headers,
    cookie_names_from_session,
    headers_for_auth_path,
)
from signup_protocol import (
    AUTH_ORIGIN,
    make_protocol_session,
    post_signup_json,
)

SIGNUP_URL = AUTH_ORIGIN + "/sign-up?redirect=grok-com"
GROK_HOME_URL = "https://grok.com/"
IMPERSONATE = "chrome136"
DEFAULT_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
# HTML embeds castlePk inside an escaped RSC/JSON string: castlePk\":\"pk_...
CASTLE_PK_RE = re.compile(r'\\?"castlePk\\?"\s*:\s*\\?"(pk_[^"\\]+)\\?"')
SITEKEY_RE = re.compile(r"0x4[0-9A-Za-z_-]{8,}")


def signup_page_url() -> str:
    """Same long sign-up URL the Patchright path opens."""
    htid = str(uuid.uuid4())
    return (
        f"{SIGNUP_URL}&return_to=%2F&mwbv=true&htid={htid}"
        "&analytics_consent=true&advertising_consent=true"
    )


def open_session(har: Any, proxy_url: str, impersonate: str = IMPERSONATE):
    headers = cffi_fingerprint_headers(har)
    session = make_protocol_session(
        proxy_url=proxy_url,
        headers=headers,
        impersonate=impersonate,
    )
    return session


def bootstrap_signup(session, har: Any, timeout: float = 45.0) -> dict[str, Any]:
    """GET /sign-up and keep only cookies the HTTP response actually set.

    Does not invent __cuid / xai_anon_id / OptanonConsent / mixpanel / redirect.
    """
    headers = cffi_fingerprint_headers(har)
    get_headers = dict(headers)
    get_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    get_headers["Referer"] = GROK_HOME_URL
    get_headers.pop("Content-Type", None)
    resp = session.get(signup_page_url(), headers=get_headers, timeout=timeout, allow_redirects=True)
    html = ""
    try:
        html = resp.text or ""
    except Exception:
        html = ""
    pk_match = CASTLE_PK_RE.search(html)
    site_match = SITEKEY_RE.search(html)
    live = {}
    try:
        live = {str(k): str(v) for k, v in session.cookies.items()}
    except Exception:
        live = {}
    return {
        "status": int(getattr(resp, "status_code", 0) or 0),
        "castle_pk": pk_match.group(1) if pk_match else "",
        "sitekey": site_match.group(0) if site_match else "",
        "cookie_names": cookie_names_from_session(session),
        "http_cookies": live,
        "final_url": str(getattr(resp, "url", "") or SIGNUP_URL),
        "html_len": len(html),
    }


def probe_live_signup(proxy_url: str, email: str = "probe@example.com", timeout: float = 45.0) -> dict[str, Any]:
    """GET the sign-up page with curl_cffi, then POST send-code without a Castle token."""
    from signup_protocol import AUTH_ORIGIN, AUTH_SEND_CODE, make_protocol_session

    session = make_protocol_session(proxy_url=proxy_url, impersonate=IMPERSONATE)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": GROK_HOME_URL,
    }
    resp = session.get(signup_page_url(), headers=headers, timeout=timeout, allow_redirects=True)
    html = ""
    try:
        html = resp.text or ""
    except Exception:
        html = ""
    post = session.post(
        AUTH_ORIGIN + AUTH_SEND_CODE,
        json={"email": str(email or "probe@example.com").strip()},
        headers={
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": AUTH_ORIGIN,
            "Referer": str(getattr(resp, "url", "") or SIGNUP_URL),
        },
        timeout=timeout,
    )
    data: Any = None
    try:
        data = post.json()
    except Exception:
        data = {}
    code = ""
    keys: list[str] = []
    if isinstance(data, dict):
        code = str(data.get("code") or data.get("error") or "")[:80]
        keys = sorted(str(k) for k in data.keys())
    return {
        "impersonate": IMPERSONATE,
        "status": int(getattr(resp, "status_code", 0) or 0),
        "html_len": len(html),
        "castle_pk": bool(CASTLE_PK_RE.search(html)),
        "castle_script": "02bh2mv7efkj1.js" in html,
        "cookie_names": sorted({str(k) for k in session.cookies.keys()}),
        "send_status": int(getattr(post, "status_code", 0) or 0),
        "send_code": code,
        "send_keys": keys,
    }


def post_auth(session, har: Any, built: dict, timeout: float = 45.0):
    path = str(built.get("path") or "")
    headers = headers_for_auth_path(har, path)
    payload = dict(built)
    payload["headers"] = headers
    return post_signup_json(session, payload, timeout=timeout)


def response_json_keys(data: Any) -> list[str]:
    if isinstance(data, dict):
        return sorted(data.keys())
    return []
