#!/usr/bin/env python3
"""Capture one live accounts.x.ai signup as HAR, then dump the three JSON auth calls."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

HAR_PATH = Path(os.environ.get("GROK_RECORD_HAR") or str(ROOT / "log" / "signup-protocol.har"))
DUMP_PATH = Path(os.environ.get("GROK_HAR_DUMP") or str(ROOT / "log" / "signup-protocol-dump.json"))

os.environ["TURNSTILE_PROVIDER"] = "yescaptcha"
os.environ["GROK_CF_DEBUG"] = "1"
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("XDG_CACHE_HOME", "/data/claw-relocated/camoufox-cache")
os.environ["GROK_RECORD_HAR"] = str(HAR_PATH)

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

HAR_PATH.parent.mkdir(parents=True, exist_ok=True)

from grok_register_ttk import _wire_runtime_modules, load_config, run_registration_cli
import grok_register_ttk as app
import register_flow as rf
from signup_protocol import (
    AUTH_CREATE_ACCOUNT,
    AUTH_SEND_CODE,
    AUTH_VERIFY_EMAIL,
    build_create_account,
    extract_signup_calls,
)
from turnstile_solver import get_live_token

load_config()
app.config["register_workers"] = 1
app.config["account_interval"] = "0"
_wire_runtime_modules()

_capture_state = {"email": "", "code": ""}
_orig_fill_email = rf.fill_email_and_submit
_orig_fill_code = rf.fill_code_and_submit
_orig_fill_profile = rf.fill_profile_and_submit

_FETCH_CREATE_JS = """
const path = arguments[0];
const body = arguments[1];
return fetch(path, {
  method: 'POST',
  credentials: 'include',
  headers: { 'content-type': 'application/json', 'accept': 'application/json' },
  body: body
}).then((res) => res.text().then((text) => ({
  status: res.status,
  text: String(text || '').slice(0, 800)
})));
"""


def _fill_email_and_submit_capture(*args, **kwargs):
    email, token = _orig_fill_email(*args, **kwargs)
    _capture_state["email"] = str(email or "")
    return email, token


def _fill_code_and_submit_capture(*args, **kwargs):
    code = _orig_fill_code(*args, **kwargs)
    _capture_state["code"] = str(code or "")
    return code


def _fill_profile_and_submit_capture(*args, **kwargs):
    try:
        return _orig_fill_profile(*args, **kwargs)
    except Exception as profile_exc:
        email = _capture_state.get("email") or ""
        code = _capture_state.get("code") or ""
        token = ""
        try:
            token = get_live_token() or rf._stashed_cf_token()
        except Exception:
            token = ""
        log_callback = kwargs.get("log_callback")
        if not (email and code and token and len(token) >= 80):
            raise
        given_name, family_name, password = rf.build_profile()
        built = build_create_account(
            email=email,
            password=password,
            given_name=given_name,
            family_name=family_name,
            email_validation_code=code,
            turnstile_token=token,
        )
        if log_callback:
            log_callback(
                f"[har-capture] profile widget blocked ({profile_exc}); "
                f"protocol fetch create-account keys={sorted(built['json'])}"
            )
        result = rf.page.run_js(_FETCH_CREATE_JS, built["path"], json.dumps(built["json"]))
        if log_callback:
            log_callback(f"[har-capture] create-account fetch result={result!r}"[:400])
        status = 0
        if isinstance(result, dict):
            try:
                status = int(result.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
        if status >= 200 and status < 300:
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }
        raise


rf.fill_email_and_submit = _fill_email_and_submit_capture
rf.fill_code_and_submit = _fill_code_and_submit_capture
rf.fill_profile_and_submit = _fill_profile_and_submit_capture
app.fill_email_and_submit = _fill_email_and_submit_capture
app.fill_code_and_submit = _fill_code_and_submit_capture
app.fill_profile_and_submit = _fill_profile_and_submit_capture
print(
    f"[har-capture] DISPLAY={os.environ.get('DISPLAY')!r} har={HAR_PATH}",
    flush=True,
)
try:
    run_registration_cli(1)
finally:
    print("[har-capture] registration returned; dumping HAR", flush=True)

if not HAR_PATH.is_file():
    print(f"[har-capture] MISSING HAR {HAR_PATH}", flush=True)
    sys.exit(2)

calls = extract_signup_calls(HAR_PATH)
dump = {
    "har_path": str(HAR_PATH),
    "har_bytes": HAR_PATH.stat().st_size,
    "paths": sorted(calls.keys()),
}
for path, call in calls.items():
    body = dict(call.get("json") or {})
    if "password" in body:
        body["password"] = "<redacted>"
    if "turnstileToken" in body:
        token = str(body["turnstileToken"])
        body["turnstileToken"] = token[:16] + f"…(len={len(token)})"
    if "castleRequestToken" in body:
        token = str(body["castleRequestToken"])
        body["castleRequestToken"] = token[:24] + f"…(len={len(token)})"
    cookies = {k: ("<set>" if v else "") for k, v in (call.get("cookies") or {}).items()}
    dump[path] = {
        "method": call.get("method"),
        "url": call.get("url"),
        "status": call.get("status"),
        "json_keys": sorted((call.get("json") or {}).keys()),
        "json": body,
        "cookie_names": sorted(cookies),
        "header_names": sorted((call.get("headers") or {}).keys()),
        "user_agent": (call.get("headers") or {}).get("User-Agent")
        or (call.get("headers") or {}).get("user-agent"),
    }
DUMP_PATH.write_text(json.dumps(dump, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: dump.get(k) for k in ("har_bytes", "paths")}, ensure_ascii=False), flush=True)
missing = [p for p in (AUTH_SEND_CODE, AUTH_VERIFY_EMAIL, AUTH_CREATE_ACCOUNT) if p not in calls]
if missing:
    print(f"[har-capture] missing paths: {missing}", flush=True)
    sys.exit(3)
print("[har-capture] ok three signup JSON calls captured", flush=True)
