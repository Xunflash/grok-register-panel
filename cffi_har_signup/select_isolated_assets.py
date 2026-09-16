#!/usr/bin/env python3
"""Pick unused same-/24 resin stickies and disjoint Outlook lines. Read-only on panel."""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urlencode

PANEL = Path("/data/claw-relocated/grok-register-panel")
RESIN = "http://127.0.0.1:2260"
XFPACK_ID = "ffd2e677-84d6-4577-9966-97fa9055cc4a"


def env_file(path: Path) -> dict[str, str]:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.strip().startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def resin_token() -> str:
    return os.environ.get("RESIN_ADMIN_TOKEN") or env_file(Path("/home/claw/resin/.env")).get(
        "RESIN_ADMIN_TOKEN", ""
    )


def get(path: str, token: str, query: dict | None = None):
    url = RESIN + path
    if query:
        url += "?" + urlencode(query)
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def prefix24(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) if len(parts) == 4 else ""


def _emails_from_file(path: Path) -> set[str]:
    found: set[str] = set()
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        email = line.strip().split("----")[0].strip().lower()
        if "@" in email:
            found.add(email)
    return found


def panel_blocked_emails() -> set[str]:
    """Emails already in the live panel inventory/used/claims — do not reuse."""
    found: set[str] = set()
    found |= _emails_from_file(PANEL / "2000outlook_b3.txt")
    for path in PANEL.glob("*.used"):
        found |= _emails_from_file(path)
    for d in PANEL.glob("*.claims"):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            name = p.name.lower()
            if "@" in name:
                found.add(name.split("----")[0])
    return found


def pick_outlook(n: int = 12) -> list[str]:
    blocked = panel_blocked_emails()
    picked = []
    for src in (
        PANEL / "outlook邮箱.txt",
        PANEL / "outlook邮箱1.txt",
        PANEL / "outlook邮箱-重试.txt",
    ):
        if not src.is_file():
            continue
        for line in src.read_text(encoding="utf-8", errors="replace").splitlines():
            email = line.strip().split("----")[0].strip().lower()
            if email in blocked or "@" not in email:
                continue
            picked.append(line.strip())
            blocked.add(email)
            if len(picked) >= n:
                return picked
    return picked


def pick_proxies(token: str, want: int = 6) -> tuple[str, list[dict]]:
    panel_pool = json.loads((PANEL / "log" / "proxy_pool.json").read_text(encoding="utf-8"))
    panel_ips = {
        str(it.get("exit_ip") or "").strip()
        for it in (panel_pool.get("items") or [])
        if it.get("exit_ip")
    }
    panel_enabled = {
        str(it.get("exit_ip") or "").strip()
        for it in (panel_pool.get("items") or [])
        if it.get("enabled") and it.get("exit_ip")
    }
    unused_by_p24: dict[str, list[str]] = defaultdict(list)
    offset = 0
    while offset < 2400:
        data = get(
            "/api/v1/nodes",
            token,
            {"subscription_id": XFPACK_ID, "limit": 200, "offset": offset},
        )
        items = data.get("items") or []
        for it in items:
            if not it.get("enabled") and it.get("has_outbound") is False:
                continue
            ip = str(it.get("egress_ip") or "").strip()
            if not ip or ip in panel_ips or ip in panel_enabled:
                continue
            p24 = prefix24(ip)
            if p24:
                unused_by_p24[p24].append(ip)
        if len(items) < 200:
            break
        offset += 200
    ranked = sorted(unused_by_p24.items(), key=lambda kv: -len(set(kv[1])))
    print("unused /24 catalog", [(k, len(set(v))) for k, v in ranked[:8]])

    cfg = json.loads((PANEL / "config.json").read_text(encoding="utf-8"))
    mixed = str(cfg.get("proxy") or "http://Default:x@localhost:2260")
    from urllib.parse import urlsplit

    parsed = urlsplit(mixed)
    password = parsed.password or ""
    hostinfo = parsed.hostname or "127.0.0.1"
    port = parsed.port or 2260
    scheme = parsed.scheme or "http"

    raw_probes = []
    for _ in range(max(want * 10, 40)):
        sticky = "cffiiso-" + secrets.token_hex(6)
        user = quote("Default." + sticky, safe="-._~")
        url = f"{scheme}://{user}:{password}@{hostinfo}:{port}"
        ip = probe_ip(url)
        print("probe", sticky, ip)
        if not ip or ip in panel_ips or ip in panel_enabled:
            time.sleep(0.15)
            continue
        p24 = prefix24(ip)
        if not p24:
            continue
        if any(p["exit_ip"] == ip for p in raw_probes):
            continue
        raw_probes.append({"url": url, "exit_ip": ip, "sticky": sticky, "prefix24": p24})
        time.sleep(0.15)

    by_p24: dict[str, list[dict]] = defaultdict(list)
    for row in raw_probes:
        by_p24[row["prefix24"]].append(row)
    if not by_p24:
        raise SystemExit("no unused probed stickies")
    chosen_p24, probes = max(by_p24.items(), key=lambda kv: len(kv[1]))
    probes = probes[:want]
    print("chosen probed /24", chosen_p24, "n", len(probes))
    return chosen_p24, probes


def probe_ip(proxy_url: str) -> str:
    try:
        from curl_cffi import requests as cf

        s = cf.Session(impersonate="firefox135")
        s.proxies = {"http": proxy_url, "https": proxy_url}
        r = s.get("https://api.ipify.org", timeout=20)
        return str(r.text or "").strip()
    except Exception as exc:
        print("probe fail", str(exc)[:120])
        return ""


def main() -> None:
    token = resin_token()
    p24, proxies = pick_proxies(token, want=6)
    outlook = pick_outlook(12)
    panel_cfg = json.loads((PANEL / "config.json").read_text(encoding="utf-8"))
    out = {
        "prefix24": p24,
        "proxies": [
            {"exit_ip": p["exit_ip"], "sticky": p["sticky"], "url": p["url"], "prefix24": p["prefix24"]}
            for p in proxies
        ],
        "outlook_emails": [line.split("----", 1)[0] for line in outlook],
        "outlook_lines": outlook,
        "capsolver": {
            "api": panel_cfg.get("capsolver_api") or "https://api.capsolver.com",
            "key": panel_cfg.get("capsolver_key") or "",
        },
        "guard": {
            "url": panel_cfg.get("guard_url") or "http://192.168.0.5:8788",
            "token": panel_cfg.get("guard_token") or "",
        },
        "panel_enabled_overlap": False,
    }
    print(json.dumps({k: out[k] for k in ("prefix24", "outlook_emails") } | {"n_proxies": len(proxies)}, ensure_ascii=False))
    Path("/tmp/cffi-isolated-full.json").write_text(json.dumps(out), encoding="utf-8")


if __name__ == "__main__":
    main()
