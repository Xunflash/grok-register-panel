from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import requests


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def jwt_claims(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("invalid_access_token")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))


class Admin:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.local = threading.local()

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.trust_env = False
            self.local.token = ""
        return self.local.session

    def login(self, force=False):
        session = self.session()
        if self.local.token and not force:
            return self.local.token
        response = session.post(
            self.base_url + "/api/admin/v1/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=20,
        )
        response.raise_for_status()
        self.local.token = response.json()["data"]["tokens"]["accessToken"]
        return self.local.token

    def request(self, method, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        for attempt in range(2):
            headers["Authorization"] = "Bearer " + self.login()
            response = self.session().request(
                method,
                self.base_url + "/api/admin/v1" + path,
                headers=headers,
                timeout=kwargs.pop("timeout", 120),
                **kwargs,
            )
            if response.status_code == 401 and attempt == 0:
                response.close()
                self.login(force=True)
                continue
            return response
        raise RuntimeError("admin_unauthorized")

    def accounts(self):
        values = []
        for page in range(1, 101):
            response = self.request(
                "GET", "/accounts", params={"provider": "grok_build", "page": page, "pageSize": 500}
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data", payload)
            items = data["items"]
            values.extend(items)
            if len(items) < int(data.get("pageSize") or 500):
                return values
        raise RuntimeError("account_pagination_limit")

    def import_refresh_token(self, account, token):
        claims = jwt_claims(str(token.get("access_token") or ""))
        user_id = str(claims.get("sub") or claims.get("principal_id") or "")
        team_id = str(claims.get("team_id") or "")
        email = str(account.get("email") or "").strip()
        if not user_id or user_id != str(account.get("userId") or ""):
            raise ValueError("token_user_identity_mismatch")
        if team_id != str(account.get("teamId") or ""):
            raise ValueError("token_team_identity_mismatch")
        claim_email = str(claims.get("email") or "").strip().lower()
        if claim_email and claim_email != email.lower():
            raise ValueError("token_email_identity_mismatch")
        refresh = str(token.get("refresh_token") or "").strip()
        if not refresh:
            raise ValueError("missing_refresh_token")
        document = {
            "provider": "grok_build",
            "accounts": [{
                "name": str(account.get("name") or email),
                "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
                "access_token": str(token.get("access_token") or ""),
                "refresh_token": refresh,
                "token_type": str(token.get("token_type") or "Bearer"),
                "email": email,
                "user_id": user_id,
                "team_id": team_id,
            }],
        }
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        response = self.request(
            "POST",
            "/accounts/import",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            files={"files": ("reauth.json", body, "application/json")},
            timeout=300,
        )
        if response.status_code != 200:
            status = response.status_code
            response.close()
            raise RuntimeError("import_http_" + str(status))
        event = "message"
        data_lines = []
        completed = None
        try:
            for raw in response.iter_lines():
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                line = raw.strip() if raw else ""
                if not line:
                    if data_lines and event == "complete":
                        completed = json.loads("\n".join(data_lines))
                    event, data_lines = "message", []
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            if data_lines and event == "complete":
                completed = json.loads("\n".join(data_lines))
        finally:
            response.close()
        if completed is None:
            raise RuntimeError("import_complete_event_missing")
        expected = str(account["id"])
        created = int(completed.get("created") or 0)
        updated = int(completed.get("updated") or 0)
        failed = int(completed.get("failed") or 0)
        if created or updated > 1:
            raise RuntimeError("import_did_not_update_exact_original_account")
        if updated != 1 or failed:
            raise RuntimeError("import_counts_c{}_u{}_f{}".format(created, updated, failed))
        check = self.request("GET", "/accounts/" + expected)
        check.raise_for_status()
        payload = check.json()
        stored = payload.get("data", payload)
        if (
            str(stored.get("id")) != expected
            or stored.get("provider") != "grok_build"
            or str(stored.get("email") or "").strip().lower() != email.lower()
            or str(stored.get("userId") or "") != user_id
            or str(stored.get("teamId") or "") != team_id
            or stored.get("authStatus") != "active"
            or bool(stored.get("enabled")) != bool(account.get("enabled"))
        ):
            raise RuntimeError("post_import_account_verification_failed")
        return {
            "id": expected,
            "status": "refreshed",
            "updated": updated,
            "failed": failed,
            "sync_failed": int(completed.get("syncFailed") or 0),
            "finished_at": time.time(),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--receipts", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    config = read_json(args.config)
    admin = Admin(args.base_url, config["grok2api_remote_username"], config["grok2api_remote_password"])
    manifest = read_json(args.source / "manifest.json")
    ids = [str(row["id"]) for row in manifest if row.get("status") == "reauthorized"]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate_source_account_ids")
    accounts = {str(row["id"]): row for row in admin.accounts()}
    work = []
    for account_id in ids:
        path = args.source / ("token-" + account_id + ".json")
        if not path.is_file():
            continue
        account = accounts.get(account_id)
        if not account or account.get("provider") != "grok_build":
            raise RuntimeError("target_account_missing_or_wrong_provider:" + account_id)
        work.append((account, path))
    prior = read_json(args.receipts) if args.receipts.exists() else []
    done = {str(row["id"]) for row in prior}
    work = [(account, path) for account, path in work if str(account["id"]) not in done]
    if args.limit:
        work = work[:args.limit]
    print(json.dumps({"queued": len(work), "already_refreshed": len(done), "workers": args.workers}), flush=True)
    lock = threading.Lock()
    stop = threading.Event()
    receipts = list(prior)

    def save(result):
        with lock:
            receipts.append(result)
            write_json(args.receipts, receipts)
            if len(receipts) <= 3 or len(receipts) % 25 == 0 or result.get("status") == "fatal":
                print(json.dumps({"processed": len(receipts), "status": result.get("status"), "id": result.get("id")}), flush=True)

    def run(item):
        account, path = item
        account_id = str(account["id"])
        if stop.is_set():
            return
        try:
            token = read_json(path)
            result = admin.import_refresh_token(account, token)
            path.unlink(missing_ok=True)
        except Exception as error:
            error_code = str(error)
            status_code = getattr(getattr(error, "response", None), "status_code", None)
            fatal = error_code in {
                "import_did_not_update_exact_original_account",
                "post_import_account_verification_failed",
            } or status_code in {401, 403, 404, 500, 502, 503, 504} or isinstance(error, requests.RequestException)
            if error_code.startswith("import_counts_"):
                fatal = False
            result = {
                "id": account_id,
                "status": "fatal" if fatal else "error",
                "error_code": error_code if error_code.startswith("import_counts_") or error_code in {
                    "invalid_access_token", "token_user_identity_mismatch", "token_team_identity_mismatch",
                    "token_email_identity_mismatch", "missing_refresh_token", "import_complete_event_missing",
                    "import_did_not_update_exact_original_account", "post_import_account_verification_failed",
                } else "request_failed",
                "error_type": type(error).__name__,
                "http_status": status_code,
                "finished_at": time.time(),
            }
            if fatal:
                stop.set()
        save(result)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(run, work))
    counts = collections.Counter(row.get("status") for row in receipts)
    print(json.dumps({"receipts": len(receipts), "results": dict(counts), "remaining_source_tokens": len(list(args.source.glob("token-*.json")))}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
