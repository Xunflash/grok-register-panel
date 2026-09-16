#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live no-Camoufox protocol signup: curl_cffi or Patchright Chromium."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nobrowser_protocol.cffi_engine import run_cffi_one
from nobrowser_protocol.patchright_engine import run_patchright_one
from signup_protocol import load_har


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_panel_config() -> dict:
    path = ROOT / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def default_har() -> Path:
    env = os.environ.get("CFFI_HAR") or os.environ.get("GROK_PROTOCOL_HAR") or ""
    if env and Path(env).is_file():
        return Path(env)
    for candidate in (
        ROOT / "tests" / "fixtures" / "signup_har_excerpt.json",
        ROOT / "cffi_har_signup" / "tests" / "fixtures" / "signup_har_excerpt.json",
        ROOT / "log" / "signup-protocol.har",
    ):
        if candidate.is_file():
            return candidate
    return ROOT / "tests" / "fixtures" / "signup_har_excerpt.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("cffi", "patchright"), default="patchright")
    parser.add_argument("--har", default=str(default_har()))
    parser.add_argument("--outlook", default="")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--guard-url", default="")
    parser.add_argument("--guard-token", default="")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args(argv)

    cfg = load_panel_config()
    har_path = Path(args.har)
    har = load_har(har_path)
    outlook = Path(
        args.outlook
        or cfg.get("outlook_rt_inventory")
        or (ROOT / "2000outlook_b3.txt")
    )
    sys.path.insert(0, str(ROOT / "cffi_har_signup"))
    from outlook_min import load_inventory

    accounts = load_inventory(outlook)
    if not accounts:
        raise SystemExit(f"no outlook accounts in {outlook}")
    proxy = str(args.proxy or cfg.get("proxy") or "").strip()
    guard_url = str(
        args.guard_url
        or os.environ.get("GUARD_URL")
        or cfg.get("guard_url")
        or cfg.get("risk_guard_url")
        or "http://127.0.0.1:8788"
    )
    guard_token = str(
        args.guard_token
        or os.environ.get("GUARD_TOKEN")
        or os.environ.get("GUARD_CONTROL_TOKEN")
        or cfg.get("guard_token")
        or ""
    )
    grok2api = {
        "url": str(cfg.get("grok2api_remote_url") or ""),
        "username": str(cfg.get("grok2api_remote_username") or ""),
        "password": str(cfg.get("grok2api_remote_password") or ""),
    }
    cap_key = str(cfg.get("capsolver_key") or os.environ.get("CAPSOLVER_KEY") or "")
    cap_api = str(cfg.get("capsolver_api") or "https://api.capsolver.com")
    runner = run_cffi_one if args.engine == "cffi" else run_patchright_one
    log(f"engine={args.engine} har={har_path} outlook={outlook} n={min(args.count, len(accounts))}")
    last = {}
    for account in accounts[: max(1, args.count)]:
        log(f"try email={account['email']}")
        try:
            last = runner(
                har=har,
                proxy=proxy,
                account=account,
                capsolver_key=cap_key,
                capsolver_api=cap_api,
                guard_url=guard_url,
                guard_token=guard_token,
                grok2api=grok2api,
                log=log,
            )
        except Exception as exc:
            log(f"fail {exc}")
            last = {"email": account["email"], "error": str(exc)[:400], "guard_payload": {}}
            continue
        classification = str((last.get("guard_payload") or {}).get("classification") or "")
        log(f"result classification={classification!r} guard={last.get('guard')}")
        if classification == "normal":
            print(json.dumps(last, ensure_ascii=False, default=str)[:2000])
            return 0
    print(json.dumps(last, ensure_ascii=False, default=str)[:2000])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
