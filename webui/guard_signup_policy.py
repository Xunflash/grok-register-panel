"""Narrow authenticated adapter for Guard-owned signup policy."""
import json
import os
import urllib.parse
import urllib.request


def signup_policy(config, enabled=None):
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    url = str(config.get("guard_url") or "").rstrip("/")
    token = str(os.environ.get("GUARD_CONTROL_TOKEN") or config.get("guard_token") or "")
    if not token or urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise RuntimeError("guard access is not configured")
    body = None if enabled is None else json.dumps({"policy": {"signup_short_window_risk": enabled}}).encode()
    request = urllib.request.Request(
        url + "/api/config", data=body, method="GET" if body is None else "PUT",
        headers={"X-Guard-Control-Token": token, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        data = response.read(100001)
    if len(data) > 100000:
        raise RuntimeError("guard response exceeds limit")
    policy = json.loads(data).get("policy", {})
    value = policy.get("signup_short_window_risk")
    if type(value) is not bool or (enabled is not None and value != enabled):
        raise RuntimeError("guard signup policy unavailable")
    return {"enabled": value, "min_generation_ms": policy.get("min_hard_generation_ms"), "max_attempts": 3}
