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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def resin_outlook_proxies(count: int) -> list[str]:
    """One new sticky per worker on the Resin outlook platform."""
    config = ROOT / "config.json"
    data = json.loads(config.read_text(encoding="utf-8") or "{}")
    base = str((data or {}).get("proxy") or "").strip()
    platform = str((data or {}).get("outlook_rt_resin_platform") or "outlook").strip() or "outlook"
    if not base:
        raise SystemExit("config proxy is empty")
    from resin_sticky import with_resin_platform, with_resin_sticky_account

    swapped = with_resin_platform(base, platform)
    stamp = int(time.time())
    return [
        with_resin_sticky_account(swapped, f"rt{stamp}{index:02d}", platform)
        for index in range(max(1, count))
    ]


def exchange(client_id: str, refresh_token: str, post=None, proxies=None) -> dict:
    """Return a status dict. The new refresh token stays out of the report."""
    send = post or requests.post
    last = {"ok": False, "error": "no-response", "channel": "", "status": 0}
    skip_graph = False
    for label, url, scope in CHANNELS:
        if skip_graph and label.startswith("graph"):
            continue
        data = {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if scope:
            data["scope"] = scope
        try:
            response = send(url, data=data, timeout=30, proxies=proxies)
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
        if last["error"] == "AADSTS70000" and label.startswith("graph"):
            skip_graph = True
    return last


def pick_accounts(
    inventory: str,
    used_path: str,
    emails: list[str],
    limit: int,
    include_used: bool = False,
) -> list[dict]:
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
        if not wanted and not include_used and email.lower() in used:
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


def _refresh_one(inventory: str, account: dict, post=None, proxies=None) -> dict:
    result = exchange(
        account.get("client_id") or DEFAULT_CLIENT_ID,
        account["refresh_token"],
        post=post,
        proxies=proxies,
    )
    saved = False
    if result.get("ok") and result.get("rotated"):
        saved = update_inventory_credentials(
            inventory,
            account["email"],
            refresh_token=result["new_refresh_token"],
            client_id=account.get("client_id") or "",
        )
    return public_report(account["email"], len(account["refresh_token"]), result, saved)


def refresh_accounts(
    inventory: str,
    accounts: list[dict],
    post=None,
    proxies=None,
    workers: int = 1,
    proxy_urls: list[str] | None = None,
    on_report=None,
    should_stop=None,
) -> list[dict]:
    def run(item: tuple[int, dict]) -> dict | None:
        if should_stop and should_stop():
            return None
        index, account = item
        chosen = proxies
        if proxy_urls:
            url = proxy_urls[index % len(proxy_urls)]
            chosen = {"http": url, "https": url}
        return _refresh_one(inventory, account, post=post, proxies=chosen)

    def collect(report: dict | None, reports: list[dict]) -> None:
        if not report:
            return
        reports.append(report)
        if on_report:
            on_report(report)

    jobs = list(enumerate(accounts))
    reports: list[dict] = []
    if workers <= 1 or len(jobs) <= 1:
        for item in jobs:
            collect(run(item), reports)
        return reports
    with ThreadPoolExecutor(max_workers=workers) as pool:
        if on_report is None and should_stop is None:
            return list(pool.map(run, jobs))
        futures = [pool.submit(run, item) for item in jobs]
        for future in as_completed(futures):
            collect(future.result(), reports)
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Outlook pool refresh tokens")
    parser.add_argument("--inventory", default="")
    parser.add_argument("--used", default="")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--proxy", default="", help="resin uses the outlook platform sticky")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    inventory = args.inventory or default_inventory()
    used = args.used or default_used(inventory)
    workers = max(1, args.workers)
    proxies = None
    proxy_urls = None
    if args.proxy == "resin":
        proxy_urls = resin_outlook_proxies(workers)
        print(f"proxy=resin-outlook workers={workers}", flush=True)
    elif args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}
        print(f"proxy=custom workers={workers}", flush=True)
    accounts = pick_accounts(inventory, used, args.email, max(1, args.limit))
    print(f"picked {len(accounts)}", flush=True)
    reports = refresh_accounts(
        inventory,
        accounts,
        proxies=proxies,
        workers=workers,
        proxy_urls=proxy_urls,
    )
    counts: dict[str, int] = {}
    for report in reports:
        key = "ok:" + str(report.get("channel") or "") if report["ok"] else "fail:" + str(report.get("error") or "")
        counts[key] = counts.get(key, 0) + 1
        print(json.dumps(report, ensure_ascii=True), flush=True)
    print(
        "summary "
        + json.dumps(
            {
                "picked": len(reports),
                "ok": sum(1 for item in reports if item["ok"]),
                "rotated": sum(1 for item in reports if item["rotated"]),
                "saved": sum(1 for item in reports if item["saved"]),
                "counts": counts,
            },
            ensure_ascii=True,
        ),
        flush=True,
    )
    if not reports:
        return 1
    return 0 if all(item["ok"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
