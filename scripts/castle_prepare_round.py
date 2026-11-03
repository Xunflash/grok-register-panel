#!/usr/bin/env python3
"""Open Chromium once and store signup computed-style names for this round."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nobrowser_protocol.castle_env import DEFAULT_STYLE_FILE, prepare_castle_round  # noqa: E402


def main() -> None:
    config_path = Path(os.environ.get("CASTLE_CONFIG") or (ROOT / "config.json"))
    proxy = ""
    if config_path.is_file():
        proxy = str(json.loads(config_path.read_text(encoding="utf-8")).get("proxy") or "").strip()
    dest = Path(os.environ.get("CASTLE_STYLE_FILE") or DEFAULT_STYLE_FILE)
    count = prepare_castle_round(proxy, dest)
    print("style_names", count, "file", dest.name)


if __name__ == "__main__":
    main()
