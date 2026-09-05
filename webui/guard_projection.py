"""Authenticated, bounded adapter; Guard remains owner of quarantine state."""
import json
import os
import urllib.parse
import urllib.request


def quarantines(config, page=1):
    page = int(page)
    if page < 1:
        raise ValueError("invalid pagination")
    url = str(config.get("guard_url") or "").rstrip("/")
    token = str(os.environ.get("GUARD_CONTROL_TOKEN") or config.get("guard_token") or "")
    if not token or urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("guard access is not configured")
    request = urllib.request.Request(url + f"/api/quarantines?page={page}&pageSize=25", headers={"X-Guard-Control-Token": token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = response.read(100001)
    if len(data) > 100000:
        raise ValueError("guard response exceeds limit")
    payload = json.loads(data)
    items = [{"account_id": str(item.get("account_id") or "")[:80],
              "state": str(item.get("state") or "")[:32],
              "next_probe_at": item.get("next_probe_at") if isinstance(item.get("next_probe_at"), (float, int)) else None}
             for item in payload.get("items", [])[:25]]
    return {"items": items, "page": page, "page_size": 25, "total": int(payload.get("total") or 0)}
