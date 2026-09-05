# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from turnstile_solver import (  # noqa: E402
    CapSolverTurnstile,
    YesCaptchaTurnstile,
    captcha_http_session,
    ensure_page_turnstile_hooks,
    load_turnstile_settings,
    parse_yescaptcha_proxy,
    patch_post_body,
    persist_turnstile_provider,
    resolve_turnstile_provider,
    should_attach_browser_turnstile_hooks,
    solve_turnstile,
)


def test_captcha_http_session_ignores_env_proxy(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:2260")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:2260")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:2260")
    session = captcha_http_session()
    assert session.trust_env is False
    assert session.proxies == {}
    solver = CapSolverTurnstile(client_key="CAP-k")
    assert solver.s.trust_env is False
    assert solver.s.proxies == {}


def test_should_attach_hooks_for_cloud_not_local():
    assert should_attach_browser_turnstile_hooks("capsolver") is True
    assert should_attach_browser_turnstile_hooks("cap") is True
    assert should_attach_browser_turnstile_hooks("yescaptcha") is True
    assert should_attach_browser_turnstile_hooks("云打码") is True
    assert should_attach_browser_turnstile_hooks("local") is False
    assert should_attach_browser_turnstile_hooks("本地点击") is False
    assert should_attach_browser_turnstile_hooks("local", debug=True) is True
    assert (
        should_attach_browser_turnstile_hooks("auto", capsolver_key="CAP-1") is True
    )
    assert should_attach_browser_turnstile_hooks("auto") is False


def test_browser_session_attaches_hooks_for_capsolver():
    text = (ROOT / "browser_session.py").read_text(encoding="utf-8")
    assert "should_attach_browser_turnstile_hooks" in text
    assert "load_turnstile_settings" in text
    assert 'if provider == "yescaptcha" or debug_cf:' not in text
    flow = (ROOT / "register_flow.py").read_text(encoding="utf-8")
    assert "def _ensure_turnstile_page_hooks(" in flow
    assert "ensure_page_turnstile_hooks(page.run_js)" in flow


def test_ensure_page_turnstile_hooks_evaluates_init_script():
    seen = []

    def ev(js):
        seen.append(str(js))
        if "return" in str(js) and "__grokCfHooked" in str(js) and "cbs" in str(js):
            return {"cbs": 1, "hasTs": True, "hooked": True}
        return None

    meta = ensure_page_turnstile_hooks(ev)
    assert meta["hooked"] is True
    assert meta["cbs"] == 1
    assert any("ts.render" in s and "__grokCfHooked" in s for s in seen)


def test_resolve_explicit_local_keeps_key():
    assert resolve_turnstile_provider("local", yescaptcha_key="abc") == "local"
    assert resolve_turnstile_provider("本地点击", yescaptcha_key="abc") == "local"
    assert resolve_turnstile_provider("yescaptcha", yescaptcha_key="") == "yescaptcha"
    assert resolve_turnstile_provider("云打码") == "yescaptcha"
    assert resolve_turnstile_provider("auto", yescaptcha_key="abc") == "yescaptcha"
    assert resolve_turnstile_provider("auto", yescaptcha_key="") == "local"
    assert resolve_turnstile_provider("cap") == "capsolver"
    assert resolve_turnstile_provider("capsolver") == "capsolver"
    assert resolve_turnstile_provider("yes") == "yescaptcha"
    assert resolve_turnstile_provider("本地") == "local"
    assert (
        resolve_turnstile_provider("auto", yescaptcha_key="abc", capsolver_key="CAP-1")
        == "capsolver"
    )


def test_load_settings_env_overrides_config(tmp_path, monkeypatch=None):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "turnstile_provider": "local",
                "yescaptcha_key": "from-file",
                "yescaptcha_api": "https://api.yescaptcha.com",
            }
        ),
        encoding="utf-8",
    )
    old = {k: os.environ.get(k) for k in ("TURNSTILE_PROVIDER", "YESCAPTCHA_KEY", "YESCAPTCHA_API")}
    try:
        os.environ["TURNSTILE_PROVIDER"] = "yescaptcha"
        os.environ["YESCAPTCHA_KEY"] = "from-env"
        os.environ["YESCAPTCHA_API"] = "https://cn.yescaptcha.com"
        settings = load_turnstile_settings(control={"turnstile_provider": "local"}, config_path=cfg)
        assert settings["provider"] == "yescaptcha"
        assert settings["key"] == "from-env"
        assert settings["api"].rstrip("/") == "https://cn.yescaptcha.com"
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_control_local_without_env(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"turnstile_provider": "yescaptcha", "yescaptcha_key": "file-key"}),
        encoding="utf-8",
    )
    old = os.environ.pop("TURNSTILE_PROVIDER", None)
    old_key = os.environ.pop("YESCAPTCHA_KEY", None)
    try:
        settings = load_turnstile_settings(
            control={"turnstile_provider": "local"},
            config_path=cfg,
        )
        assert settings["provider"] == "local"
        assert settings["key"] == "file-key"
    finally:
        if old is None:
            os.environ.pop("TURNSTILE_PROVIDER", None)
        else:
            os.environ["TURNSTILE_PROVIDER"] = old
        if old_key is None:
            os.environ.pop("YESCAPTCHA_KEY", None)
        else:
            os.environ["YESCAPTCHA_KEY"] = old_key


def test_load_settings_capsolver_from_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "turnstile_provider": "cap",
                "capsolver_key": "CAP-from-file",
                "capsolver_api": "https://api.capsolver.com",
                "yescaptcha_key": "yes-file",
            }
        ),
        encoding="utf-8",
    )
    old = {
        k: os.environ.pop(k, None)
        for k in ("TURNSTILE_PROVIDER", "CAPSOLVER_KEY", "YESCAPTCHA_KEY")
    }
    try:
        settings = load_turnstile_settings(config_path=cfg)
        assert settings["provider"] == "capsolver"
        assert settings["key"] == "CAP-from-file"
        assert settings["capsolver_key"] == "CAP-from-file"
        assert settings["yescaptcha_key"] == "yes-file"
        assert settings["api"].rstrip("/") == "https://api.capsolver.com"
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_persist_turnstile_provider_keeps_other_keys(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"proxy": "http://127.0.0.1:9", "yescaptcha_key": "keep"}),
        encoding="utf-8",
    )
    resolved = persist_turnstile_provider("cap", cfg)
    assert resolved == "capsolver"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["turnstile_provider"] == "capsolver"
    assert data["proxy"] == "http://127.0.0.1:9"
    assert data["yescaptcha_key"] == "keep"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_patch_post_body_fills_existing_captcha_field():
    token = "t" * 90
    out = patch_post_body('{"givenName":"A","turnstileToken":""}', token)
    assert token in out
    assert "givenName" in out
    unchanged = patch_post_body('{"givenName":"A"}', token)
    assert unchanged == '{"givenName":"A"}'


def test_parse_yescaptcha_proxy_skips_local_resin():
    assert parse_yescaptcha_proxy("http://user:pass@127.0.0.1:2260") is None
    assert parse_yescaptcha_proxy("http://10.1.1.8:8080") is None
    assert parse_yescaptcha_proxy("") is None
    got = parse_yescaptcha_proxy("http://user:pass@8.8.8.8:8000")
    assert got["proxyType"] == "http"
    assert got["proxyAddress"] == "8.8.8.8"
    assert got["proxyPort"] == 8000
    assert got["proxyLogin"] == "user"
    assert got["proxyPassword"] == "pass"


def test_yescaptcha_create_and_poll():
    solver = YesCaptchaTurnstile(client_key="k", initial_delay=0, retry_delay=0.01, poll_max=5)
    posts = []

    def fake_post(url, json=None, timeout=30):
        posts.append((url, json))
        if url.endswith("/createTask"):
            assert json["task"]["type"] == "TurnstileTaskProxyless"
            assert json["task"]["websiteKey"] == "0x4AAAAAAAhr9JGVDZbrZOo0"
            assert json["clientKey"] == "k"
            return _FakeResp({"errorId": 0, "taskId": 99})
        assert json["taskId"] == "99"
        if len([1 for u, _ in posts if u.endswith("/getTaskResult")]) < 2:
            return _FakeResp({"errorId": 0, "status": "processing"})
        return _FakeResp({"errorId": 0, "status": "ready", "solution": {"token": "t" * 90}})

    solver.s.post = fake_post
    token = solver.solve("https://accounts.x.ai/sign-up", "0x4AAAAAAAhr9JGVDZbrZOo0")
    assert token == "t" * 90
    assert any(u.endswith("/createTask") for u, _ in posts)


def test_yescaptcha_create_with_public_proxy():
    solver = YesCaptchaTurnstile(client_key="k", initial_delay=0, retry_delay=0.01, poll_max=3)
    types = []

    def fake_post(url, json=None, timeout=30):
        if url.endswith("/createTask"):
            types.append(json["task"]["type"])
            assert json["task"]["proxyAddress"] == "8.8.8.8"
            assert json["task"]["proxyPort"] == 8000
            return _FakeResp({"errorId": 0, "taskId": 7})
        return _FakeResp({"errorId": 0, "status": "ready", "solution": {"token": "p" * 90}})

    solver.s.post = fake_post
    token = solver.solve(
        "https://accounts.x.ai/sign-up",
        "0x4AAAAAAAhr9JGVDZbrZOo0",
        proxy={"proxyType": "http", "proxyAddress": "8.8.8.8", "proxyPort": 8000},
    )
    assert token == "p" * 90
    assert types == ["TurnstileTask"]


def test_capsolver_create_is_proxyless_and_polls():
    solver = CapSolverTurnstile(client_key="CAP-k", initial_delay=0, retry_delay=0.01, poll_max=5)
    posts = []

    def fake_post(url, json=None, timeout=30):
        posts.append((url, json))
        if url.endswith("/createTask"):
            assert json["clientKey"] == "CAP-k"
            assert json["task"]["type"] == "AntiTurnstileTaskProxyLess"
            assert json["task"]["websiteURL"] == "https://accounts.x.ai/sign-up"
            assert json["task"]["websiteKey"] == "0x4AAAAAAAhr9JGVDZbrZOo0"
            assert json["task"]["metadata"]["action"] == "signup"
            assert "proxyAddress" not in json["task"]
            assert "userAgent" not in json["task"]
            return _FakeResp({"errorId": 0, "taskId": "61138bb6-19fb-11ec-a9c8-0242ac110006"})
        assert json["clientKey"] == "CAP-k"
        assert json["taskId"] == "61138bb6-19fb-11ec-a9c8-0242ac110006"
        if len([1 for u, _ in posts if u.endswith("/getTaskResult")]) < 2:
            return _FakeResp({"errorId": 0, "status": "idle"})
        return _FakeResp(
            {
                "errorId": 0,
                "status": "ready",
                "solution": {
                    "token": "c" * 90,
                    "type": "turnstile",
                    "userAgent": "Mozilla/5.0",
                },
            }
        )

    solver.s.post = fake_post
    token = solver.solve(
        "https://accounts.x.ai/sign-up",
        "0x4AAAAAAAhr9JGVDZbrZOo0",
        action="signup",
    )
    assert token == "c" * 90
    assert any(u.endswith("/createTask") for u, _ in posts)
    assert any(u.endswith("/getTaskResult") for u, _ in posts)


def test_solve_turnstile_dispatches_capsolver(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"turnstile_provider": "capsolver", "capsolver_key": "CAP-k"}),
        encoding="utf-8",
    )
    old = {k: os.environ.pop(k, None) for k in ("TURNSTILE_PROVIDER", "CAPSOLVER_KEY")}
    posts = []

    def fake_post(url, json=None, timeout=30):
        posts.append((url, json))
        if url.endswith("/createTask"):
            assert json["task"]["type"] == "AntiTurnstileTaskProxyLess"
            return _FakeResp({"errorId": 0, "taskId": "t1"})
        return _FakeResp({"errorId": 0, "status": "ready", "solution": {"token": "d" * 90}})

    try:
        os.environ["TURNSTILE_PROVIDER"] = "capsolver"
        os.environ["CAPSOLVER_KEY"] = "CAP-k"
        settings = load_turnstile_settings(config_path=cfg)
        from turnstile_solver import CapSolverTurnstile as Cls

        orig_init = Cls.__init__

        def wrapped(self, *args, **kwargs):
            orig_init(self, *args, **kwargs)
            self.s.post = fake_post
            self.initial_delay = 0
            self.retry_delay = 0.01

        Cls.__init__ = wrapped
        try:
            token = solve_turnstile(
                "https://accounts.x.ai/sign-up",
                "0x4AAAAAAAhr9JGVDZbrZOo0",
                settings=settings,
            )
        finally:
            Cls.__init__ = orig_init
        assert token == "d" * 90
        assert posts[0][0].endswith("/createTask")
        assert posts[0][1]["task"]["type"] == "AntiTurnstileTaskProxyLess"
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_get_turnstile_token_yescaptcha_does_not_click():
    import register_flow as rf

    clicks = []

    class _Page:
        def run_js(self, script, *args):
            text = str(script)
            if "cf-turnstile-response" in text:
                return ""
            if "data-sitekey" in text:
                return "0x4AAAAAAAhr9JGVDZbrZOo0"
            if "location.href" in text:
                return "https://accounts.x.ai/sign-up?redirect=grok-com"
            if "userAgent" in text:
                return "Mozilla/5.0"
            return 90

    rf._try_click_turnstile_frame = lambda **k: clicks.append("click")
    orig_page = rf.page
    orig_active = rf.active_page
    orig_solve = rf.solve_turnstile_yescaptcha
    orig_load = rf.load_turnstile_settings
    orig_fill = rf._fill_cf_turnstile_token
    orig_proxy = rf.get_bound_proxy
    rf._stash_cf_token("")
    rf.page = _Page()
    rf.active_page = lambda: True
    rf.get_bound_proxy = lambda: "http://127.0.0.1:2260"
    rf.load_turnstile_settings = lambda: {
        "provider": "yescaptcha",
        "key": "k",
        "api": "https://api.yescaptcha.com",
        "fallback_local": False,
        "sitekey": "0x4AAAAAAAhr9JGVDZbrZOo0",
    }
    solves = []
    rf.solve_turnstile_yescaptcha = lambda *a, **k: solves.append(k.get("proxy_url")) or "y" * 90
    rf._fill_cf_turnstile_token = lambda token: 90
    try:
        token = rf.getTurnstileToken()
        again = rf.getTurnstileToken()
        assert token == again == "y" * 90
        assert clicks == []
        assert solves == ["http://127.0.0.1:2260"]
    finally:
        rf.page = orig_page
        rf.active_page = orig_active
        rf.solve_turnstile_yescaptcha = orig_solve
        rf.load_turnstile_settings = orig_load
        rf._fill_cf_turnstile_token = orig_fill
        rf.get_bound_proxy = orig_proxy
        rf._stash_cf_token("")


def test_get_turnstile_token_capsolver_does_not_click():
    import register_flow as rf

    clicks = []

    class _Page:
        def run_js(self, script, *args):
            text = str(script)
            if "cf-turnstile-response" in text:
                return ""
            if "data-sitekey" in text:
                return "0x4AAAAAAAhr9JGVDZbrZOo0"
            if "location.href" in text:
                return "https://accounts.x.ai/sign-up?redirect=grok-com"
            if "userAgent" in text:
                return "Mozilla/5.0"
            return 90

    rf._try_click_turnstile_frame = lambda **k: clicks.append("click")
    orig_page = rf.page
    orig_active = rf.active_page
    orig_solve = rf.solve_turnstile_capsolver
    orig_load = rf.load_turnstile_settings
    orig_fill = rf._fill_cf_turnstile_token
    orig_proxy = rf.get_bound_proxy
    rf._stash_cf_token("")
    rf.page = _Page()
    rf.active_page = lambda: True
    rf.get_bound_proxy = lambda: "http://127.0.0.1:2260"
    rf.load_turnstile_settings = lambda: {
        "provider": "capsolver",
        "key": "CAP-k",
        "capsolver_key": "CAP-k",
        "api": "https://api.capsolver.com",
        "fallback_local": False,
        "sitekey": "0x4AAAAAAAhr9JGVDZbrZOo0",
    }
    solves = []
    rf.solve_turnstile_capsolver = lambda *a, **k: solves.append("cap") or "c" * 90
    rf._fill_cf_turnstile_token = lambda token: 90
    try:
        token = rf.getTurnstileToken()
        assert token == "c" * 90
        assert clicks == []
        assert solves == ["cap"]
    finally:
        rf.page = orig_page
        rf.active_page = orig_active
        rf.solve_turnstile_capsolver = orig_solve
        rf.load_turnstile_settings = orig_load
        rf._fill_cf_turnstile_token = orig_fill
        rf.get_bound_proxy = orig_proxy
        rf._stash_cf_token("")


def test_monitor_html_has_cap_yes_local():
    mon = (ROOT / "webui/monitor.py").read_text(encoding="utf-8")
    html = mon.split('HTML = r"""', 1)[1].split('"""', 1)[0]
    assert 'id="turnstile_provider"' in html
    assert 'value="capsolver">Capsolver 云打码</option>' in html
    assert 'value="yescaptcha">YesCaptcha 云打码</option>' in html
    assert 'value="local">本地点击</option>' in html
    assert '"capsolver"' in mon
    assert "persist_turnstile_provider" in mon


def test_save_control_accepts_capsolver(tmp_path, monkeypatch):
    from webui import monitor

    control_file = tmp_path / "monitor_control.json"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"proxy": "http://127.0.0.1:9"}), encoding="utf-8")
    monkeypatch.setattr(monitor, "CONTROL_FILE", control_file)
    monkeypatch.setattr(monitor, "ROOT", tmp_path)
    saved = monitor.save_control({"turnstile_provider": "cap", "workers": 2})
    assert saved["turnstile_provider"] == "capsolver"
    env = monitor._registration_env()
    assert env["TURNSTILE_PROVIDER"] == "capsolver"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["turnstile_provider"] == "capsolver"
    assert data["proxy"] == "http://127.0.0.1:9"


def test_get_turnstile_token_local_still_clicks():
    import register_flow as rf

    clicks = []
    reads = {"n": 0}

    class _Page:
        def run_js(self, script, *args):
            reads["n"] += 1
            if reads["n"] >= 2:
                return "z" * 90
            return ""

    rf._try_click_turnstile_frame = lambda **k: clicks.append("click")
    orig_page = rf.page
    orig_active = rf.active_page
    orig_load = rf.load_turnstile_settings
    orig_sleep = rf.sleep_with_cancel
    rf.page = _Page()
    rf.active_page = lambda: True
    rf.load_turnstile_settings = lambda: {
        "provider": "local",
        "key": "k",
        "fallback_local": False,
    }
    rf.sleep_with_cancel = lambda *a, **k: None
    try:
        token = rf.getTurnstileToken(budget=8)
        assert token == "z" * 90
        assert "click" in clicks
    finally:
        rf.page = orig_page
        rf.active_page = orig_active
        rf.load_turnstile_settings = orig_load
        rf.sleep_with_cancel = orig_sleep


if __name__ == "__main__":
    from pathlib import Path as _P
    import tempfile

    test_resolve_explicit_local_keeps_key()
    with tempfile.TemporaryDirectory() as d:
        test_load_settings_env_overrides_config(_P(d))
        test_control_local_without_env(_P(d))
    test_patch_post_body_fills_existing_captcha_field()
    test_parse_yescaptcha_proxy_skips_local_resin()
    test_yescaptcha_create_and_poll()
    test_yescaptcha_create_with_public_proxy()
    test_capsolver_create_is_proxyless_and_polls()
    test_get_turnstile_token_yescaptcha_does_not_click()
    test_get_turnstile_token_capsolver_does_not_click()
    test_get_turnstile_token_local_still_clicks()
    print("OK turnstile solver")
