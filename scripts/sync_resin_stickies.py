#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resin_sticky import sync_resin_stickies


def main() -> int:
    parser = argparse.ArgumentParser(description="Expand Resin into 1 sticky per node")
    parser.add_argument("--target", type=int, default=None)
    parser.add_argument("--no-warm", action="store_true")
    args = parser.parse_args()
    result = sync_resin_stickies(target=args.target, warm=not args.no_warm)
    safe = {k: result.get(k) for k in (
        "ok", "platform", "routable_node_count", "target", "mapped",
        "existing", "warmed", "probed", "collisions", "errors", "unique_nodes",
    )}
    summary = result.get("summary") or {}
    safe["pool_total"] = summary.get("total")
    safe["pool_usable"] = summary.get("usable")
    print(json.dumps(safe, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
