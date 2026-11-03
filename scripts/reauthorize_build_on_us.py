from __future__ import annotations

import argparse
import collections
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from import_build_reauth_us import Admin, read_json, write_json


def guard_accounts() -> dict:
    query = '''import json,sqlite3
db=sqlite3.connect("file:/var/lib/account-risk-guard/state.db?mode=ro",uri=True,timeout=15)
db.row_factory=sqlite3.Row
print(json.dumps([dict(row) for row in db.execute("SELECT account_id,managed_quarantine FROM accounts")]))
'''
    rows = json.loads(subprocess.check_output([
        "docker", "exec", "grok2api-account-risk-guard", "python", "-c", query
    ], text=True, timeout=45))
    return {str(row["account_id"]): row for row in rows}


def sticky_proxy(base: str, platform: str, account_id: str) -> str:
    parsed = urlsplit(base)
    if not parsed.hostname or not parsed.username or not parsed.password:
        raise RuntimeError("resin_proxy_auth_missing")
    configured_platform = unquote(parsed.username).split(".", 1)[0]
    selected_platform = platform.strip() or configured_platform or "Default"
    username = quote(selected_platform + ".reauth-" + account_id, safe="-._~")
    password = quote(unquote(parsed.password), safe="")
    netloc = f"{username}:{password}@127.0.0.1:{parsed.port or 2260}"
    return urlunsplit((parsed.scheme or "http", netloc, "", "", ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    os.umask(0o077)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sso_to_auth_json import (
        decode_jwt_payload,
        inspect_token_bundle_bfs,
        sso_to_token,
        token_to_grok2api_account,
    )

    config = read_json(args.config)
    admin = Admin(args.base_url, config["grok2api_remote_username"], config["grok2api_remote_password"])
    source_rows = read_json(args.source / "sso-manifest.json")
    source = {str(row["id"]): row for row in source_rows}
    accounts = {str(row["id"]): row for row in admin.accounts()}
    quarantines = guard_accounts()
    prior = read_json(args.receipts) if args.receipts.exists() else []
    done = {str(row["id"]) for row in prior}
    candidates = []
    skipped = collections.Counter()
    for account_id, row in source.items():
        account = accounts.get(account_id)
        if not account or account.get("provider") != "grok_build":
            skipped["missing_or_wrong_provider"] += 1
        elif account.get("authStatus") == "active":
            skipped["already_active"] += 1
        elif not account.get("enabled"):
            skipped["disabled"] += 1
        elif quarantines.get(account_id, {}).get("managed_quarantine"):
            skipped["managed_quarantine"] += 1
        elif account_id in done:
            skipped["already_processed"] += 1
        else:
            candidates.append((account, row))
    if args.limit:
        candidates = candidates[:args.limit]
    print(json.dumps({"candidates": len(candidates), "skipped": dict(skipped), "workers": args.workers}), flush=True)

    lock = threading.Lock()
    stop = threading.Event()
    receipts = list(prior)

    def save(result):
        with lock:
            receipts.append(result)
            write_json(args.receipts, receipts)
            if len(receipts) <= 3 or len(receipts) % 10 == 0 or result.get("status") == "fatal":
                print(json.dumps({"processed": len(receipts), "id": result.get("id"), "status": result.get("status")}), flush=True)

    def process(item):
        account, source_row = item
        account_id = str(account["id"])
        if stop.is_set():
            return
        result = {"id": account_id, "started_at": time.time()}
        try:
            messages = []
            proxy = sticky_proxy(str(config.get("proxy") or ""), str(config.get("resin_platform") or "Default"), account_id)
            token = sso_to_token(
                str(source_row["sso"]), proxy=proxy, prefer="auth_code", allow_fallback=False,
                log=lambda message: messages.append(str(message)),
            )
            if not token or not token.get("access_token") or not token.get("refresh_token"):
                result["status"] = "authorization_failed"
                result["oauth_error_count"] = len(messages)
                return
            claims = decode_jwt_payload(token["access_token"])
            identity = str(claims.get("sub") or claims.get("principal_id") or "")
            if identity != str(account.get("userId") or ""):
                result["status"] = "token_identity_mismatch"
                return
            if str(claims.get("team_id") or "") != str(account.get("teamId") or ""):
                result["status"] = "token_team_mismatch"
                return
            email = str(account.get("email") or source_row.get("email") or "")
            metadata = inspect_token_bundle_bfs(
                access_token=token["access_token"], sso=str(source_row["sso"]),
                id_token=token.get("id_token", ""), refresh_token=token.get("refresh_token", ""),
            )
            if metadata.get("has_bfs"):
                result["status"] = "new_token_bot_flagged"
                return
            document = token_to_grok2api_account(token, email)
            token_for_import = {
                "access_token": document["access_token"],
                "refresh_token": document["refresh_token"],
                "token_type": document["token_type"],
            }
            imported = admin.import_refresh_token(account, token_for_import)
            result.update(imported)
            result["status"] = "reauthorized"
        except Exception as error:
            code = str(error)
            fatal = code in {
                "import_did_not_update_exact_original_account",
                "post_import_account_verification_failed",
            } or isinstance(error, Exception) and type(error).__name__ in {"ConnectionError", "Timeout"}
            result.update({
                "status": "fatal" if fatal else "error",
                "error_code": code if code in {
                    "token_user_identity_mismatch", "token_team_identity_mismatch", "missing_refresh_token",
                    "import_complete_event_missing", "post_import_account_verification_failed",
                    "import_did_not_update_exact_original_account",
                } else "request_failed",
                "error_type": type(error).__name__,
            })
            if fatal:
                stop.set()
        finally:
            if result.get("status"):
                result["finished_at"] = time.time()
                save(result)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(process, candidates))
    counts = collections.Counter(row.get("status") for row in receipts)
    print(json.dumps({"receipts": len(receipts), "results": dict(counts)}), flush=True)


if __name__ == "__main__":
    main()
