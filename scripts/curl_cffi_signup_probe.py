#!/usr/bin/env python3
"""Open sign-up with curl_cffi and print status only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFFI = ROOT / "cffi_har_signup"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CFFI))

from cffi_client import probe_live_signup


def main() -> int:
    proxy = ""
    config = ROOT / "config.json"
    if config.is_file():
        data = json.loads(config.read_text(encoding="utf-8"))
        proxy = str(data.get("proxy") or "").strip()
    report = probe_live_signup(proxy)
    print("impersonate", report.get("impersonate"))
    print("status", report.get("status"))
    print("html_len", report.get("html_len"))
    print("castle_pk", report.get("castle_pk"))
    print("castle_script", report.get("castle_script"))
    print("cookies", ",".join(report.get("cookie_names") or []))
    print("send_status", report.get("send_status"))
    print("send_code", report.get("send_code"))
    return 0 if report.get("status") else 1


if __name__ == "__main__":
    raise SystemExit(main())
