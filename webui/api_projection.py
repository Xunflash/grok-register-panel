"""Bounded public projections with explicit field ownership."""
import hashlib
import json
import math
import re

from webui.security_utils import redact_log_line


SCALARS = {"base_cpa", "base_cpa_stale", "cpa", "cpa_delta", "target", "done_attempts", "progress_pct", "success_rate", "rate_per_min", "eta", "workers", "ok", "fail", "domain", "skip", "bot0", "bot1", "bfs"}
CONTROL = {"workers", "batch_count", "add_count", "risk_pause", "mode", "turnstile_provider", "registration_scheme", "stop_min_accounts", "stop_min_success_rate", "debug_mode"}
CONTROL_ENUMS = {"mode": {"orch", "batch"}, "turnstile_provider": {"capsolver", "yescaptcha", "local"}, "registration_scheme": {"browser", "protocol", "protocol_patchright"}}
TRAFFIC = {"running", "bytes_up", "bytes_down", "bytes_total", "connections", "active_connections", "metered_proxies", "unmetered_proxies", "target", "workers", "successful_accounts", "batch_count", "completed_batch_count", "includes_current", "total_bytes", "bytes_per_batch", "bytes_per_success"}


def numeric_fields(value, keys):
    return {key: safe_scalar(item) for key, item in value.items() if key in keys}


def safe_scalar(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value if value is None or isinstance(value, (bool, int)) else None


def summary(value):
    result = {key: safe_scalar(value[key]) for key in SCALARS if key in value}
    result["process"] = numeric_fields(value.get("process", {}), {"running", "pid", "batch_pid", "orch_pid", "orch_running", "batch_running"})
    result["control"] = {
        key: item
        for key, item in value.get("control", {}).items()
        if key in CONTROL
        and (
            isinstance(item, (bool, int))
            or (key == "stop_min_success_rate" and isinstance(item, float))
        )
    }
    for key, choices in CONTROL_ENUMS.items():
        if value.get("control", {}).get(key) in choices:
            result["control"][key] = value["control"][key]
    for key in ("traffic", "traffic_summary"):
        result[key] = numeric_fields(value.get(key, {}), TRAFFIC)
    result["traffic"]["batch_id"] = bool(value.get("traffic", {}).get("batch_id"))
    result["blacklist"] = numeric_fields(value.get("blacklist", {}), {"count"})
    result["blacklist_update"] = numeric_fields(value.get("blacklist_update", {}), {"error_count"})
    result["rates"] = {key: numeric_fields(bucket, {"total", "ok", "fail", "risk", "success_rate"})
                       for key, bucket in value.get("rates", {}).items() if key in {"1h", "3h", "12h", "all"} and isinstance(bucket, dict)}
    for key in ("worker_ok", "worker_fail"):
        result[key] = {name: safe_scalar(count) for name, count in list(value.get(key, {}).items())[:32] if re.fullmatch(r"W\d{1,3}|\?", name)}
    result["ended"] = numeric_fields(value.get("ended") or {}, {"success", "fail"}) or None
    eta = str(value.get("eta") or "")
    result["eta"] = eta if re.fullmatch(r"\d{1,6}(\.\d)?[mh]", eta) else None
    result["log_size"] = safe_scalar(value.get("log_size"))
    result["log_name"] = "批次日志"
    result["tail"] = [
        redact_log_line(str(line))[:1200]
        for line in list(value.get("tail") or [])[-40:]
        if str(line).strip()
    ]
    result["caught_up"] = bool(value.get("caught_up", True))
    result["schema_version"] = 2
    result["state_version"] = etag(result).strip('"')
    return result


def etag(value):
    content = {key: item for key, item in value.items() if key not in {"ts", "ts_human", "sampled_at"}} if isinstance(value, dict) else value
    return '"' + hashlib.sha256(json.dumps(content, sort_keys=True, allow_nan=False).encode()).hexdigest() + '"'


class PageConflict(ValueError):
    pass


def paginate(value, page=1, page_size=50, generation=None):
    page = int(page)
    page_size = int(page_size)
    if page < 1 or page_size < 1:
        raise ValueError("invalid pagination")
    page_size = min(50, page_size)
    items = value.get("items", [])
    current = etag(items).strip('"')
    if generation and generation != current:
        raise PageConflict("snapshot changed; restart pagination")
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": len(items), "page": page, "page_size": page_size, "generation": current}
