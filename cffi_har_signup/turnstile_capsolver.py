# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
import urllib.request

DEFAULT_API = "https://api.capsolver.com"
DEFAULT_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
DEFAULT_PAGE = "https://accounts.x.ai/sign-up?redirect=grok-com"


def _post(api: str, path: str, payload: dict, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(
        api.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"capsolver non-object: {data!r}")
    return data


def solve_turnstile(
    client_key: str,
    *,
    website_url: str = DEFAULT_PAGE,
    website_key: str = DEFAULT_SITEKEY,
    api: str = DEFAULT_API,
    poll_max: int = 40,
) -> str:
    key = str(client_key or "").strip()
    if not key:
        raise RuntimeError("CAPSOLVER_KEY empty")
    created = _post(
        api,
        "/createTask",
        {
            "clientKey": key,
            "task": {
                "type": "AntiTurnstileTaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": website_key,
            },
        },
    )
    if created.get("errorId") not in (0, None):
        raise RuntimeError(created.get("errorDescription") or created)
    task_id = created.get("taskId") or created.get("task_id")
    if not task_id:
        raise RuntimeError(f"capsolver no taskId: {created}")
    last = None
    for _ in range(poll_max):
        time.sleep(1.0)
        last = _post(
            api,
            "/getTaskResult",
            {"clientKey": key, "taskId": task_id},
        )
        if last.get("errorId") not in (0, None):
            raise RuntimeError(last.get("errorDescription") or last)
        if last.get("status") == "ready":
            token = str((last.get("solution") or {}).get("token") or "").strip()
            if not token:
                raise RuntimeError("capsolver empty token")
            return token
        if last.get("status") in {"failed", "error"}:
            raise RuntimeError(f"capsolver failed: {last}")
    raise RuntimeError(f"capsolver timeout: {last}")
