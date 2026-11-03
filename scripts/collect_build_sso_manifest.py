#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect saved Build account SSO records into an sso-manifest.json.

The registration panel writes one file per account to ``accounts/<email>.txt``
after SSO capture::

    <email>----<grok password>-----<sso cookie>

Those files live where registration ran.  After the registration host moved
from the local machine to Claw, the SSO records for accounts imported into
grok2api stayed under the Claw panel data directory
(``/data/claw-relocated/grok-register-panel/accounts``).  This tool maps the
accounts that grok2api reports as ``reauthRequired`` back to those files and
writes the ``sso-manifest.json`` consumed by
``scripts/reauthorize_build_on_us.py``.

It never prints passwords or SSO cookies; only coverage counts are printed.

Usage (run on a host that can read the panel accounts directory)::

    # accounts.json is the grok2api admin account list (id/email), e.g.
    # {"data": {"items": [{"id": 123, "email": "...", "authStatus": ...}]}}
    python3 scripts/collect_build_sso_manifest.py \
        --accounts-dir /data/claw-relocated/grok-register-panel/accounts \
        --accounts-json accounts.json \
        --also /data/claw-relocated/releases/panel-18a85b0/accounts \
        --out /tmp/build-sso

    # on the US host, after copying the directory:
    python3 scripts/reauthorize_build_on_us.py \
        --source /tmp/build-sso --config config.json \
        --receipts /tmp/reauth-receipts.json --workers 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable


def _emails_from_admin(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            rows = data.get("items") or data.get("accounts") or []
        else:
            rows = data
    else:
        rows = payload
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: expected a list of accounts or data.items")

    wanted: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("authStatus") or row.get("auth_status") or "").strip()
        if status and status != "reauthRequired":
            continue
        email = str(row.get("email") or "").strip()
        account_id = row.get("id")
        if email and account_id is not None:
            wanted.append({"id": str(account_id), "email": email})
    return wanted


def _parse_account_file(path: Path) -> str:
    """Return the sso cookie from an ``accounts/<email>.txt`` record, or ""."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    parts = raw.split("----")
    if len(parts) < 3:
        return ""
    sso = "----".join(parts[2:]).lstrip("-").strip()
    return sso if sso.startswith("eyJ") else ""


def _index(accounts_dir: Path) -> dict[str, Path]:
    try:
        names = os.listdir(accounts_dir)
    except OSError as exc:
        print(f"[!] skip {accounts_dir}: {exc}", file=sys.stderr)
        return {}
    return {
        name.lower(): accounts_dir / name
        for name in names
        if name.lower().endswith(".txt") and "@" in name
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts-dir", required=True, type=Path)
    parser.add_argument("--accounts-json", required=True, type=Path,
                        help="grok2api admin account list (JSON)")
    parser.add_argument("--also", action="append", default=[], type=Path,
                        help="extra accounts directories to search (repeatable)")
    parser.add_argument("--out", required=True, type=Path,
                        help="directory to write sso-manifest.json into")
    args = parser.parse_args(argv)

    wanted = _emails_from_admin(args.accounts_json)
    if not wanted:
        print("no reauthRequired accounts in the admin account list", file=sys.stderr)
        return 1

    indexes = [_index(d) for d in [args.accounts_dir, *args.also]]
    manifest: list[dict[str, str]] = []
    missing: list[str] = []
    for row in wanted:
        key = row["email"].lower() + ".txt"
        sso = ""
        for index in indexes:
            path = index.get(key)
            if path is None:
                continue
            sso = _parse_account_file(path)
            if sso:
                break
        if sso:
            manifest.append({"id": row["id"], "email": row["email"], "sso": sso})
        else:
            missing.append(row["email"])

    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / "sso-manifest.json"
    output.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    output.chmod(0o600)

    print(json.dumps({
        "reauthRequired": len(wanted),
        "with_sso": len(manifest),
        "missing": len(missing),
        "missing_sample": missing[:10],
        "manifest": str(output),
    }, ensure_ascii=False))
    return 0 if manifest else 1


if __name__ == "__main__":
    raise SystemExit(main())
