# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
CODE_RE = re.compile(r"\b([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\b")
_FALSE_CODE = {"per-100", "per-1k", "utf-8"}


def parse_line(line: str) -> dict[str, str]:
    parts = [p.strip() for p in line.strip().split("----")]
    if len(parts) >= 4:
        return {
            "email": parts[0],
            "password": parts[1],
            "client_id": parts[2] or DEFAULT_CLIENT_ID,
            "refresh_token": "----".join(parts[3:]),
        }
    if len(parts) == 3:
        return {
            "email": parts[0],
            "client_id": parts[1] or DEFAULT_CLIENT_ID,
            "refresh_token": parts[2],
        }
    if len(parts) == 2:
        return {
            "email": parts[0],
            "client_id": DEFAULT_CLIENT_ID,
            "refresh_token": parts[1],
        }
    raise ValueError(f"bad outlook line: {line[:60]!r}")


def load_inventory(path: Path) -> list[dict[str, str]]:
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        rows.append(parse_line(raw))
    return rows


def _form(url: str, data: dict, timeout: float = 30.0) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code} {url}: {raw}") from exc


def _get(url: str, token: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_access(account: dict[str, str]) -> str:
    payload = {
        "client_id": account.get("client_id") or DEFAULT_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": account["refresh_token"],
        "scope": "https://graph.microsoft.com/.default offline_access",
    }
    data = _form(TOKEN_URL, payload)
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"outlook refresh failed: {data}")
    if data.get("refresh_token"):
        account["refresh_token"] = str(data["refresh_token"])
    return token


def _normalize_code(raw: str) -> str:
    return str(raw or "").strip().upper()


def _is_plausible_xai_code(raw: str, allow_numeric: bool = False) -> bool:
    cand = _normalize_code(raw)
    if not CODE_RE.fullmatch(cand) and not re.fullmatch(r"[A-Z0-9]{3}-[A-Z0-9]{3}", cand):
        return False
    if cand.lower() in {x.lower() for x in _FALSE_CODE}:
        return False
    left, right = cand.split("-", 1)
    if not allow_numeric and left.isdigit() and right.isdigit():
        return False
    return True


def apply_live_otp(built: dict, code: str, field: str) -> dict:
    """Keep live XXX-YYY OTP values; HAR fixture used 6-digit digits-only."""
    payload = dict(built)
    body = dict(payload.get("json") or {})
    live = str(code or "").strip()
    if "-" in live:
        body[field] = live
    payload["json"] = body
    return payload


def extract_code(text: str, subject: str = "") -> str:
    subject = subject or ""
    text = text or ""
    m = re.search(r"^([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\s+xAI\b", subject, re.I)
    if m and _is_plausible_xai_code(m.group(1), allow_numeric=True):
        return _normalize_code(m.group(1))
    hay = f"{subject}\n{text}"
    for pat in (
        r"(?:spacexai|x\.?ai)\s+confirmation\s+code[:\s]+([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\b",
        r"(?:verification|security|confirm(?:ation)?)\s+code[:\s]+([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\b",
        r"\b([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\b\s*xAI\b",
        r"\bxAI\b[^\n]{0,48}\b([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\b",
    ):
        m = re.search(pat, hay, re.I)
        if m and _is_plausible_xai_code(m.group(1), allow_numeric=True):
            return _normalize_code(m.group(1))
    allow_numeric = any(
        k in hay.lower() for k in ("spacexai", "confirmation code", "noreply@" + "x.ai", "x.ai")
    )
    if allow_numeric:
        for m in CODE_RE.finditer(hay):
            if _is_plausible_xai_code(m.group(1), allow_numeric=True):
                return _normalize_code(m.group(1))
    return ""


def _msg_received_epoch(raw: str) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def code_from_messages(messages, after_ts: float = 0.0, seen: Optional[set[str]] = None) -> str:
    """Pick a hyphenated xAI OTP from Graph message dicts, ignoring older mail."""
    min_ts = (after_ts - 15.0) if after_ts else 0.0
    seen_ids = seen if seen is not None else set()
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        mid = str(msg.get("id") or "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        received = _msg_received_epoch(str(msg.get("receivedDateTime") or ""))
        if min_ts and received and received < min_ts:
            continue
        subject = str(msg.get("subject") or "")
        hay = " ".join(
            [
                subject,
                str(msg.get("bodyPreview") or ""),
                str((msg.get("body") or {}).get("content") or ""),
            ]
        )
        code = extract_code(hay, subject=subject)
        if code:
            return code
    return ""


def wait_for_code(
    account: dict[str, str],
    timeout: float = 180.0,
    poll: float = 5.0,
    after_ts: float = 0.0,
) -> str:
    deadline = time.time() + timeout
    seen: set[str] = set()
    while time.time() < deadline:
        access = refresh_access(account)
        query = urllib.parse.urlencode(
            {
                "$top": "12",
                "$select": "subject,body,bodyPreview,receivedDateTime",
                "$orderby": "receivedDateTime desc",
            }
        )
        url = GRAPH + "/me/mailFolders/inbox/messages?" + query
        data = _get(url, access)
        code = code_from_messages(data.get("value") or [], after_ts=after_ts, seen=seen)
        if code:
            return code
        time.sleep(poll)
    raise TimeoutError(f"no xAI code for {account.get('email')}")
