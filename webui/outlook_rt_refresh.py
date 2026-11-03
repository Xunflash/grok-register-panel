# -*- coding: utf-8 -*-
"""Background refresh of Outlook pool refresh tokens, including used rows."""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from pathlib import Path

DEFAULT_WORKERS = 16
MAX_WORKERS = 32

_lock = threading.Lock()
_cancel = threading.Event()
_thread: threading.Thread | None = None
_state = {
    "running": False,
    "workers": DEFAULT_WORKERS,
    "picked": 0,
    "done": 0,
    "ok_count": 0,
    "rotated": 0,
    "saved": 0,
    "failed": 0,
    "error": "",
    "started_at": "",
    "finished_at": "",
}


def status() -> dict:
    with _lock:
        data = dict(_state)
    data["ok"] = True
    return data


def registration_active() -> bool:
    """True when a panel registration batch is already using the inventory."""
    needles = ("run_batch_headless.py", "run_until_100.py")
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for pid in entries:
        if not pid.isdigit():
            continue
        try:
            raw = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if "python -c" in raw or "python3 -c" in raw:
            continue
        if any(needle in raw for needle in needles):
            return True
    return False


def _tool():
    path = Path(__file__).resolve().parents[1] / "scripts" / "refresh_outlook_pool_rt.py"
    spec = importlib.util.spec_from_file_location("refresh_outlook_pool_rt", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("outlook refresh script missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worker_count(value) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_WORKERS
    return max(1, min(MAX_WORKERS, parsed or DEFAULT_WORKERS))


def start(workers=None) -> dict:
    if registration_active():
        return {"ok": False, "error": "注册任务进行中，先停止再刷新 refresh token", "running": False}
    count = _worker_count(workers if workers is not None else DEFAULT_WORKERS)
    global _thread
    with _lock:
        if _state["running"]:
            return {"ok": False, "error": "Outlook RT 刷新已在运行", "running": True}
        _state["running"] = True
        _state["error"] = ""
    try:
        tool = _tool()
        inventory = tool.default_inventory()
        used = tool.default_used(inventory)
        accounts = tool.pick_accounts(inventory, used, [], 10**9, include_used=True)
        proxy_urls = tool.resin_outlook_proxies(count)
    except SystemExit:
        with _lock:
            _state["running"] = False
        return {"ok": False, "error": "代理未配置", "running": False}
    except Exception as exc:
        with _lock:
            _state["running"] = False
            _state["error"] = type(exc).__name__
        return {"ok": False, "error": type(exc).__name__, "running": False}
    if not accounts:
        with _lock:
            _state["running"] = False
        return {"ok": False, "error": "没有可刷新的邮箱", "running": False}
    _cancel.clear()
    with _lock:
        _state.update(
            {
                "running": True,
                "workers": count,
                "picked": len(accounts),
                "done": 0,
                "ok_count": 0,
                "rotated": 0,
                "saved": 0,
                "failed": 0,
                "error": "",
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "finished_at": "",
            }
        )
    thread = threading.Thread(
        target=_run,
        kwargs={
            "tool": tool,
            "inventory": inventory,
            "accounts": accounts,
            "proxy_urls": proxy_urls,
            "workers": count,
        },
        name="outlook-rt-refresh",
        daemon=True,
    )
    _thread = thread
    thread.start()
    return status()


def _run(tool, inventory: str, accounts: list, proxy_urls: list, workers: int) -> None:
    error = ""
    try:
        def on_report(report: dict) -> None:
            with _lock:
                _state["done"] += 1
                if report.get("ok"):
                    _state["ok_count"] += 1
                else:
                    _state["failed"] += 1
                if report.get("rotated"):
                    _state["rotated"] += 1
                if report.get("saved"):
                    _state["saved"] += 1

        tool.refresh_accounts(
            inventory,
            accounts,
            workers=workers,
            proxy_urls=proxy_urls,
            on_report=on_report,
            should_stop=_cancel.is_set,
        )
    except Exception as exc:
        error = type(exc).__name__
    with _lock:
        _state["running"] = False
        _state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if error and not _state["error"]:
            _state["error"] = error
        elif _cancel.is_set() and not error:
            _state["error"] = "已停止"


def stop() -> dict:
    _cancel.set()
    thread = _thread
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    return status()
