#!/usr/bin/env python3
"""Single-browser Turnstile debug run. Does not rewrite config.json."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

os.environ["TURNSTILE_PROVIDER"] = "yescaptcha"
os.environ["GROK_CF_DEBUG"] = "1"
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("XDG_CACHE_HOME", "/data/claw-relocated/camoufox-cache")

cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
key = str(cfg.get("yescaptcha_key") or "").strip()
if key:
    os.environ["YESCAPTCHA_KEY"] = key
api = str(cfg.get("yescaptcha_api") or "").strip()
if api:
    os.environ["YESCAPTCHA_API"] = api

for proxy_key in (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
):
    os.environ.pop(proxy_key, None)

from grok_register_ttk import _wire_runtime_modules, load_config, run_registration_cli
import grok_register_ttk as app

load_config()
app.config["register_workers"] = 1
app.config["account_interval"] = "0"
_wire_runtime_modules()
print(
    f"[debug-once] DISPLAY={os.environ.get('DISPLAY')!r} "
    f"provider={os.environ.get('TURNSTILE_PROVIDER')} "
    f"cf_debug=1 workers=1",
    flush=True,
)
run_registration_cli(1)
print("[debug-once] finished", flush=True)
