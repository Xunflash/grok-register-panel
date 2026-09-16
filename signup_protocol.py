# -*- coding: utf-8 -*-
"""accounts.x.ai 协议注册：HAR 解析 + 请求体构造。

远程打码只用于 create-account 的 turnstileToken。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
)
DEFAULT_FINGERPRINT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://accounts.x.ai",
    "Referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
    "Content-Type": "application/json",
}

AUTH_ORIGIN = "https://accounts.x.ai"
AUTH_SEND_CODE = "/api/auth/send-verification-code"
AUTH_VERIFY_EMAIL = "/api/auth/sign-up/verify-email"
AUTH_CREATE_ACCOUNT = "/api/auth/sign-up/create-account"

SIGNUP_PATHS = (
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    AUTH_CREATE_ACCOUNT,
)

_PATH_ALIASES = {
    "send-verification-code": AUTH_SEND_CODE,
    "send_code": AUTH_SEND_CODE,
    "verify-email": AUTH_VERIFY_EMAIL,
    "verify_email": AUTH_VERIFY_EMAIL,
    "create-account": AUTH_CREATE_ACCOUNT,
    "create_account": AUTH_CREATE_ACCOUNT,
}


def _url_path(url: str) -> str:
    parsed = urlparse(str(url or ""))
    return parsed.path or ""


def _entry_request(entry: dict) -> dict:
    return entry.get("request") or {}


def _post_text(request: dict) -> str:
    post = request.get("postData") or {}
    if isinstance(post, str):
        return post
    text = post.get("text")
    if text:
        return str(text)
    params = post.get("params")
    if isinstance(params, list):
        return json.dumps({str(p.get("name")): p.get("value") for p in params if isinstance(p, dict)})
    return ""


def _post_json(request: dict) -> Optional[dict]:
    raw = _post_text(request).strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _header_map(request: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in request.get("headers") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out[name] = str(item.get("value") or "")
    return out


def _cookie_map(request: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in request.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out[name] = str(item.get("value") or "")
    cookie_header = _header_map(request).get("Cookie") or _header_map(request).get("cookie") or ""
    if cookie_header and not out:
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def load_har(source: Any) -> dict:
    """Load a HAR dict from path, bytes, str, or already-parsed object."""
    if isinstance(source, dict):
        if "log" in source or "entries" in source:
            return source
        raise ValueError("dict is not a HAR object")
    if isinstance(source, (bytes, bytearray)):
        text = source.decode("utf-8")
    elif isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    elif isinstance(source, str):
        stripped = source.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            text = source
        else:
            path = Path(source)
            if not path.is_file():
                raise ValueError(f"HAR path not found: {source}")
            text = path.read_text(encoding="utf-8")
    else:
        raise ValueError("unsupported HAR source")
    data = json.loads(text)
    if isinstance(data, dict):
        return data
    raise ValueError("HAR JSON must be an object")


def har_entries(har: Any) -> list[dict]:
    data = load_har(har)
    log = data.get("log") if isinstance(data.get("log"), dict) else data
    entries = log.get("entries") or []
    return [e for e in entries if isinstance(e, dict)]


def match_signup_path(url: str) -> Optional[str]:
    path = _url_path(url)
    for wanted in SIGNUP_PATHS:
        if path == wanted or path.endswith(wanted):
            return wanted
    return None


def extract_signup_calls(har: Any) -> dict[str, dict]:
    """Return the last captured call for each signup endpoint.

    Each value: {method, url, path, headers, cookies, json, status}
    """
    found: dict[str, dict] = {}
    for entry in har_entries(har):
        req = _entry_request(entry)
        url = str(req.get("url") or "")
        path = match_signup_path(url)
        if not path:
            continue
        method = str(req.get("method") or "GET").upper()
        status = None
        try:
            status = int((entry.get("response") or {}).get("status") or 0) or None
        except (TypeError, ValueError):
            status = None
        found[path] = {
            "method": method,
            "url": url,
            "path": path,
            "headers": _header_map(req),
            "cookies": _cookie_map(req),
            "json": _post_json(req),
            "post_text": _post_text(req),
            "status": status,
        }
    return found


# Hop-by-hop / proxy headers from HAR must not be replayed.
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-authorization",
}

# Client header names observed on HAR auth calls (plus optional sec-ch).
AUTH_CLIENT_HEADER_NAMES = (
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "content-type",
    "origin",
    "referer",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "te",
    "sentry-trace",
    "baggage",
)

# Headers page.evaluate(fetch) is allowed to set; Cookie/Origin/Referer/Sec-Fetch/UA are browser-controlled.
FETCH_SETTABLE_HEADERS = {
    "accept",
    "content-type",
    "sentry-trace",
    "baggage",
    "accept-language",
}

# Companion POSTs recorded around signup in signup-protocol-full.har.
COMPANION_CLASSES = (
    ("mp_track", "/mp/track"),
    ("cf_jsd", "/cdn-cgi/challenge-platform"),
    ("cf_rum", "/cdn-cgi/rum"),
    ("observability", "/api/observability/client-metrics"),
)


def fingerprint_headers_from_call(call: dict) -> dict[str, str]:
    """Keep client fingerprint headers from a captured request; drop hop-by-hop."""
    headers = call.get("headers") or {}
    out: dict[str, str] = {}
    for name, value in headers.items():
        low = name.lower()
        if low in HOP_BY_HOP_HEADERS or low == "cookie":
            continue
        if low in AUTH_CLIENT_HEADER_NAMES and value:
            out[name] = value
    return out


def build_send_verification_code(
    *,
    email: str,
    castle_request_token: str = "",
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"email": str(email or "").strip()}
    token = str(castle_request_token or "").strip()
    if token:
        body["castleRequestToken"] = token
    if extra:
        body.update(extra)
    return {
        "method": "POST",
        "url": AUTH_ORIGIN + AUTH_SEND_CODE,
        "path": AUTH_SEND_CODE,
        "json": body,
    }


def apply_live_otp(built: dict, code: str, field: str) -> dict:
    """Keep live XXX-YYY OTP on the named JSON field. Builders strip to digits."""
    payload = dict(built or {})
    body = dict(payload.get("json") or {})
    live = str(code or "").strip()
    if "-" in live:
        body[field] = live
    payload["json"] = body
    return payload


def build_verify_email(*, email: str, code: str, extra: Optional[dict] = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": str(email or "").strip(),
        "code": re.sub(r"\D+", "", str(code or "")),
    }
    if extra:
        body.update(extra)
    return {
        "method": "POST",
        "url": AUTH_ORIGIN + AUTH_VERIFY_EMAIL,
        "path": AUTH_VERIFY_EMAIL,
        "json": body,
    }


def build_create_account(
    *,
    email: str,
    password: str,
    given_name: str,
    family_name: str,
    email_validation_code: str,
    turnstile_token: str,
    castle_request_token: str = "",
    extra: Optional[dict] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "email": str(email or "").strip(),
        "password": str(password or ""),
        "givenName": str(given_name or "").strip(),
        "familyName": str(family_name or "").strip(),
        "emailValidationCode": re.sub(r"\D+", "", str(email_validation_code or "")),
        "turnstileToken": str(turnstile_token or "").strip(),
    }
    token = str(castle_request_token or "").strip()
    if token:
        # Live page JS (HAR bundle) sends castleRequestToken on create-account;
        # the captured create-account POST omitted it because that capture used fetch.
        body["castleRequestToken"] = token
    if extra:
        body.update(extra)
    return {
        "method": "POST",
        "url": AUTH_ORIGIN + AUTH_CREATE_ACCOUNT,
        "path": AUTH_CREATE_ACCOUNT,
        "json": body,
    }


def assert_captured_contract(calls: dict[str, dict]) -> None:
    send = calls.get(AUTH_SEND_CODE) or {}
    verify = calls.get(AUTH_VERIFY_EMAIL) or {}
    create = calls.get(AUTH_CREATE_ACCOUNT) or {}
    if str(send.get("method") or "") != "POST":
        raise AssertionError("send-verification-code missing POST capture")
    send_json = send.get("json") or {}
    if "email" not in send_json:
        raise AssertionError("send-verification-code json missing email")
    if "castleRequestToken" not in send_json:
        raise AssertionError("send-verification-code json missing castleRequestToken")
    if str(verify.get("method") or "") != "POST":
        raise AssertionError("verify-email missing POST capture")
    verify_json = verify.get("json") or {}
    if "email" not in verify_json or "code" not in verify_json:
        raise AssertionError("verify-email json missing email/code")
    if str(create.get("method") or "") != "POST":
        raise AssertionError("create-account missing POST capture")
    create_json = create.get("json") or {}
    for key in ("email", "password", "givenName", "familyName", "emailValidationCode", "turnstileToken"):
        if key not in create_json:
            raise AssertionError(f"create-account json missing {key}")


def fingerprint_headers_from_har(har: Any) -> dict[str, str]:
    calls = extract_signup_calls(har)
    for path in (AUTH_CREATE_ACCOUNT, AUTH_SEND_CODE, AUTH_VERIFY_EMAIL):
        call = calls.get(path)
        if call:
            headers = fingerprint_headers_from_call(call)
            if headers:
                return headers
    return dict(DEFAULT_FINGERPRINT_HEADERS)


def cookies_from_har_call(call: dict) -> dict[str, str]:
    return dict(call.get("cookies") or {})


def _norm_name_set(names) -> set[str]:
    return {str(n).strip() for n in (names or []) if str(n).strip()}


def auth_cookie_names_from_har(har: Any) -> list[str]:
    """Union of cookie names on captured auth POSTs, stable order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for path in SIGNUP_PATHS:
        call = extract_signup_calls(har).get(path) or {}
        for name in (call.get("cookies") or {}):
            if name and name not in seen:
                seen.add(name)
                ordered.append(name)
    return ordered


def auth_header_names_from_har(har: Any) -> list[str]:
    """Client header names on captured auth POSTs (no hop-by-hop / Cookie)."""
    seen: set[str] = set()
    ordered: list[str] = []
    calls = extract_signup_calls(har)
    for path in SIGNUP_PATHS:
        headers = (calls.get(path) or {}).get("headers") or {}
        for name in headers:
            low = str(name).lower()
            if not name or low in HOP_BY_HOP_HEADERS or low == "cookie":
                continue
            if low not in AUTH_CLIENT_HEADER_NAMES:
                continue
            if name not in seen and name.lower() not in {x.lower() for x in seen}:
                seen.add(name)
                ordered.append(name)
    return ordered


def json_keys_for_path(har: Any, path: str) -> list[str]:
    call = extract_signup_calls(har).get(path) or {}
    keys = list((call.get("json") or {}).keys())
    return keys


def har_json_key_union(har: Any) -> set[str]:
    """JSON field names that appear on any captured auth POST in the HAR dump."""
    keys: set[str] = set()
    for call in extract_signup_calls(har).values():
        keys |= set((call.get("json") or {}) or {})
    return keys


def companion_class_for_url(url: str) -> Optional[str]:
    path = _url_path(url)
    full = str(url or "")
    for name, marker in COMPANION_CLASSES:
        if marker in path or marker in full:
            return name
    return None


def _mixpanel_events_from_post(text: str) -> list[dict]:
    raw = str(text or "").strip()
    if not raw:
        return []
    payload = raw
    if "data=" in raw:
        from urllib.parse import parse_qs

        payload = (parse_qs(raw).get("data") or [""])[0]
    data: Any = None
    try:
        data = json.loads(payload)
    except Exception:
        try:
            import base64

            data = json.loads(base64.b64decode(payload).decode("utf-8"))
        except Exception:
            return []
    items = data if isinstance(data, list) else [data]
    return [x for x in items if isinstance(x, dict)]


def extract_companion_posts(har: Any) -> list[dict[str, Any]]:
    """Companion POSTs from a HAR: mp/track, CF jsd/rum, observability.

    Each item: method, url, path, class, json_keys, form_keys, event_names.
    Body values are not copied — live page must produce them.
    """
    found: list[dict[str, Any]] = []
    seen_class: set[str] = set()
    for entry in har_entries(har):
        req = _entry_request(entry)
        method = str(req.get("method") or "GET").upper()
        if method != "POST":
            continue
        url = str(req.get("url") or "")
        klass = companion_class_for_url(url)
        if not klass:
            continue
        post = req.get("postData") or {}
        mime = ""
        if isinstance(post, dict):
            mime = str(post.get("mimeType") or "")
        text = _post_text(req)
        json_body = _post_json(req)
        form_keys: list[str] = []
        event_names: list[str] = []
        if "urlencoded" in mime.lower() or text.startswith("data="):
            from urllib.parse import parse_qs

            form_keys = list(parse_qs(text).keys())
            event_names = [str(ev.get("event") or "") for ev in _mixpanel_events_from_post(text) if ev.get("event")]
        status = None
        try:
            status = int((entry.get("response") or {}).get("status") or 0) or None
        except (TypeError, ValueError):
            status = None
        found.append(
            {
                "method": method,
                "url": url,
                "path": _url_path(url),
                "class": klass,
                "mime": mime,
                "status": status,
                "json_keys": sorted(json_body.keys()) if json_body else [],
                "form_keys": form_keys,
                "event_names": event_names,
            }
        )
        seen_class.add(klass)
    return found


def companion_classes_from_har(har: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in extract_companion_posts(har):
        klass = str(item.get("class") or "")
        if klass and klass not in seen:
            seen.add(klass)
            ordered.append(klass)
    return ordered


def required_companion_classes_from_har(har: Any) -> list[str]:
    """Companion classes the capture actually completed (HTTP 2xx). status=-1 is not required."""
    ordered: list[str] = []
    seen: set[str] = set()
    for item in extract_companion_posts(har):
        klass = str(item.get("class") or "")
        status = item.get("status")
        try:
            code = int(status or 0)
        except (TypeError, ValueError):
            code = 0
        if not klass or klass in seen:
            continue
        if 200 <= code < 300:
            seen.add(klass)
            ordered.append(klass)
    return ordered


def overlay_live_on_har_names(har_names, live_map: Optional[dict]) -> dict[str, str]:
    """Copy live values only for names that appear in the HAR. Never invent names or values."""
    live = live_map or {}
    live_by_low = {str(k).lower(): (k, str(v) if v is not None else "") for k, v in live.items()}
    out: dict[str, str] = {}
    for name in har_names or []:
        key = str(name or "").strip()
        if not key:
            continue
        hit = live_by_low.get(key.lower())
        if not hit:
            continue
        _orig, value = hit
        if value:
            out[key] = value
    return out


def missing_live_names(har_names, live_map: Optional[dict]) -> list[str]:
    live = overlay_live_on_har_names(har_names, live_map)
    have = {k.lower() for k in live}
    return [str(n) for n in (har_names or []) if str(n).strip() and str(n).lower() not in have]


# CF may skip cf_clearance on some exits; in-page fetch still sends __cf_bm.
OPTIONAL_LIVE_COOKIE_NAMES = frozenset({"cf_clearance"})


def hard_missing_live_cookie_names(har_names, live_map: Optional[dict]) -> list[str]:
    """HAR cookie names that must exist live. cf_clearance is optional."""
    missing = missing_live_names(har_names, live_map)
    skip = {n.lower() for n in OPTIONAL_LIVE_COOKIE_NAMES}
    return [n for n in missing if str(n).lower() not in skip]


def fetch_settable_headers(har_header_names, live_headers: Optional[dict]) -> dict[str, str]:
    """Headers the in-page fetch is allowed to set, filtered to HAR names + live values."""
    overlay = overlay_live_on_har_names(har_header_names, live_headers)
    return {k: v for k, v in overlay.items() if k.lower() in FETCH_SETTABLE_HEADERS}


def assert_send_code_has_castle(built: dict) -> None:
    body = (built or {}).get("json") or {}
    token = str(body.get("castleRequestToken") or "").strip()
    if not token:
        raise AssertionError("send-verification-code missing live castleRequestToken")


def assert_json_keys_match_capture(
    built_json: dict,
    captured_json: dict,
    extra_allowed: Optional[set] = None,
) -> None:
    built_keys = set(built_json or {})
    captured_keys = set(captured_json or {})
    allowed = {str(x) for x in (extra_allowed or set())}
    missing = captured_keys - built_keys
    extra = built_keys - captured_keys - allowed
    if missing or extra:
        raise AssertionError(f"json keys mismatch missing={sorted(missing)} extra={sorted(extra)}")


def live_names_subset_of_har(live_names, har_names) -> bool:
    har = {str(n).lower() for n in _norm_name_set(har_names)}
    return all(str(n).lower() in har for n in _norm_name_set(live_names))


def extra_live_json_keys(live_keys, har: Any) -> list[str]:
    """Live JSON names not present anywhere in the HAR auth dump (invented names)."""
    allowed = {str(k) for k in har_json_key_union(har)}
    extra = []
    seen: set[str] = set()
    for name in live_keys or []:
        key = str(name or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if key not in allowed:
            extra.append(key)
    return extra


def make_protocol_session(
    *,
    proxy_url: str = "",
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    impersonate: str = "firefox135",
):
    """curl_cffi session with captured UA/cookies; proxy is the panel Resin URL."""
    from curl_cffi import requests as cf_requests

    session = cf_requests.Session(impersonate=impersonate)
    proxy = str(proxy_url or "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    merged = dict(DEFAULT_FINGERPRINT_HEADERS)
    if headers:
        merged.update(headers)
    session.headers.update(merged)
    for name, value in (cookies or {}).items():
        if name and value:
            session.cookies.set(name, value)
    return session


def post_signup_json(session, built: dict, timeout: float = 30.0):
    """POST one builder payload. Returns (status, json_or_text)."""
    url = str(built.get("url") or (AUTH_ORIGIN + built.get("path", "")))
    headers = dict(DEFAULT_FINGERPRINT_HEADERS)
    headers.update(built.get("headers") or {})
    response = session.post(url, json=built.get("json") or {}, headers=headers, timeout=timeout)
    data: Any = None
    try:
        data = response.json()
    except Exception:
        data = response.text
    return response.status_code, data, response


def parse_inpage_post_result(result: Any) -> tuple:
    """Normalize page.evaluate(fetch) output to (status, json_or_text, raw)."""
    if not isinstance(result, dict):
        return 0, {"error": str(result)}, result
    try:
        status = int(result.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    data = result.get("json")
    if data is None:
        data = result.get("text") or ""
    return status, data, result


def sso_from_create_payload(payload: Any) -> str:
    """create-account JSON 里的 session / sso 字段。"""
    if isinstance(payload, str) and len(payload.strip()) >= 20:
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    session = payload.get("session")
    if isinstance(session, str) and len(session.strip()) >= 20:
        return session.strip()
    if isinstance(session, dict):
        for key in ("sso", "sso-rw", "token", "value", "id", "session"):
            value = session.get(key)
            if isinstance(value, str) and len(value.strip()) >= 20:
                return value.strip()
    for key in ("sso", "sso-rw"):
        value = payload.get(key)
        if isinstance(value, str) and len(value.strip()) >= 20:
            return value.strip()
    return ""


def sso_from_response(response, payload: Any = None) -> str:
    """Read sso cookie from a curl_cffi/requests response or cookie jar."""
    try:
        value = response.cookies.get("sso") or response.cookies.get("sso-rw") or ""
        if value:
            return str(value)
    except Exception:
        pass
    try:
        jar = getattr(response, "cookies", None)
        if jar is not None:
            for cookie in jar:
                if str(getattr(cookie, "name", "")).lower() in {"sso", "sso-rw"}:
                    return str(cookie.value or "")
    except Exception:
        pass
    try:
        get_all = getattr(response.cookies, "get_all", None)
        if get_all:
            for cookie in get_all("sso") or []:
                if getattr(cookie, "value", ""):
                    return str(cookie.value)
    except Exception:
        pass
    from_payload = sso_from_create_payload(payload)
    if from_payload:
        return from_payload
    return ""


def _union_names(*groups) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for name in group or []:
            key = str(name)
            if key and key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def canonical_signup_chain(hars: Any) -> dict[str, Any]:
    """Union one or more HAR captures into a single ordered signup request chain.

    Auth POSTs stay in SIGNUP_PATHS order. Companion classes, cookie names,
    header names, and JSON keys are unions. Body values are not copied.
    """
    if isinstance(hars, (dict, str, Path, bytes, bytearray)):
        sources = [hars]
    else:
        sources = list(hars or [])
    if not sources:
        raise ValueError("canonical_signup_chain requires at least one HAR")

    auth_steps: list[dict[str, Any]] = []
    for path in SIGNUP_PATHS:
        json_keys: list[str] = []
        cookie_names: list[str] = []
        header_names: list[str] = []
        methods: list[str] = []
        present_in = 0
        for har in sources:
            call = extract_signup_calls(har).get(path) or {}
            if not call:
                continue
            present_in += 1
            methods.append(str(call.get("method") or "POST").upper())
            json_keys = _union_names(json_keys, (call.get("json") or {}).keys())
            cookie_names = _union_names(cookie_names, (call.get("cookies") or {}).keys())
            header_names = _union_names(
                header_names,
                [
                    name
                    for name in (call.get("headers") or {})
                    if str(name).lower() not in HOP_BY_HOP_HEADERS
                    and str(name).lower() != "cookie"
                ],
            )
        auth_steps.append(
            {
                "path": path,
                "method": methods[-1] if methods else "POST",
                "present_in": present_in,
                "required": True,
                "json_keys": json_keys,
                "cookie_names": cookie_names,
                "header_names": header_names,
            }
        )

    companion_classes: list[str] = []
    for har in sources:
        companion_classes = _union_names(companion_classes, companion_classes_from_har(har))

    return {
        "auth_steps": auth_steps,
        "companion_classes": companion_classes,
        "cookie_names": _union_names(*(step["cookie_names"] for step in auth_steps)),
        "header_names": _union_names(*(step["header_names"] for step in auth_steps)),
        "har_count": len(sources),
    }


def protocol_replay_chain() -> dict[str, Any]:
    """Auth POSTs emitted by the shipped protocol builders.

    Companion Mixpanel / CF jsd / rum bodies are not synthesized here.
    Protocol waits for live-page companion POSTs instead of inventing them.
    """
    send = build_send_verification_code(
        email="fixture@example.com",
        castle_request_token="castle-fixture",
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
    return {
        "auth_steps": [
            {
                "path": AUTH_SEND_CODE,
                "method": send["method"],
                "json_keys": sorted(send["json"]),
            },
            {
                "path": AUTH_VERIFY_EMAIL,
                "method": verify["method"],
                "json_keys": sorted(verify["json"]),
            },
            {
                "path": AUTH_CREATE_ACCOUNT,
                "method": create["method"],
                "json_keys": sorted(create["json"]),
            },
        ],
        "companion_classes": [],
        "companion_wait": True,
        "replay_paths": list(SIGNUP_PATHS),
    }


def diff_canonical_chain_vs_protocol(
    canonical: dict[str, Any],
    protocol: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Step-by-step diff of a HAR canonical chain vs shipped protocol builders.

    Each row: step, kind in {match, missing, extra, value-drift}, evidence.
    Does not invent Mixpanel / Castle / CF jsd request bodies.
    """
    proto = protocol or protocol_replay_chain()
    rows: list[dict[str, Any]] = []
    can_auth = {str(step.get("path")): step for step in (canonical.get("auth_steps") or [])}
    proto_auth = {str(step.get("path")): step for step in (proto.get("auth_steps") or [])}
    ordered: list[str] = []
    for path in list(SIGNUP_PATHS) + list(can_auth) + list(proto_auth):
        if path and path not in ordered:
            ordered.append(path)

    for path in ordered:
        captured = can_auth.get(path)
        built = proto_auth.get(path)
        if captured and not built:
            rows.append(
                {
                    "step": path,
                    "kind": "extra",
                    "side": "har",
                    "evidence": "HAR auth POST is not in protocol builders/replay list",
                }
            )
            continue
        if built and not captured:
            rows.append(
                {
                    "step": path,
                    "kind": "missing",
                    "side": "har",
                    "evidence": "protocol builder path is absent from the HAR chain",
                }
            )
            continue
        if not captured or not built:
            continue
        if int(captured.get("present_in") or 0) <= 0:
            rows.append(
                {
                    "step": path,
                    "kind": "missing",
                    "side": "har",
                    "evidence": "auth path not present in any HAR",
                }
            )
            continue
        har_keys = set(captured.get("json_keys") or [])
        proto_keys = set(built.get("json_keys") or [])
        missing_in_protocol = sorted(har_keys - proto_keys)
        extra_in_protocol = sorted(proto_keys - har_keys)
        if missing_in_protocol or extra_in_protocol:
            rows.append(
                {
                    "step": path,
                    "kind": "value-drift",
                    "evidence": (
                        f"json_keys har={sorted(har_keys)} protocol={sorted(proto_keys)} "
                        f"missing_in_protocol={missing_in_protocol} "
                        f"extra_in_protocol={extra_in_protocol}"
                    ),
                    "missing_in_protocol": missing_in_protocol,
                    "extra_in_protocol": extra_in_protocol,
                }
            )
        else:
            rows.append(
                {
                    "step": path,
                    "kind": "match",
                    "evidence": f"json_keys={sorted(har_keys)}",
                }
            )

    har_comp = [str(x) for x in (canonical.get("companion_classes") or []) if str(x)]
    proto_comp = {str(x) for x in (proto.get("companion_classes") or []) if str(x)}
    wait = bool(proto.get("companion_wait"))
    for klass in har_comp:
        if wait:
            rows.append(
                {
                    "step": f"companion:{klass}",
                    "kind": "match",
                    "evidence": (
                        "HAR companion class; protocol waits on the live page "
                        "and does not invent Mixpanel/Castle/jsd bodies"
                    ),
                }
            )
        elif klass in proto_comp:
            rows.append(
                {
                    "step": f"companion:{klass}",
                    "kind": "match",
                    "evidence": "companion class present on both sides",
                }
            )
        else:
            rows.append(
                {
                    "step": f"companion:{klass}",
                    "kind": "missing",
                    "side": "protocol",
                    "evidence": (
                        "HAR companion class is not in the protocol POST list; "
                        "protocol must not invent Mixpanel/Castle/jsd bodies"
                    ),
                }
            )
    for klass in sorted(proto_comp - set(har_comp)):
        rows.append(
            {
                "step": f"companion:{klass}",
                "kind": "extra",
                "side": "protocol",
                "evidence": "protocol lists a companion class not seen in the HAR union",
            }
        )
    return rows
