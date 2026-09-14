#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Change Outlook passwords and/or mint new Graph refresh tokens into panel inventory."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from email_providers.outlook_renew import CLIENT_ID, renew_inventory


def _default_inventory() -> str:
    config = ROOT / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8") or "{}")
        path = str((data or {}).get("outlook_rt_inventory") or "").strip()
        if path:
            return path
    except Exception:
        pass
    return str(ROOT / "2000outlook_b3.txt")


def _default_proxy() -> str:
    # Resin is for x.ai signup; Microsoft login/authorize often 502s there.
    # Empty means requests trust_env / claw TUN.
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Renew Outlook inventory passwords and Graph RTs")
    parser.add_argument("--inventory", default=_default_inventory())
    parser.add_argument(
        "--mode",
        choices=("rt-only", "change-password"),
        default="change-password",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--only-dead", action="store_true")
    parser.add_argument("--include-alive", action="store_true")
    parser.add_argument("--unused-only", action="store_true", default=True)
    parser.add_argument("--include-used", action="store_true")
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--proxy", default=_default_proxy())
    args = parser.parse_args(argv)

    only_dead = bool(args.only_dead) and not args.include_alive
    unused_only = bool(args.unused_only) and not args.include_used
    print(
        f"inventory={args.inventory} mode={args.mode} limit={args.limit} "
        f"only_dead={only_dead} unused_only={unused_only} client_id={CLIENT_ID}",
        flush=True,
    )
    stats = renew_inventory(
        args.inventory,
        mode=args.mode,
        only_dead=only_dead,
        unused_only=unused_only,
        limit=args.limit,
        emails=args.email,
        proxy=args.proxy,
        log=lambda msg: print(msg, flush=True),
    )
    print(
        f"done ok={stats['ok']} fail={stats['fail']} skip={stats['skip']} wrote={stats['wrote']}",
        flush=True,
    )
    return 0 if stats["fail"] == 0 and stats["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
