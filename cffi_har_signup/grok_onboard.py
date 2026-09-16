# -*- coding: utf-8 -*-
"""HTTP onboarding so a new xAI account can use chat (no browser).

New grok_build tokens get chat-endpoint 503 until birth date + ToS are set.
"""
from __future__ import annotations

import random
import struct
from typing import Any


def random_birth_date() -> str:
    year = random.randint(1984, 2000)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}T16:00:00.000Z"


def birth_date_payload(birth_date: str = "") -> dict[str, str]:
    value = str(birth_date or random_birth_date()).strip()
    return {"birthDate": value}


def tos_grpc_body() -> bytes:
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def attach_sso(session, sso: str) -> None:
    token = str(sso or "").strip()
    if not token:
        return
    for domain in (".x.ai", "accounts.x.ai", "auth.x.ai", ".grok.com", "grok.com"):
        try:
            session.cookies.set("sso", token, domain=domain)
            session.cookies.set("sso-rw", token, domain=domain)
        except Exception:
            session.cookies.set("sso", token)


def onboard_new_account(session, sso: str, timeout: float = 20.0) -> dict[str, Any]:
    """POST grok.com birth date + accounts.x.ai ToS. Returns HTTP statuses."""
    attach_sso(session, sso)
    out: dict[str, Any] = {"birth_status": 0, "tos_status": 0, "home_status": 0}
    try:
        home = session.get(
            "https://grok.com/",
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://accounts.x.ai/"},
            timeout=timeout,
            allow_redirects=True,
        )
        out["home_status"] = int(getattr(home, "status_code", 0) or 0)
    except Exception as exc:
        out["home_error"] = str(exc)[:160]
    try:
        birth = session.post(
            "https://grok.com/rest/auth/set-birth-date",
            json=birth_date_payload(),
            headers={
                "content-type": "application/json",
                "origin": "https://grok.com",
                "referer": "https://grok.com/",
            },
            timeout=timeout,
        )
        out["birth_status"] = int(getattr(birth, "status_code", 0) or 0)
        out["birth_body"] = str(getattr(birth, "text", "") or "")[:160]
    except Exception as exc:
        out["birth_error"] = str(exc)[:160]
    try:
        tos = session.post(
            "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion",
            data=tos_grpc_body(),
            headers={
                "content-type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "x-user-agent": "connect-es/2.1.1",
                "origin": "https://accounts.x.ai",
                "referer": "https://accounts.x.ai/accept-tos",
            },
            timeout=timeout,
        )
        out["tos_status"] = int(getattr(tos, "status_code", 0) or 0)
    except Exception as exc:
        out["tos_error"] = str(exc)[:160]
    return out
