"""Expand a Resin mixed port into one sticky identity per assigned node.

Resin has no API to pin a sticky to a chosen node. A 1:1 mapping is created by
sending traffic through a unique sticky username, reading the lease, and
dropping collisions so each kept identity owns exactly one node_hash.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from secure_files import atomic_write_json
from webui.security_utils import redact_log_line

ROOT = Path(__file__).resolve().parent
MAP_PATH = Path(os.environ.get("RESIN_STICKY_MAP_FILE", str(ROOT / "log" / "resin_sticky_map.json")))
CONFIG_PATH = Path(os.environ.get("EMAIL_PROVIDER_CONFIG_FILE", str(ROOT / "config.json")))

DEFAULT_TARGET = 128
DEFAULT_WARM_WORKERS = 32
MAX_WARM_WORKERS = 64
PROBE_TIMEOUT = 8.0

_SYNC_LOCK = threading.RLock()
_SYNC_JOB: dict[str, Any] = {
    "running": False,
    "job_id": None,
    "phase": "",
    "target": 0,
    "workers": 0,
    "mapped": 0,
    "probed": 0,
    "collisions": 0,
    "errors": 0,
    "started_at": None,
    "finished_at": None,
    "error": "",
}


class ResinStickyError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
    except OSError as exc:
        raise ResinStickyError(f"cannot read config.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise ResinStickyError("config.json must be an object")
    return raw


def _admin_token(cfg: dict) -> str:
    return str(cfg.get("resin_admin_token") or os.environ.get("RESIN_ADMIN_TOKEN") or "").strip()


def _admin_base(cfg: dict) -> str:
    explicit = str(cfg.get("resin_admin_url") or os.environ.get("RESIN_ADMIN_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    proxy = str(cfg.get("proxy") or "").strip()
    if not proxy:
        return "http://127.0.0.1:2260"
    parsed = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    host = parsed.hostname or "127.0.0.1"
    if host in {"host.docker.internal", "172.17.0.1"}:
        host = "127.0.0.1"
    port = parsed.port or 2260
    return f"http://{host}:{port}"


def _platform_name(cfg: dict) -> str:
    return str(cfg.get("resin_platform") or "Default").strip() or "Default"


def _target_count(cfg: dict, override: object = None) -> int:
    if override not in (None, ""):
        try:
            value = int(override)
        except (TypeError, ValueError) as exc:
            raise ResinStickyError("resin_sticky_target must be an integer") from exc
    else:
        try:
            value = int(cfg.get("resin_sticky_target") or DEFAULT_TARGET)
        except (TypeError, ValueError):
            value = DEFAULT_TARGET
    return max(1, value)


def with_resin_sticky_account(proxy_url: str, account: str, platform: str = "") -> str:
    value = str(proxy_url or "").strip()
    sticky_account = str(account or "").strip()
    if not value or not sticky_account:
        return value
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    if not parsed.hostname:
        return value
    if "@" in parsed.netloc:
        raw_userinfo, hostinfo = parsed.netloc.rsplit("@", 1)
        raw_username, separator, raw_password = raw_userinfo.partition(":")
    else:
        raw_username, separator, raw_password = "", "", ""
        hostinfo = parsed.netloc
    existing_platform = unquote(raw_username).split(".", 1)[0].strip()
    selected_platform = str(platform or "").strip() or existing_platform or "Default"
    identity = ".".join(
        (
            quote(selected_platform, safe="-._~"),
            quote(sticky_account, safe="-._~"),
        )
    )
    password = f":{raw_password}" if separator else ""
    resolved = urlunsplit(
        (
            parsed.scheme or "http",
            f"{identity}{password}@{hostinfo}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )
    return resolved


def with_resin_platform(proxy_url: str, platform: str) -> str:
    """Swap the Resin mixed-port platform in the username; keep sticky suffix."""
    value = str(proxy_url or "").strip()
    selected = str(platform or "").strip()
    if not value or not selected:
        return value
    parsed = urlsplit(value if "://" in value else f"http://{value}")
    if not parsed.hostname or "@" not in parsed.netloc:
        return value
    raw_userinfo, hostinfo = parsed.netloc.rsplit("@", 1)
    raw_username, separator, raw_password = raw_userinfo.partition(":")
    user = unquote(raw_username)
    if not user:
        new_user = selected
    elif "." in user:
        _, sticky = user.split(".", 1)
        new_user = f"{selected}.{sticky}" if sticky else selected
    else:
        new_user = selected
    identity = quote(new_user, safe="-._~")
    password = f":{raw_password}" if separator else ""
    return urlunsplit(
        (
            parsed.scheme or "http",
            f"{identity}{password}@{hostinfo}",
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _request(base: str, token: str, method: str, path: str, query: dict | None = None, timeout: float = 10.0) -> Any:
    import urllib.error
    import urllib.parse
    import urllib.request

    url = f"{base.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, '')})}"
    req = urllib.request.Request(url, method=method.upper())
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")[:240]
        raise ResinStickyError(f"{method} {path} HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ResinStickyError(f"{method} {path} failed: {exc}") from exc


def _resolve_platform(base: str, token: str, name: str) -> dict:
    payload = _request(base, token, "GET", "/api/v1/platforms") or {}
    items = list(payload.get("items") or [])
    wanted = str(name or "Default").strip()
    for item in items:
        if str(item.get("id") or "") == wanted or str(item.get("name") or "").strip() == wanted:
            return item
    lowered = wanted.lower()
    for item in items:
        if str(item.get("name") or "").strip().lower() == lowered:
            return item
    raise ResinStickyError(f"Resin platform not found: {wanted}")


def _list_leases(base: str, token: str, platform_id: str) -> list[dict]:
    page_size = 500
    offset = 0
    items = []
    while True:
        payload = _request(
            base,
            token,
            "GET",
            f"/api/v1/platforms/{platform_id}/leases",
            query={"limit": page_size, "offset": offset},
        ) or {}
        page = list(payload.get("items") or [])
        for item in page:
            account = str(item.get("account") or "").strip()
            node_hash = str(item.get("node_hash") or "").strip()
            if account and node_hash:
                items.append(item)
        if len(page) < page_size:
            break
        total = payload.get("total")
        if total is not None:
            try:
                if offset + page_size >= int(total):
                    break
            except (TypeError, ValueError):
                pass
        offset += page_size
    return items


def _warm_workers(cfg: dict) -> int:
    raw = cfg.get("resin_sticky_workers")
    if raw in (None, ""):
        raw = os.environ.get("RESIN_STICKY_WORKERS", DEFAULT_WARM_WORKERS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_WARM_WORKERS
    return max(1, min(MAX_WARM_WORKERS, value))


def _get_lease(base: str, token: str, platform_id: str, account: str) -> dict | None:
    """Read one sticky lease without scanning the paginated lease list."""
    account = str(account or "").strip()
    if not account:
        return None
    path = f"/api/v1/platforms/{platform_id}/leases/{quote(account, safe='')}"
    try:
        payload = _request(base, token, "GET", path)
    except ResinStickyError as exc:
        if " HTTP 404:" in str(exc):
            return None
        raise
    if isinstance(payload, dict) and str(payload.get("account") or "").strip() == account:
        return payload
    if isinstance(payload, dict):
        for item in list(payload.get("items") or []):
            if str(item.get("account") or "").strip() == account:
                return item
    return None


def _delete_lease(base: str, token: str, platform_id: str, account: str) -> None:
    import urllib.parse

    account = str(account or "").strip()
    if not account:
        return
    path = f"/api/v1/platforms/{platform_id}/leases/{urllib.parse.quote(account, safe='')}"
    try:
        _request(base, token, "DELETE", path)
    except ResinStickyError:
        pass


def _lease_alive(item: dict, now: datetime | None = None) -> bool:
    raw = str(item.get("expiry") or "").strip()
    if not raw:
        return True
    now = now or datetime.now(timezone.utc)
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        expiry = datetime.fromisoformat(text)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > now


def _probe_exit_ip(proxy_url: str, timeout: float = PROBE_TIMEOUT) -> str:
    import requests

    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy_url, "https": proxy_url}
    last_error = None
    for endpoint in ("https://api.ipify.org?format=json", "https://ipinfo.io/json"):
        try:
            response = session.get(
                endpoint,
                proxies=proxies,
                timeout=(min(4.0, timeout), timeout),
                headers={"Accept": "application/json", "User-Agent": "GrokRegister/1"},
            )
            response.raise_for_status()
            payload = response.json()
            ip = str(payload.get("ip") or "").strip()
            if ip:
                return ip
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise ResinStickyError(redact_log_line(str(last_error or "probe failed")))


def _entry(
    *,
    sticky: str,
    node_hash: str,
    url: str,
    egress_ip: str = "",
    node_tag: str = "",
    source_lease: str = "",
) -> dict:
    tag = str(node_tag or "").strip()
    label = tag or f"node {str(node_hash or '')[:16]}"
    return {
        "sticky": sticky,
        "node_hash": node_hash,
        "url": url,
        "egress_ip": egress_ip,
        "node_tag": tag,
        "label": f"{label} · {sticky}",
        "source_lease": source_lease,
    }


def _warm_one(base: str, token: str, platform_id: str, platform: str, proxy: str, taken: set[str]) -> dict:
    sticky = f"grokreg-node-{uuid.uuid4().hex[:12]}"
    url = with_resin_sticky_account(proxy, sticky, platform)
    ip = _probe_exit_ip(url)
    lease = None
    for _ in range(6):
        time.sleep(0.2)
        lease = _get_lease(base, token, platform_id, sticky)
        if lease:
            break
    if lease is None:
        raise ResinStickyError("lease not visible after probe")
    node_hash = str(lease.get("node_hash") or "").strip()
    if not node_hash:
        _delete_lease(base, token, platform_id, sticky)
        raise ResinStickyError("lease missing node_hash")
    if node_hash in taken:
        _delete_lease(base, token, platform_id, sticky)
        raise ResinStickyError(f"collision {node_hash[:16]}")
    return _entry(
        sticky=sticky,
        node_hash=node_hash,
        url=url,
        egress_ip=str(lease.get("egress_ip") or ip or ""),
        node_tag=str(lease.get("node_tag") or ""),
        source_lease="warm",
    )


def _collect_existing(base: str, token: str, platform_id: str, platform: str, proxy: str) -> list[dict]:
    mapped: dict[str, dict] = {}
    for item in _list_leases(base, token, platform_id):
        if not _lease_alive(item):
            continue
        node_hash = str(item.get("node_hash") or "").strip()
        sticky = str(item.get("account") or "").strip()
        if not node_hash or not sticky or node_hash in mapped:
            continue
        mapped[node_hash] = _entry(
            sticky=sticky,
            node_hash=node_hash,
            url=with_resin_sticky_account(proxy, sticky, platform),
            egress_ip=str(item.get("egress_ip") or ""),
            node_tag=str(item.get("node_tag") or ""),
            source_lease="existing",
        )
    return list(mapped.values())


def _replace_pool(entries: list[dict]) -> dict:
    from webui import proxy_store

    with proxy_store.exclusive_file_lock(proxy_store.LOCK_PATH):
        state, _ = proxy_store._read_unlocked()
        kept = []
        for item in state["items"]:
            source = str(item.get("source") or "")
            parsed = urlsplit(item.get("url") or "")
            username = unquote(parsed.username or "")
            is_resin_mixed = (parsed.hostname or "") in {"127.0.0.1", "localhost", "172.17.0.1"} and parsed.port == 2260
            bare_platform = "." not in username
            if source in {"resin-sticky", "config.proxy"} or (is_resin_mixed and bare_platform):
                continue
            kept.append(item)
        now = proxy_store._utc_now()
        for entry in entries:
            item = proxy_store._normalize_item(
                {
                    "url": entry["url"],
                    "enabled": True,
                    "status": "healthy" if entry.get("egress_ip") else "unknown",
                    "exit_ip": entry.get("egress_ip") or "",
                    "label": entry.get("label") or "",
                    "source": "resin-sticky",
                    "created_at": now,
                    "last_checked_at": now if entry.get("egress_ip") else "",
                }
            )
            if item:
                kept.append(item)
        state["items"] = kept
        proxy_store._write_unlocked(state)
    return proxy_store.read_proxy_pool()


def resin_sync_status() -> dict:
    with _SYNC_LOCK:
        return dict(_SYNC_JOB)


def sync_resin_stickies(*, target: object = None, warm: bool = True) -> dict:
    cfg = _load_config()
    token = _admin_token(cfg)
    if not token:
        raise ResinStickyError("config.json missing resin_admin_token")
    proxy = str(cfg.get("proxy") or "").strip()
    if not proxy:
        raise ResinStickyError("config.json missing proxy")
    base = _admin_base(cfg)
    platform_name = _platform_name(cfg)
    want = _target_count(cfg, target)
    platform = _resolve_platform(base, token, platform_name)
    platform_id = str(platform.get("id") or "")
    if not platform_id:
        raise ResinStickyError("platform id empty")
    warm_workers = _warm_workers(cfg)

    existing = _collect_existing(base, token, platform_id, platform_name, proxy)
    mapped: dict[str, dict] = {item["node_hash"]: item for item in existing}
    probed = 0
    collisions = 0
    errors = 0

    with _SYNC_LOCK:
        _SYNC_JOB.update({"phase": "existing", "mapped": len(mapped), "target": want, "workers": warm_workers})

    if warm and len(mapped) < want:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        attempts = 0
        max_attempts = max(want * 3, want + 8)
        while len(mapped) < want and attempts < max_attempts:
            batch = min(warm_workers, want - len(mapped), max_attempts - attempts)
            taken = set(mapped)
            with _SYNC_LOCK:
                _SYNC_JOB.update({"phase": "warming", "mapped": len(mapped), "probed": probed, "workers": warm_workers})
            with ThreadPoolExecutor(max_workers=batch) as pool:
                futs = [
                    pool.submit(_warm_one, base, token, platform_id, platform_name, proxy, taken)
                    for _ in range(batch)
                ]
                for fut in as_completed(futs):
                    attempts += 1
                    probed += 1
                    try:
                        entry = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        message = str(exc)
                        if str(message).startswith("collision "):
                            collisions += 1
                        else:
                            errors += 1
                        continue
                    if entry["node_hash"] in mapped:
                        collisions += 1
                        _delete_lease(base, token, platform_id, entry["sticky"])
                        continue
                    mapped[entry["node_hash"]] = entry

    entries = list(mapped.values())
    entries.sort(key=lambda item: (str(item.get("node_tag") or ""), item["node_hash"]))
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        MAP_PATH,
        {
            "updated_at": _utc_now(),
            "platform": platform_name,
            "platform_id": platform_id,
            "target": want,
            "count": len(entries),
            "items": [
                {
                    "sticky": item["sticky"],
                    "node_hash": item["node_hash"],
                    "node_tag": item.get("node_tag") or "",
                    "egress_ip": item.get("egress_ip") or "",
                    "label": item.get("label") or "",
                }
                for item in entries
            ],
        },
    )
    os.chmod(MAP_PATH, 0o600)
    pool = _replace_pool(entries)
    result = {
        "ok": True,
        "platform": platform_name,
        "routable_node_count": platform.get("routable_node_count"),
        "target": want,
        "mapped": len(entries),
        "existing": sum(1 for item in entries if item.get("source_lease") == "existing"),
        "warmed": sum(1 for item in entries if item.get("source_lease") == "warm"),
        "probed": probed,
        "collisions": collisions,
        "errors": errors,
        "unique_nodes": len({item["node_hash"] for item in entries}),
    }
    result.update({k: pool.get(k) for k in ("items", "summary", "legacy", "updated_at", "test_job") if k in pool})
    result["ok"] = True
    return result


def start_resin_sync(*, target: object = None) -> dict:
    with _SYNC_LOCK:
        if _SYNC_JOB.get("running"):
            status = dict(_SYNC_JOB)
            status.update({"ok": False, "running": True, "error": "sync already running"})
            return status
        job_id = uuid.uuid4().hex[:12]
        _SYNC_JOB.update(
            {
                "running": True,
                "job_id": job_id,
                "phase": "starting",
                "target": 0,
                "workers": 0,
                "mapped": 0,
                "probed": 0,
                "collisions": 0,
                "errors": 0,
                "started_at": _utc_now(),
                "finished_at": None,
                "error": "",
            }
        )

    def _runner() -> None:
        try:
            result = sync_resin_stickies(target=target, warm=True)
            with _SYNC_LOCK:
                _SYNC_JOB.update(
                    {
                        "running": False,
                        "phase": "done",
                        "mapped": result.get("mapped") or 0,
                        "probed": result.get("probed") or 0,
                        "collisions": result.get("collisions") or 0,
                        "errors": result.get("errors") or 0,
                        "finished_at": _utc_now(),
                        "error": "",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            with _SYNC_LOCK:
                _SYNC_JOB.update(
                    {
                        "running": False,
                        "phase": "error",
                        "finished_at": _utc_now(),
                        "error": redact_log_line(str(exc)),
                    }
                )

    threading.Thread(target=_runner, name="resin-sticky-sync", daemon=True).start()
    with _SYNC_LOCK:
        status = dict(_SYNC_JOB)
    status["ok"] = True
    return status
