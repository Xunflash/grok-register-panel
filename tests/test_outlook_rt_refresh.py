# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webui.outlook_rt_refresh as job


def test_homepage_exposes_outlook_refresh_and_warning_log():
    source = (ROOT / "webui" / "monitor.py").read_text(encoding="utf-8")
    assert 'id="outlook-rt-refresh"' in source
    assert "/api/outlook-rt/refresh" in source
    assert "警告日志" in source
    assert "本批代理流量" in source


def test_start_refuses_while_registration_is_running():
    previous = job.registration_active
    job.registration_active = lambda: True
    with job._lock:
        job._state["running"] = False
    try:
        result = job.start()
    finally:
        job.registration_active = previous
    assert result["ok"] is False
    assert result["running"] is False
    assert job.status()["running"] is False


def test_refresh_progress_omits_tokens_and_proxy():
    secret_rt = "rt-secret-value"
    secret_proxy = "http://user:proxy-secret@proxy.example:8080"
    previous_active = job.registration_active
    previous_tool = job._tool

    class Tool:
        def default_inventory(self):
            return "inventory.txt"

        def default_used(self, _inventory):
            return "inventory.txt.used"

        def pick_accounts(self, *_args, **kwargs):
            assert kwargs.get("include_used") is True
            return [{"email": "one@outlook.com", "refresh_token": secret_rt, "client_id": "cid"}]

        def resin_outlook_proxies(self, count):
            return [secret_proxy] * count

        def refresh_accounts(self, _inventory, accounts, **kwargs):
            assert accounts[0]["refresh_token"] == secret_rt
            kwargs["on_report"](
                {"ok": True, "rotated": True, "saved": True, "error": "", "channel": "imap"}
            )
            return [{}]

    job.registration_active = lambda: False
    job._tool = lambda: Tool()
    with job._lock:
        job._state["running"] = False
    try:
        started = job.start(16)
        assert started["ok"] is True
        assert started["picked"] == 1
        assert started["workers"] == 16
        job._thread.join(timeout=3)
        data = job.status()
    finally:
        job.registration_active = previous_active
        job._tool = previous_tool
        with job._lock:
            job._state["running"] = False
    blob = json.dumps(data)
    assert secret_rt not in blob
    assert "proxy-secret" not in blob
    assert "one@outlook.com" not in blob
    assert data["done"] == 1
    assert data["ok_count"] == 1
    assert data["saved"] == 1
    assert data["running"] is False


if __name__ == "__main__":
    test_homepage_exposes_outlook_refresh_and_warning_log()
    test_start_refuses_while_registration_is_running()
    test_refresh_progress_omits_tokens_and_proxy()
    print("ok")
