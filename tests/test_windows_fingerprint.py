# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from browser_session import (  # noqa: E402
    DEFAULT_BROWSER_OS,
    _SKIP_RENDERER_SUBSTR,
    _pick_webgl_config,
    create_browser_options,
    exit_ip_allowed,
    exit_ip_prefixes,
    fingerprint_enhance_enabled,
    format_fingerprint_log,
    fp_screen_choices,
    fp_timezone_override,
    parse_screen_spec,
    resolve_browser_os,
)


@contextmanager
def _env(**values):
    old = {}
    try:
        for key, value in values.items():
            old[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _isolated_launch(proxies=None, exit_ip="1.2.3.4", blocked=False):
    import browser_session as bs

    orig = {
        "proxies": bs._proxies,
        "exe": bs._detect_camoufox_exe,
        "resolve_ip": bs._resolve_proxy_exit_ip,
        "blocked": bs.is_blocked_exit_ip,
        "meter": bs.meter_proxy_url,
        "pick_webgl": bs._pick_webgl_config,
        "screen": bs._xvfb_screen_constraint,
        "cache": dict(bs._WEBGL_PAIRS_CACHE),
    }

    class _DummyScreen:
        def __init__(self):
            self.min_width = 1280
            self.max_width = 1920
            self.min_height = 720
            self.max_height = 1080

    bs._proxies = lambda: (proxies or {})
    bs._detect_camoufox_exe = lambda: ""
    bs._resolve_proxy_exit_ip = lambda *a, **k: exit_ip
    bs.is_blocked_exit_ip = lambda ip: (blocked, "test-meta")
    bs.meter_proxy_url = lambda url: url
    bs._pick_webgl_config = lambda os_name: (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0)",
    )
    bs._xvfb_screen_constraint = lambda: _DummyScreen()
    try:
        yield bs
    finally:
        bs._proxies = orig["proxies"]
        bs._detect_camoufox_exe = orig["exe"]
        bs._resolve_proxy_exit_ip = orig["resolve_ip"]
        bs.is_blocked_exit_ip = orig["blocked"]
        bs.meter_proxy_url = orig["meter"]
        bs._pick_webgl_config = orig["pick_webgl"]
        bs._xvfb_screen_constraint = orig["screen"]
        bs._WEBGL_PAIRS_CACHE.clear()
        bs._WEBGL_PAIRS_CACHE.update(orig["cache"])


def _assert_click_untouched(opts: dict) -> None:
    assert opts["humanize"] is True
    assert opts["block_webrtc"] is True
    assert "disable_coop" not in opts
    prefs = opts.get("firefox_user_prefs") or {}
    assert "browser.tabs.remote.useCrossOriginOpenerPolicy" not in prefs


def test_resolve_browser_os_default_is_windows():
    with _env(GROK_BROWSER_OS=None):
        assert resolve_browser_os() == "windows"
        assert DEFAULT_BROWSER_OS == "windows"


def test_resolve_browser_os_aliases_and_fallback():
    assert resolve_browser_os("win") == "windows"
    assert resolve_browser_os("WIN32") == "windows"
    assert resolve_browser_os("mac") == "macos"
    assert resolve_browser_os("darwin") == "macos"
    assert resolve_browser_os("lin") == "linux"
    assert resolve_browser_os("nope") == "windows"
    assert resolve_browser_os("") == "windows"


def test_resolve_browser_os_env_override():
    with _env(GROK_BROWSER_OS="linux"):
        assert resolve_browser_os() == "linux"


def test_create_browser_options_pins_windows():
    with _env(GROK_BROWSER_OS=None, GROK_FP_ENHANCE=None):
        with _isolated_launch():
            opts = create_browser_options(unique_profile=False)
            assert opts["os"] == "windows"
            _assert_click_untouched(opts)


def test_enhance_on_no_proxy_keeps_en_us_and_adds_screen_webgl():
    with _env(GROK_FP_ENHANCE=None, GROK_BROWSER_OS=None):
        with _isolated_launch(proxies={}):
            opts = create_browser_options(unique_profile=False)
            assert opts["locale"] == "en-US"
            assert "screen" in opts
            assert opts["webgl_config"][1]
            assert "gtx 980" not in opts["webgl_config"][1].lower()
            _assert_click_untouched(opts)


def test_enhance_on_proxy_drops_locale_for_geoip():
    with _env(GROK_FP_ENHANCE="1"):
        with _isolated_launch(proxies={"https": "http://user:pass@proxy.example:8080"}):
            opts = create_browser_options(unique_profile=False)
            assert "locale" not in opts
            assert opts["geoip"] == "1.2.3.4"
            assert "screen" in opts
            assert "webgl_config" in opts
            _assert_click_untouched(opts)


def test_enhance_off_restores_old_launch_fingerprint():
    with _env(
        GROK_FP_ENHANCE="0",
        GROK_FP_SCREENS=None,
        GROK_FP_SCREEN=None,
        GROK_FP_TIMEZONE=None,
        GROK_EXIT_IP_PREFIXES=None,
    ):
        assert fingerprint_enhance_enabled() is False
        with _isolated_launch(proxies={"https": "http://user:pass@proxy.example:8080"}):
            opts = create_browser_options(unique_profile=False)
            assert opts["locale"] == "en-US"
            assert "screen" not in opts
            assert "webgl_config" not in opts
            assert opts["geoip"] == "1.2.3.4"
            _assert_click_untouched(opts)


def test_pick_webgl_skips_gtx980_and_software():
    import browser_session as bs

    fake = {
        "win": [
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0)"),
            ("Google Inc. (Google)", "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)))"),
            ("Google Inc. (Microsoft)", "ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0)"),
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0)"),
        ]
    }
    orig_cache = dict(bs._WEBGL_PAIRS_CACHE)
    bs._WEBGL_PAIRS_CACHE.clear()
    bs._WEBGL_PAIRS_CACHE["win"] = fake["win"]
    try:
        for _ in range(20):
            pair = _pick_webgl_config("windows")
            assert pair is not None
            renderer = pair[1].lower()
            for skip in _SKIP_RENDERER_SUBSTR:
                assert skip not in renderer
        assert pair[1].endswith("GTX 1650 Direct3D11 vs_5_0 ps_5_0)")
    finally:
        bs._WEBGL_PAIRS_CACHE.clear()
        bs._WEBGL_PAIRS_CACHE.update(orig_cache)


def test_format_fingerprint_log_flags_linux_leak():
    line = format_fingerprint_log(
        "windows",
        {
            "ua": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
            "platform": "Linux x86_64",
            "oscpu": "Linux x86_64",
            "webgl": False,
            "renderer": "",
            "fontsWin": False,
        },
    )
    assert "os=windows" in line
    assert "linux-ua" in line
    assert "no-webgl" in line
    assert "no-segoe" in line


def test_format_fingerprint_log_clean_windows():
    line = format_fingerprint_log(
        "windows",
        {
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
            "platform": "Win32",
            "oscpu": "Windows NT 10.0; Win64; x64",
            "webgl": True,
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0)",
            "fontsWin": True,
            "tz": "America/Los_Angeles",
            "langs": "en-US,en",
            "hw": 8,
            "screenW": 1920,
            "screenH": 1080,
        },
    )
    assert "platform=Win32" in line
    assert "leak=none" in line
    assert "ANGLE" in line
    assert "tz=America/Los_Angeles" in line
    assert "screen=1920x1080" in line


def test_parse_screen_spec_and_choices():
    assert parse_screen_spec("1920x1080") == (1920, 1080)
    assert parse_screen_spec("2560X1440") == (2560, 1440)
    assert parse_screen_spec("nope") is None
    assert parse_screen_spec("100x100") is None
    with _env(GROK_FP_SCREENS="1920x1080,2560x1440,bad", GROK_FP_SCREEN=None):
        assert fp_screen_choices() == [(1920, 1080), (2560, 1440)]
    with _env(GROK_FP_SCREENS=None, GROK_FP_SCREEN="1920x1080"):
        assert fp_screen_choices() == [(1920, 1080)]
    with _env(GROK_FP_TIMEZONE="America/Sao_Paulo"):
        assert fp_timezone_override() == "America/Sao_Paulo"


def test_exit_ip_prefix_allowlist():
    with _env(GROK_EXIT_IP_PREFIXES=None):
        assert exit_ip_prefixes() == []
        assert exit_ip_allowed("65.111.1.2") is True
    with _env(GROK_EXIT_IP_PREFIXES="104.207,209.50.0.0/16"):
        assert exit_ip_prefixes() == ["104.207.", "209.50."]
        assert exit_ip_allowed("104.207.61.254") is True
        assert exit_ip_allowed("209.50.173.147") is True
        assert exit_ip_allowed("65.111.9.164") is False
        assert exit_ip_allowed("") is False


def test_enhance_off_screen_override_and_timezone():
    class _Pinned:
        min_width = 1920
        max_width = 1920
        min_height = 1080
        max_height = 1080

    with _env(
        GROK_FP_ENHANCE="0",
        GROK_FP_TIMEZONE="America/Sao_Paulo",
        GROK_FP_SCREENS="1920x1080",
        GROK_EXIT_IP_PREFIXES="1.2",
    ):
        with _isolated_launch(proxies={"https": "http://user:pass@proxy.example:8080"}):
            import browser_session as bs

            orig = bs._screen_from_wh
            pinned = _Pinned()
            bs._screen_from_wh = lambda w, h: pinned
            try:
                opts = create_browser_options(unique_profile=False)
            finally:
                bs._screen_from_wh = orig
            assert opts["locale"] == "en-US"
            assert "webgl_config" not in opts
            assert opts["config"]["timezone"] == "America/Sao_Paulo"
            assert opts["screen"] is pinned
            assert opts["geoip"] == "1.2.3.4"
            _assert_click_untouched(opts)


def test_exit_ip_allowlist_rejects_outside_prefix():
    with _env(GROK_FP_ENHANCE="0", GROK_EXIT_IP_PREFIXES="104.207"):
        with _isolated_launch(
            proxies={"https": "http://user:pass@proxy.example:8080"},
            exit_ip="65.111.1.2",
        ):
            try:
                create_browser_options(unique_profile=False)
            except RuntimeError as exc:
                assert "出口IP命中黑名单" in str(exc)
                assert "65.111.1.2" in str(exc)
            else:
                raise AssertionError("expected allowlist reject")


def test_patchright_fingerprint_follows_exit_and_rotates_profile():
    import browser_session as session

    class _Geo:
        timezone = "America/Chicago"
        latitude = 41.8
        longitude = -87.6
        locale = "pt-BR"

    previous_ip = session._resolve_proxy_exit_ip
    previous_webgl = session._pick_webgl_config
    previous_version = session.chromium_product_version
    renders = iter(
        [
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, RTX A)"),
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, RTX B)"),
        ]
    )
    session._resolve_proxy_exit_ip = lambda *_args, **_kwargs: "8.8.8.8"
    session._pick_webgl_config = lambda *_args, **_kwargs: next(renders)
    session.chromium_product_version = lambda *_args, **_kwargs: "145.0.7632.159"
    import sys
    import types

    fake_geo = types.ModuleType("camoufox.geolocation")
    fake_geo.get_geolocation = lambda *_args, **_kwargs: _Geo()
    previous_camoufox = sys.modules.get("camoufox")
    previous_geo = sys.modules.get("camoufox.geolocation")
    sys.modules.setdefault("camoufox", types.ModuleType("camoufox"))
    sys.modules["camoufox.geolocation"] = fake_geo
    try:
        first = session.patchright_fingerprint("http://user:secret@proxy.example:8080")
        second = session.patchright_fingerprint("http://user:secret@proxy.example:8080")
    finally:
        if previous_geo is None:
            sys.modules.pop("camoufox.geolocation", None)
        else:
            sys.modules["camoufox.geolocation"] = previous_geo
        if previous_camoufox is None:
            sys.modules.pop("camoufox", None)
        else:
            sys.modules["camoufox"] = previous_camoufox
        session._resolve_proxy_exit_ip = previous_ip
        session._pick_webgl_config = previous_webgl
        session.chromium_product_version = previous_version
    assert first["locale"] == "en-US"
    assert first["timezone_id"] == "America/Chicago"
    assert first["geolocation"]["latitude"] == 41.8
    assert "Windows NT 10.0" in first["user_agent"]
    assert "145.0.7632.159" in first["user_agent"]
    assert "secret" not in first["user_agent"]
    assert first["screen"] != second["screen"] or first["webgl_renderer"] != second["webgl_renderer"]
    assert first["webgl_renderer"] != second["webgl_renderer"]
    hints = first["ua_metadata"]
    assert hints["fullVersion"] == "145.0.7632.159"
    assert hints["architecture"] == "x86"
    assert hints["platform"] == "Windows"
    assert first["languages"] == ["en-US", "en"]
    assert first["hardware_concurrency"] in {4, 8, 12}
    assert first["device_memory"] in {8, 16}
    profile = session.castle_node_profile(first)
    blob = json.dumps(profile)
    assert "secret" not in blob
    assert "127.0.0.1" not in blob
    assert profile["screen"][0] > 0
    assert profile["timezone_id"] == "America/Chicago"
    assert "Chrome/145.0.7632.159" in profile["user_agent"]
    assert "Windows NT 10.0" in profile["user_agent"]
    assert profile["platform"] == "Win32"
    assert profile["webgl_vendor"] == "Google Inc. (NVIDIA)"
    assert profile["webgl_renderer"] == "ANGLE (NVIDIA, RTX A)"
    assert profile["ua_metadata"]["platform"] == "Windows"
    assert profile["ua_metadata"]["fullVersion"] == "145.0.7632.159"
    assert [item["brand"] for item in profile["ua_metadata"]["brands"]] == [
        "Chromium",
        "Google Chrome",
        "Not-A.Brand",
    ]


def test_format_fingerprint_log_flags_screen_gt_xvfb():
    line = format_fingerprint_log(
        "windows",
        {
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0",
            "platform": "Win32",
            "webgl": True,
            "renderer": "ok",
            "fontsWin": True,
            "screenW": 2560,
            "screenH": 1440,
        },
    )
    assert "screen>xvfb" in line


if __name__ == "__main__":
    test_resolve_browser_os_default_is_windows()
    test_resolve_browser_os_aliases_and_fallback()
    test_resolve_browser_os_env_override()
    test_create_browser_options_pins_windows()
    test_enhance_on_no_proxy_keeps_en_us_and_adds_screen_webgl()
    test_enhance_on_proxy_drops_locale_for_geoip()
    test_enhance_off_restores_old_launch_fingerprint()
    test_pick_webgl_skips_gtx980_and_software()
    test_format_fingerprint_log_flags_linux_leak()
    test_format_fingerprint_log_clean_windows()
    test_patchright_fingerprint_follows_exit_and_rotates_profile()
    test_format_fingerprint_log_flags_screen_gt_xvfb()
    test_parse_screen_spec_and_choices()
    test_exit_ip_prefix_allowlist()
    test_enhance_off_screen_override_and_timezone()
    test_exit_ip_allowlist_rejects_outside_prefix()
    print("OK windows fingerprint")
