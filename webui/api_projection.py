"""Bounded public projections with explicit field ownership."""
import hashlib
import json
import math


SCALARS = {"base_cpa", "base_cpa_stale", "cpa", "cpa_delta", "target", "done_attempts", "progress_pct", "success_rate", "rate_per_min", "eta", "workers", "ok", "fail", "domain", "skip", "bot0", "bot1", "bfs"}
CONTROL = {"workers", "batch_count", "add_count", "risk_pause", "mode", "turnstile_provider", "registration_scheme"}


def safe_scalar(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value if value is None or isinstance(value, (bool, int)) else None


def summary(value):
    result = {key: safe_scalar(value[key]) for key in SCALARS if key in value}
    result["process"] = {key: safe_scalar(item) for key, item in value.get("process", {}).items() if key in {"running", "pid", "batch_pid"}}
    result["control"] = {key: item for key, item in value.get("control", {}).items() if key in CONTROL and isinstance(item, (bool, int))}
    result["schema_version"] = 2
    result["state_version"] = etag(result).strip('"')
    return result


def etag(value):
    content = {key: item for key, item in value.items() if key not in {"ts", "ts_human", "sampled_at"}}
    return '"' + hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest() + '"'


def paginate(value, page=1, page_size=50):
    page = int(page)
    page_size = int(page_size)
    if page < 1 or page_size < 1:
        raise ValueError("invalid pagination")
    page_size = min(50, page_size)
    items = value.get("items", [])
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": len(items), "page": page, "page_size": page_size}
