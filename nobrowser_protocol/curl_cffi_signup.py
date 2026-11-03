# -*- coding: utf-8 -*-
"""One signup through curl_cffi plus a Node Castle token. No browser per account."""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import re
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from browser_session import castle_sec_ch_headers
from nobrowser_protocol.castle_env import DEFAULT_STYLE_FILE, write_castle_fingerprint
from nobrowser_protocol.patchright_engine import (
    FIRST,
    LAST,
    ExistingEmailError,
    _rand_password,
    existing_email_payload,
    raise_send_code_rejection,
)
from signup_protocol import (
    AUTH_ORIGIN,
    build_create_account,
    build_send_verification_code,
    build_verify_email,
    make_protocol_session,
    sso_from_create_payload,
    sso_from_response,
)

ROOT = Path(__file__).resolve().parents[1]
MINT = ROOT / "scripts" / "castle_node_mint.mjs"
GROK_HOME_URL = "https://grok.com/"
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
SIGNIN_URL = "https://accounts.x.ai/sign-in?redirect=grok-com&_rsc=n4HnGKIS6qAv4XlC"
CASTLE_SDK_URL = "https://accounts.x.ai/_next/static/chunks/02bh2mv7efkj1.js"
TURNSTILE_API_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js"
MP_TRACK_URL = "https://accounts.x.ai/mp/track"
METRICS_URL = "https://accounts.x.ai/api/observability/client-metrics"
RUM_URL = "https://accounts.x.ai/cdn-cgi/rum"
MONITOR_URL = "https://accounts.x.ai/monitoring?o=4508179396558848&p=4511625974906880&r=us"
MP_TOKEN = "0b4055a12491884bcb6f34a5aa2718b6"
SENTRY_DSN = ("https://" + "db8ddf17ed5366946adc03fff1b8ffb1" + "@" +
               "o4508179396558848.ingest.us.sentry.io/4511625974906880")
SENTRY_PUBLIC_KEY = "db8ddf17ed5366946adc03fff1b8ffb1"
SENTRY_ORG_ID = "4508179396558848"
FALLBACK_DEPLOYMENT = "55ca444f246736c8d4d266a7b01215d2cf4b5ef7"
IMPERSONATE = "chrome145"
_SAFE_NODE_PREFIXES = (
    "fp ",
    "script_url",
    "token_len",
    "mint_error",
    "event_count",
    "slot 98",
    "style_names",
    "bytes",
    "pk ",
    "deflate",
)


def _client_headers(
    ua: str,
    referer: str,
    *,
    trace: str,
    baggage: str,
    hints: dict | None = None,
) -> dict[str, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": AUTH_ORIGIN,
        "Referer": referer,
        "User-Agent": ua,
        "sentry-trace": trace,
        "baggage": baggage,
    }
    headers.update(castle_sec_ch_headers(hints))
    return headers


def _auth_headers(ua: str, referer: str, *, trace: str, baggage: str) -> dict[str, str]:
    headers = _client_headers(ua, referer, trace=trace, baggage=baggage)
    headers["Content-Type"] = "application/json"
    return headers


def _deployment_id(html: str) -> str:
    match = re.search(r"(?:dpl=|deploymentId[\"']?\s*[:=]\s*[\"'])([a-f0-9]{40})", html or "")
    return match.group(1) if match else FALLBACK_DEPLOYMENT


def _sentry_headers(deployment_id: str) -> tuple[str, str]:
    trace_id = uuid.uuid4().hex
    span = uuid.uuid4().hex[:16]
    trace = f"{trace_id}-{span}-0"
    release = f"account%40{deployment_id}"
    baggage = ",".join(
        [
            "sentry-environment=production",
            f"sentry-release={release}",
            f"sentry-public_key={SENTRY_PUBLIC_KEY}",
            f"sentry-trace_id={trace_id}",
            f"sentry-org_id={SENTRY_ORG_ID}",
            "sentry-sampled=false",
            "sentry-sample_rand=0.4829103341",
            "sentry-sample_rate=0",
        ]
    )
    return trace, baggage


def _quiet_get(session, url: str, headers: dict, log: Callable[[str], None], name: str) -> Any:
    try:
        response = session.get(url, headers=headers, timeout=30, allow_redirects=True)
    except Exception as exc:
        log(f"warmup {name} fail {type(exc).__name__}")
        return None
    log(f"warmup {name} {response.status_code}")
    return response


def _quiet_post(session, url: str, headers: dict, log: Callable[[str], None], name: str, **kwargs) -> None:
    try:
        response = session.post(url, headers=headers, timeout=30, default_headers=False, **kwargs)
    except Exception as exc:
        log(f"warmup {name} fail {type(exc).__name__}")
        return
    log(f"warmup {name} {response.status_code}")


def _mixpanel_body(screen: list, device_id: str, hints: dict | None = None) -> str:
    now = int(time.time())
    width = int(screen[0]) if len(screen) > 0 else 1920
    height = int(screen[1]) if len(screen) > 1 else 1080
    meta = hints or {}
    browser_version = str(meta.get("fullVersion") or "145.0.7632.159").split(".", 1)[0]
    os_name = str(meta.get("platform") or "Windows")
    events = []
    for event in ("account_signup_flow_started", "page_viewed"):
        events.append(
            {
                "event": event,
                "properties": {
                    "token": MP_TOKEN,
                    "time": now,
                    "$browser": "Chrome",
                    "$browser_version": browser_version,
                    "$os": os_name,
                    "$current_url": SIGNUP_URL,
                    "$screen_width": width,
                    "$screen_height": height,
                    "$device_id": device_id,
                    "$lib_version": "2.75.0",
                    "mp_lib": "web",
                    "mp_sent_by_lib_version": "2.75.0",
                    "page_route": "sign-up",
                    "xai_app_name": "account",
                    "distinct_id": device_id,
                    "$insert_id": uuid.uuid4().hex[:16],
                },
            }
        )
    encoded = base64.b64encode(json.dumps(events, separators=(",", ":")).encode()).decode()
    return "data=" + encoded


def _rum_body() -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "startTime": now_ms,
        "pageloadId": str(uuid.uuid4()),
        "siteToken": "27a51fc7d1cd4058b900b2eea7bb7061",
        "eventType": 1,
        "location": SIGNUP_URL,
        "firstPaint": 180,
        "firstContentfulPaint": 240,
        "versions": {"fl": "2024.11.0", "js": "2026.10.1", "timings": 2},
        "timingsV2": {},
        "memory": {},
        "st": 1,
        "bi": 1,
        "nt": "",
    }


def _sentry_envelope(ua: str, deployment_id: str) -> str:
    sent = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    header = {
        "sent_at": sent,
        "sdk": {"name": "sentry.javascript.nextjs", "version": "10.68.0"},
        "dsn": SENTRY_DSN,
    }
    session = {
        "sid": uuid.uuid4().hex,
        "init": True,
        "started": sent,
        "timestamp": sent,
        "status": "ok",
        "errors": 0,
        "did": uuid.uuid4().hex,
        "attrs": {
            "release": f"account@{deployment_id}",
            "environment": "production",
            "user_agent": ua,
        },
    }
    return "\n".join(
        [
            json.dumps(header, separators=(",", ":")),
            json.dumps({"type": "session"}, separators=(",", ":")),
            json.dumps(session, separators=(",", ":")),
        ]
    )


def _warmup_before_send(
    session,
    *,
    ua: str,
    screen: list,
    html: str,
    log: Callable[[str], None],
    hints: dict | None = None,
) -> tuple[str, str]:
    """Replay the manual HAR's accounts.x.ai calls that happen before send-code."""
    deployment = _deployment_id(html)
    trace, baggage = _sentry_headers(deployment)
    page_headers = _client_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage, hints=hints)
    page_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    _quiet_get(session, CASTLE_SDK_URL, page_headers, log, "castle-sdk")
    _quiet_get(session, TURNSTILE_API_URL, page_headers, log, "turnstile-api")
    track = _mixpanel_body(screen, str(uuid.uuid4()), hints)
    track_headers = _client_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage, hints=hints)
    track_headers["Content-Type"] = "application/x-www-form-urlencoded"
    _quiet_post(session, MP_TRACK_URL, track_headers, log, "mp-track", data=track)
    metric_headers = {
        "Content-Type": "application/json",
        "Referer": SIGNUP_URL,
        "User-Agent": ua,
        **castle_sec_ch_headers(hints),
    }
    metrics_body = json.dumps(
        {
            "events": [
                {
                    "type": "navigation",
                    "path": "/sign-up",
                    "durationSeconds": 1.2,
                    "statusClass": "2xx",
                }
            ]
        },
        separators=(",", ":"),
    )
    _quiet_post(session, METRICS_URL, metric_headers, log, "metrics", data=metrics_body)
    rum_headers = _client_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage, hints=hints)
    rum_headers["Content-Type"] = "application/json"
    _quiet_post(session, RUM_URL, rum_headers, log, "rum", json=_rum_body())
    monitor_headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": SIGNUP_URL,
        "User-Agent": ua,
        **castle_sec_ch_headers(hints),
    }
    envelope = _sentry_envelope(ua, deployment)
    _quiet_post(session, MONITOR_URL, monitor_headers, log, "monitoring", data=envelope)
    _quiet_post(session, MONITOR_URL, monitor_headers, log, "monitoring-2", data=envelope)
    rsc_headers = _client_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage, hints=hints)
    rsc_headers.update(
        {
            "RSC": "1",
            "Next-Url": "/sign-up",
            "Next-Router-Prefetch": "1",
            "Next-Router-Segment-Prefetch": "/_tree",
            "x-deployment-id": deployment,
        }
    )
    _quiet_get(session, SIGNIN_URL, rsc_headers, log, "sign-in-rsc")
    _quiet_post(session, MP_TRACK_URL, track_headers, log, "mp-track-2", data=track)
    _quiet_post(session, METRICS_URL, metric_headers, log, "metrics-2", data=metrics_body)
    consent_headers = _client_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage, hints=hints)
    consent_headers["x-consent-retouch"] = ""
    _quiet_get(session, SIGNUP_URL, consent_headers, log, "sign-up-204")
    return trace, baggage


def _mint_castle(fp_path: Path, page_file: Path, token_path: Path, log: Callable[[str], None]) -> str:
    env = os.environ.copy()
    env["CASTLE_FP_FILE"] = str(fp_path)
    env["CASTLE_STYLE_FILE"] = str(DEFAULT_STYLE_FILE)
    env["CASTLE_PAGE_FILE"] = str(page_file)
    env["CASTLE_TOKEN_FILE"] = str(token_path)
    env["CASTLE_PYTHON"] = sys.executable
    proc = subprocess.run(
        ["node", str(MINT), str(page_file.with_suffix(".sdk.js"))],
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )
    for line in (proc.stdout or "").splitlines():
        if line.startswith(_SAFE_NODE_PREFIXES):
            log(line[:180])
    if proc.returncode != 0 or not token_path.is_file():
        detail = (proc.stderr or proc.stdout or "mint failed")[-180:]
        raise RuntimeError(f"castle mint failed {detail}")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("castle mint returned an empty token")
    return token


def _record_send_har(
    *,
    status: int,
    url: str,
    request_headers: dict[str, str],
    body: dict,
    response_text: str,
) -> None:
    """Write one send-code HAR when recording is on. The mailbox is not stored."""
    directory = str(os.environ.get("GROK_RECORD_HAR_DIR") or "").strip()
    if not directory:
        return
    stored = dict(body)
    stored.pop("email", None)
    kept_headers = []
    for name, value in request_headers.items():
        if str(name).lower() in {"cookie", "authorization", "proxy-authorization"}:
            continue
        kept_headers.append({"name": str(name), "value": str(value)})
    entry = {
        "startedDateTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "request": {
            "method": "POST",
            "url": url,
            "headers": kept_headers,
            "postData": {
                "mimeType": "application/json",
                "text": json.dumps(stored, ensure_ascii=False, separators=(",", ":")),
            },
        },
        "response": {
            "status": int(status),
            "content": {"mimeType": "application/json", "text": response_text[:500]},
        },
    }
    document = {
        "log": {
            "version": "1.2",
            "creator": {"name": "curl_cffi_signup", "version": "1"},
            "entries": [entry],
        }
    }
    dest = Path(directory)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"send-{int(status)}-{uuid.uuid4().hex[:8]}.har"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def signup_with_curl_cffi(
    *,
    proxy: str,
    email: str,
    wait_otp: Callable[[float], str],
    solve_ts: Callable[[str], str],
    log: Callable[[str], None],
    usage: Any = None,
) -> dict:
    """Send-code, verify, Turnstile, and create-account on one curl_cffi session."""
    work = Path(tempfile.mkdtemp(prefix="curl-cffi-signup-"))
    fp_path = work / "fp.json"
    page_file = work / "page.html"
    token_path = work / "token.txt"
    try:
        profile = write_castle_fingerprint(fp_path, proxy)
        ua = str(profile.get("user_agent") or "")
        hints = dict(profile.get("ua_metadata") or {})
        log(
            "fp "
            f"screen={'x'.join(str(n) for n in profile.get('screen') or [])} "
            f"tz={profile.get('timezone_id') or 'server'} "
            f"hw={profile.get('hardware_concurrency')}/{profile.get('device_memory')} "
            f"webgl={str(profile.get('webgl_renderer') or '')[:48]}"
        )
        session = make_protocol_session(
            proxy_url=proxy,
            impersonate=IMPERSONATE,
            headers={"User-Agent": ua, **castle_sec_ch_headers(hints)},
        )
        resp = session.get(
            SIGNUP_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": GROK_HOME_URL,
                "User-Agent": ua,
                **castle_sec_ch_headers(hints),
            },
            timeout=45,
            allow_redirects=True,
        )
        html = resp.text or ""
        page_file.write_text(html, encoding="utf-8")
        log(f"page status={resp.status_code} html_len={len(html)} pk={'yes' if 'pk_' in html else 'no'}")
        if resp.status_code >= 400 or "pk_" not in html:
            raise RuntimeError(f"signup page {resp.status_code} pk={'yes' if 'pk_' in html else 'no'}")
        screen = list(profile.get("screen") or [1920, 1080])
        trace, baggage = _warmup_before_send(
            session, ua=ua, screen=screen, html=html, log=log, hints=hints
        )
        token = _mint_castle(fp_path, page_file, token_path, log)
        sent_at = time.time()
        built = build_send_verification_code(email=email, castle_request_token=token)
        post = session.post(
            built["url"],
            json=built["json"],
            headers=_auth_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage),
            timeout=45,
        )
        data: Any = {}
        try:
            data = post.json()
        except Exception:
            data = {}
        err = ""
        if isinstance(data, dict):
            err = str(data.get("code") or data.get("error") or "")
        log(f"send-code status={post.status_code} err={err[:80]}")
        _record_send_har(
            status=int(post.status_code or 0),
            url=str(built["url"]),
            request_headers=_auth_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage),
            body=built["json"] if isinstance(built.get("json"), dict) else {},
            response_text=str(getattr(post, "text", "") or "")[:500],
        )
        if post.status_code >= 400 or post.status_code == 0:
            if err:
                raise_send_code_rejection(post.status_code, err, email)
            raise RuntimeError(f"send-code {post.status_code}")
        if usage is not None:
            usage.email_used = True
        code = str(wait_otp(sent_at) or "")
        log(f"otp digits={sum(ch.isdigit() for ch in code)}")
        verified = build_verify_email(email=email, code=code)
        vresp = session.post(
            verified["url"],
            json=verified["json"],
            headers=_auth_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage),
            timeout=45,
        )
        log(f"verify-email status={vresp.status_code}")
        if vresp.status_code >= 400 or vresp.status_code == 0:
            raise RuntimeError(f"verify-email {vresp.status_code}")
        if usage is not None:
            usage.remote_captcha_used = True
        ts_token = solve_ts("0x4AAAAAAAhr9JGVDZbrZOo0")
        log(f"turnstile len={len(ts_token or '')}")
        given = random.choice(FIRST)
        family = random.choice(LAST)
        password = _rand_password()
        created = build_create_account(
            email=email,
            password=password,
            given_name=given,
            family_name=family,
            email_validation_code=code,
            turnstile_token=ts_token,
            castle_request_token=token,
        )
        cresp = session.post(
            created["url"],
            json=created["json"],
            headers=_auth_headers(ua, SIGNUP_URL, trace=trace, baggage=baggage),
            timeout=45,
        )
        cdata: Any = {}
        try:
            cdata = cresp.json()
        except Exception:
            cdata = {}
        keys = list(cdata)[:8] if isinstance(cdata, dict) else []
        log(f"create-account status={cresp.status_code} keys={keys}")
        if cresp.status_code >= 400 or cresp.status_code == 0:
            raise RuntimeError(f"create-account {cresp.status_code} keys={keys}")
        if existing_email_payload(cdata):
            raise ExistingEmailError(email, cdata)
        sso = sso_from_response(cresp, cdata)
        redirect = str(cdata.get("redirectUrl") or "") if isinstance(cdata, dict) else ""
        if not sso and redirect:
            followed = session.get(
                redirect,
                headers={"User-Agent": ua, "Referer": SIGNUP_URL, **castle_sec_ch_headers(hints)},
                timeout=45,
                allow_redirects=True,
            )
            sso = sso_from_response(followed, cdata)
        if not sso:
            sso = sso_from_create_payload(cdata)
        if not sso:
            raise RuntimeError(f"no sso after create keys={keys}")
        log(f"sso len={len(sso)}")
        return {
            "email": email,
            "sso": sso,
            "password": password,
            "given_name": given,
            "family_name": family,
            "engine": "curl_cffi",
        }
    finally:
        for path in (fp_path, page_file, token_path):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            work.rmdir()
        except Exception:
            pass
