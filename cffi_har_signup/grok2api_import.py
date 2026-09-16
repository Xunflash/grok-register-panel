# -*- coding: utf-8 -*-
"""Import a newly created account into grok2api so Guard can resolve it.

Guard looks up `provider=grok_build` by email. Signup itself stays HAR REST.
SSO is converted with protocol Device Flow (HTTP, no browser) only for import.
"""
from __future__ import annotations

import base64
import json
import sys
import uuid
from email.generator import BytesGenerator
from pathlib import Path
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GROK_BUILD_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
IMPORT_PATHS = {
    "grok_build": "/api/admin/v1/accounts/import",
    "grok_web": "/api/admin/v1/accounts/web/import",
}
PANEL_SSO = Path("/data/claw-relocated/grok-register-panel")


def sso_subject(sso: str) -> str:
    raw = str(sso or "").strip()
    parts = raw.split(".")
    if len(parts) < 2:
        return ""
    pad = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(pad.encode("ascii")))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("sub", "principal_id", "user_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def build_import_document(
    email: str,
    access_token: str,
    refresh_token: str = "",
    user_id: str = "",
    client_id: str = "",
) -> dict[str, Any]:
    """grok_build document Guard looks up via provider=grok_build search=email."""
    mail = str(email or "").strip()
    access = str(access_token or "").strip()
    uid = str(user_id or "").strip() or sso_subject(access) or mail
    return {
        "accounts": [
            {
                "provider": "grok_build",
                "name": "cffiiso " + mail,
                "client_id": str(client_id or GROK_BUILD_CLIENT_ID).strip(),
                "access_token": access,
                "refresh_token": str(refresh_token or "").strip(),
                "id_token": "",
                "token_type": "Bearer",
                "scope": "",
                "expires_at": "",
                "expires_in": 0,
                "email": mail,
                "sub": "",
                "user_id": uid,
                "principal_id": "",
                "team_id": "",
            }
        ]
    }


def convert_sso_to_token(sso: str, proxy: str = "", log=None) -> dict[str, Any]:
    """SSO cookie → grok_build access_token via protocol Device Flow (no browser)."""
    if not (PANEL_SSO / "sso_to_auth_json.py").is_file():
        raise RuntimeError("sso_to_auth_json.py missing")
    if str(PANEL_SSO) not in sys.path:
        sys.path.insert(0, str(PANEL_SSO))
    from sso_to_auth_json import sso_to_token_device_flow  # noqa: WPS433

    sink = log if callable(log) else (lambda *_a, **_k: None)
    token = sso_to_token_device_flow(sso, proxy=proxy, log=sink)
    if not isinstance(token, dict) or not str(token.get("access_token") or "").strip():
        raise RuntimeError("device flow produced no access_token")
    return token


def web_import_document(email: str, sso: str, user_id: str = "") -> dict[str, Any]:
    mail = str(email or "").strip()
    token = str(sso or "").strip()
    uid = str(user_id or "").strip() or sso_subject(token) or mail
    return {
        "provider": "grok_web",
        "accounts": [
            {
                "name": "cffiiso " + mail,
                "email": mail,
                "user_id": uid,
                "sso_token": token,
                "token": "",
                "tier": "basic",
                "cloudflare_cookies": "",
            }
        ],
    }


def _json_post(url: str, payload: dict, headers: dict | None = None, timeout: float = 20.0) -> dict:
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} {url}: {raw}") from exc


def login(base_url: str, username: str, password: str, timeout: float = 20.0) -> str:
    data = _json_post(
        str(base_url).rstrip("/") + "/api/admin/v1/auth/login",
        {"username": username, "password": password},
        timeout=timeout,
    )
    try:
        token = str(data["data"]["tokens"]["accessToken"] or "").strip()
    except (KeyError, TypeError) as exc:
        raise RuntimeError("grok2api login missing accessToken") from exc
    if not token:
        raise RuntimeError("grok2api login empty accessToken")
    return token


def _multipart_json_file(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    outer = MIMEMultipart("form-data", boundary="cffi" + uuid.uuid4().hex)
    part = MIMEApplication(content, _subtype="json")
    part.add_header("Content-Disposition", "form-data", name=field, filename=filename)
    outer.attach(part)
    buf = BytesIO()
    gen = BytesGenerator(buf, mangle_from_=False)
    gen.flatten(outer)
    body = buf.getvalue()
    content_type = outer.get("Content-Type")
    return body, content_type


def _iter_sse(raw: str):
    event = "message"
    data_lines: list[str] = []
    for line in str(raw or "").splitlines():
        if not line:
            if data_lines:
                blob = "\n".join(data_lines)
                try:
                    payload = json.loads(blob)
                except json.JSONDecodeError:
                    payload = {"message": blob}
                yield event, payload if isinstance(payload, dict) else {"data": payload}
            event, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        blob = "\n".join(data_lines)
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError:
            payload = {"message": blob}
        yield event, payload if isinstance(payload, dict) else {"data": payload}


def import_document(
    document: dict[str, Any],
    *,
    base_url: str,
    username: str,
    password: str,
    format_name: str = "grok_build",
    timeout: float = 120.0,
) -> dict[str, Any]:
    path = IMPORT_PATHS.get(str(format_name or "grok_build"))
    if not path:
        raise RuntimeError(f"unknown grok2api import format {format_name}")
    token = login(base_url, username, password)
    content = json.dumps(document, ensure_ascii=False).encode("utf-8")
    body, content_type = _multipart_json_file("files", "cffiiso-grok-build.json", content)
    url = str(base_url).rstrip("/") + path
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + token,
            "Content-Type": content_type,
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"grok2api import HTTP {exc.code}: {raw}") from exc
    except URLError as exc:
        raise RuntimeError(f"grok2api import failed: {exc}") from exc
    completed = None
    for event, payload in _iter_sse(raw):
        if event == "error":
            raise RuntimeError(str(payload.get("message") or payload.get("code") or payload))
        if event == "complete":
            completed = payload
    if completed is None:
        raise RuntimeError("grok2api import missing complete event: " + raw[:240])
    return completed


def import_web_sso(
    email: str,
    sso: str,
    *,
    base_url: str,
    username: str,
    password: str,
    timeout: float = 120.0,
) -> dict[str, Any]:
    return import_document(
        web_import_document(email, sso),
        base_url=base_url,
        username=username,
        password=password,
        format_name="grok_web",
        timeout=timeout,
    )


def import_build_sso(
    email: str,
    sso: str,
    *,
    base_url: str,
    username: str,
    password: str,
    proxy: str = "",
    timeout: float = 120.0,
    log=None,
) -> dict[str, Any]:
    token = convert_sso_to_token(sso, proxy=proxy, log=log)
    access = str(token.get("access_token") or "").strip()
    if len(access) < 40:
        raise RuntimeError(f"device flow access_token too short len={len(access)}")
    document = build_import_document(
        email,
        access,
        refresh_token=str(token.get("refresh_token") or ""),
        user_id=sso_subject(access),
    )
    # Use the panel grok2api client (curl_cffi multipart). urllib MIME import
    # returns 导入账号失败 against this server.
    if str(PANEL_SSO) not in sys.path:
        sys.path.insert(0, str(PANEL_SSO))
    from grok2api_client import Grok2APIClient  # noqa: WPS433

    tmp_dir = Path("/data/claw-relocated/cffi-har-signup/tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / ("cffiiso-" + uuid.uuid4().hex + ".json")
    tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        with Grok2APIClient(
            base_url,
            username,
            password,
            import_timeout=timeout,
        ) as client:
            return client.import_auth_file(tmp, format_name="grok_build")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
