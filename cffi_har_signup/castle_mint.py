# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MINT_JS = ROOT / "castle_jsdom" / "mint.mjs"
DEFAULT_PK = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0"
)


def mint_castle_token(pk: str = "", user_agent: str = "", timeout: float = 20.0) -> str:
    key = (pk or os.environ.get("CASTLE_PK") or DEFAULT_PK).strip()
    ua = (user_agent or DEFAULT_UA).strip()
    if not MINT_JS.is_file():
        raise RuntimeError(f"castle mint script missing: {MINT_JS}")
    proc = subprocess.run(
        ["node", str(MINT_JS), key, ua],
        cwd=str(MINT_JS.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        err = (proc.stderr or proc.stdout or "mint failed").strip()[:300]
        raise RuntimeError(f"castle jsdom mint failed: {err}")
    return token
