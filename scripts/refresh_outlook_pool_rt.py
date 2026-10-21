#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refresh Outlook pool refresh tokens the way outlookEmail does.

Graph scopes are tried first. Accounts whose token is only valid for IMAP
fall through to the IMAP channel, which is what succeeded on 2026-10-21.
A rotated refresh token is written back into the inventory. Nothing here
prints the token or the mailbox password.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from email_providers.outlook_rt import (
    _claim_is_active,
    _claim_path,
    _load_used,
    load_inventory,
    update_inventory_credentials,
    used_path_for,
)

TOKEN_URL_GRAPH = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
TOKEN_URL_IMAP = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
TOKEN_URL_LIVE = "https://login.live.com/oauth20_token.srf"
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
GRAPH_EXPLICIT = (
    "https://graph.microsoft.com/Mail.Read "
    "https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/User.Read offline_access"
)
GRAPH_READ = (
    "https://graph.microsoft.com/Mail.Read "
    "https://graph.microsoft.com/User.Read offline_access"
)
GRAPH_DEFAULT = "https://graph.microsoft.com/.default offline_access"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
CHANNELS = (
    ("graph-explicit", TOKEN_URL_GRAPH, GRAPH_EXPLICIT),
    ("graph-read", TOKEN_URL_GRAPH, GRAPH_READ),
    ("graph-default", TOKEN_URL_GRAPH, GRAPH_DEFAULT),
    ("graph-original", TOKEN_URL_GRAPH, ""),
    ("imap", TOKEN_URL_IMAP, IMAP_SCOPE),
    ("live", TOKEN_URL_LIVE, ""),
    ("consumers-default", TOKEN_URL_IMAP, GRAPH_DEFAULT),
)
ERROR_MARKERS = (
    "AADSTS700082",
    "AADSTS700084",
    "AADSTS50173",
    "AADSTS70000",
    "invalid_grant",
)


def default_inventory() -> str:
    config = ROOT / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8") or "{}")
        path = str((data or {}).get("outlook_rt_inventory") or "").strip()
        if path:
            return path
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return str(ROOT / "2000outlook_b3.txt")


def default_used(inventory: str) -> str:
    config = ROOT / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8") or "{}")
        path = str((data or {}).get("outlook_rt_used_path") or "").strip()
        if path:
            return path
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return str(used_path_for(inventory, ""))


def short_error(payload: dict, status: int) -> str:
    code = str(payload.get("error") or f"HTTP {status}")[:40]
    description = str(payload.get("error_description") or "")
    haystack = f"{code} {description}".lower()
    for item in ERROR_MARKERS:
        if item.lower() in haystack:
            return item
    return code


def exchange(client_id: str, refresh_token: str, post=None) -> dict:
    """Return a status dict. The new refresh token stays out of the report."""
    send = post or requests.post
    last = {"ok": False, "error": "no-response", "channel": "", "status": 0}
    for label, url, scope in CHANNELS:
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if scope:
            data["scope"] = scope
        try:
            response = send(url, data=data, timeout=30)
        except requests.RequestException as exc:
            last = {"ok": False, "channel": label, "error": type(exc).__name__, "status": 0}
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if getattr(response, "status_code", 0) == 200 and payload.get("access_token"):
            new_rt = str(payload.get("refresh_token") or "").strip()
            scope_text = str(payload.get("scope") or "")
            return {
                "ok": True,
                "channel": label,
                "status": 200,
                "error": "",
                "rotated": bool(new_rt and new_rt != refresh_token),
                "new_refresh_token": new_rt or refresh_token,
                "access_len": len(str(payload.get("access_token") or "")),
                "expires_in": payload.get("expires_in"),
                "mail_scope": "Mail.Read" in scope_text or "IMAP" in scope_text,
            }
        last = {
            "ok": False,
            "channel": label,
            "status": getattr(response, "status_code", 0),
            "error": short_error(payload if isinstance(payload, dict) else {}, getattr(response, "status_code", 0)),
        }
    return last


def pick_accounts(inventory: str, used_path: str, emails: list[str], limit: int) -> list[dict]:
    wanted = {item.strip().lower() for item in emails if item.strip()}
    used = _load_used(used_path_for(inventory, used_path))
    now = time.time()
    chosen = []
    rows = load_inventory(inventory)
    if not wanted:
        rows = list(reversed(rows))
    for account in rows:
        email = account["email"]
        if wanted and email.lower() not in wanted:
            continue
        if not wanted and email.lower() in used:
            continue
        if not wanted and _claim_is_active(_claim_path(inventory, email), now):
            continue
        if not account.get("refresh_token"):
            continue
        chosen.append(account)
        if not wanted and len(chosen) >= limit:
            break
    return chosen


def public_report(email: str, old_len: int, result: dict, saved: bool) -> dict:
    return {
        "email": email,
        "ok": bool(result.get("ok")),
        "channel": result.get("channel", ""),
        "status": result.get("status"),
        "error": result.get("error", ""),
        "rotated": bool(result.get("rotated")),
        "saved": saved,
        "old_rt_len": old_len,
        "new_rt_len": len(result.get("new_refresh_token") or ""),
        "access_len": result.get("access_len", 0),
        "expires_in": result.get("expires_in"),
        "mail_scope": result.get("mail_scope", False),
    }


def refresh_accounts(inventory: str, accounts: list[dict], post=None) -> list[dict]:
    reports = []
    for account in accounts:
        result = exchange(account.get("client_id") or DEFAULT_CLIENT_ID, account["refresh_token"], post=post)
        saved = False
        if result.get("ok") and result.get("rotated"):
            saved = update_inventory_credentials(
                inventory,
                account["email"],
                refresh_token=result["new_refresh_token"],
                client_id=account.get("client_id") or "",
            )
        reports.append(public_report(account["email"], len(account["refresh_token"]), result, saved))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Outlook pool refresh tokens")
    parser.add_argument("--inventory", default="")
    parser.add_argument("--used", default="")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--email", action="append", default=[])
    args = parser.parse_args(argv)
    inventory = args.inventory or default_inventory()
    used = args.used or default_used(inventory)
    accounts = pick_accounts(inventory, used, args.email, max(1, args.limit))
    print(f"picked {len(accounts)}", flush=True)
    reports = refresh_accounts(inventory, accounts)
    for report in reports:
        print(json.dumps(report, ensure_ascii=True), flush=True)
    if not reports:
        return 1
    return 0 if all(item["ok"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
