# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grok2api_client import Grok2APIClient  # noqa: E402
from webui import monitor  # noqa: E402


class _Grok2APIStub(BaseHTTPRequestHandler):
    """Local stand-in for grok2api admin login + accounts/summary."""

    username = "admin"
    password = "secret"
    token = "stub-access-token"
    available = 42
    aggregated = 8700
    logins = 0
    summaries = 0

    def log_message(self, format, *args):  # noqa: A003
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {}
        if path != "/api/admin/v1/auth/login":
            self._json(404, {"error": {"message": "not found"}})
            return
        type(self).logins += 1
        if body.get("username") != self.username or body.get("password") != self.password:
            self._json(
                401,
                {"error": {"code": "invalidCredentials", "message": "管理员账号或密码错误"}},
            )
            return
        self._json(
            200,
            {
                "data": {
                    "admin": {"id": "1", "username": self.username},
                    "tokens": {"accessToken": self.token},
                }
            },
        )

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/admin/v1/accounts/summary":
            self._json(404, {"error": {"message": "not found"}})
            return
        auth = self.headers.get("Authorization") or ""
        if auth != f"Bearer {self.token}":
            self._json(
                401,
                {"error": {"code": "unauthorized", "message": "未登录"}},
            )
            return
        type(self).summaries += 1
        self._json(
            200,
            {
                "data": {
                    "total": 80,
                    "available": self.aggregated,
                    "recovering": 3,
                    "attention": 1,
                    "risk": 0,
                    "providers": {
                        "grok_build": {"total": 80, "available": self.available},
                        "grok_web": {"total": 4000, "available": 4092},
                        "grok_console": {"total": 4000, "available": 4531},
                    },
                    "recovery": {"cooldown": 0, "waitingReset": 0, "probing": 0},
                    "issues": {"disabled": 0, "reauthRequired": 0},
                }
            },
        )


def _start_stub(available: int = 42) -> tuple[ThreadingHTTPServer, str]:
    _Grok2APIStub.available = available
    _Grok2APIStub.logins = 0
    _Grok2APIStub.summaries = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Grok2APIStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_status_projection_allowlists_auto_register():
    from webui.api_projection import summary

    projected = summary(
        {
            "control": {
                "workers": 3,
                "mode": "orch",
                "turnstile_provider": "yescaptcha",
                "registration_scheme": "browser",
                "auto_register_threshold": 100,
                "auto_register_cooldown_min": 120,
                "auto_register_enabled": True,
                "auto_register_last_at": 123.0,
                "secret": "nope",
            }
        }
    )
    control = projected["control"]
    assert control["auto_register_threshold"] == 100
    assert control["auto_register_cooldown_min"] == 120
    assert control["auto_register_enabled"] is True
    assert "auto_register_last_at" not in control
    assert "secret" not in control


def test_save_control_persists_auto_register():
    with tempfile.TemporaryDirectory() as temp:
        control_file = Path(temp) / "monitor_control.json"
        previous = monitor.CONTROL_FILE
        monitor.CONTROL_FILE = control_file
        try:
            saved = monitor.save_control(
                {
                    "auto_register_threshold": 80,
                    "auto_register_cooldown_min": 45,
                    "auto_register_enabled": True,
                    "workers": 2,
                }
            )
        finally:
            monitor.CONTROL_FILE = previous
        assert saved["auto_register_threshold"] == 80
        assert saved["auto_register_cooldown_min"] == 45
        assert saved["auto_register_enabled"] is True
        disk = json.loads(control_file.read_text(encoding="utf-8"))
        assert disk["auto_register_threshold"] == 80
        assert disk["auto_register_cooldown_min"] == 45
        assert disk["auto_register_enabled"] is True


def test_control_http_round_trip_auto_register():
    token = "test-auto-reg-token-123456"
    previous_token = os.environ.get("MONITOR_TOKEN")
    with tempfile.TemporaryDirectory() as temp:
        control_file = Path(temp) / "monitor_control.json"
        previous_control = monitor.CONTROL_FILE
        monitor.CONTROL_FILE = control_file
        os.environ["MONITOR_TOKEN"] = token
        server = monitor.ThreadingHTTPServer(("127.0.0.1", 0), monitor.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            import urllib.request

            body = json.dumps(
                {
                    "auto_register_threshold": 100,
                    "auto_register_cooldown_min": 120,
                    "auto_register_enabled": True,
                    "workers": 3,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                base + "/api/control",
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                posted = json.loads(response.read().decode("utf-8"))
            assert posted["auto_register_threshold"] == 100
            assert posted["auto_register_cooldown_min"] == 120
            assert posted["auto_register_enabled"] is True
            req = urllib.request.Request(
                base + "/api/control",
                headers={"Authorization": f"Bearer {token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                loaded = json.loads(response.read().decode("utf-8"))
            assert loaded["auto_register_threshold"] == 100
            assert loaded["auto_register_cooldown_min"] == 120
            assert loaded["auto_register_enabled"] is True
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            monitor.CONTROL_FILE = previous_control
            if previous_token is None:
                os.environ.pop("MONITOR_TOKEN", None)
            else:
                os.environ["MONITOR_TOKEN"] = previous_token


def test_grok2api_client_reads_summary_available():
    server, base = _start_stub(available=17)
    try:
        with Grok2APIClient(base, "admin", "secret") as client:
            count = client.available_account_count()
        assert count == 17
        assert count != _Grok2APIStub.aggregated
        assert _Grok2APIStub.logins == 1
        assert _Grok2APIStub.summaries == 1
    finally:
        server.shutdown()
        server.server_close()


def test_grok2api_client_falls_back_to_top_level_available():
    server, base = _start_stub(available=17)
    try:
        original_get = _Grok2APIStub.do_GET

        def _get_without_build(self):
            path = urlparse(self.path).path
            if path != "/api/admin/v1/accounts/summary":
                return original_get(self)
            auth = self.headers.get("Authorization") or ""
            if auth != f"Bearer {self.token}":
                self._json(401, {"error": {"message": "未登录"}})
                return
            type(self).summaries += 1
            self._json(200, {"data": {"total": 80, "available": 66}})

        _Grok2APIStub.do_GET = _get_without_build
        try:
            with Grok2APIClient(base, "admin", "secret") as client:
                assert client.available_account_count() == 66
        finally:
            _Grok2APIStub.do_GET = original_get
    finally:
        server.shutdown()
        server.server_close()


def test_grok2api_client_rejects_wrong_password():
    server, base = _start_stub(available=3)
    try:
        try:
            with Grok2APIClient(base, "admin", "wrong") as client:
                client.available_account_count()
            raise AssertionError("expected login failure")
        except Exception as exc:
            from grok2api_client import Grok2APIImportError

            assert isinstance(exc, Grok2APIImportError)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    test_status_projection_allowlists_auto_register()
    test_save_control_persists_auto_register()
    test_control_http_round_trip_auto_register()
    test_grok2api_client_reads_summary_available()
    test_grok2api_client_falls_back_to_top_level_available()
    test_grok2api_client_rejects_wrong_password()
    print("OK auto register control g2a")
