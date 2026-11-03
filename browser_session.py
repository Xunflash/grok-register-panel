# -*- coding: utf-8 -*-
"""浏览器会话管理（线程本地 browser/page）。

使用 Camoufox（C++ 引擎层指纹伪装）替代 DrissionPage。
Camoufox 从编译层修改 Gecko，JS 完全不可检测。
"""
from __future__ import annotations

import gc
import ipaddress
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse


def _pin_playwright_node() -> None:
    """Playwright 1.60 自带 Node 24，管道对端关闭时 Unhandled EPIPE 会拖死整批。

    POSIX 优先 scripts/playwright-node；Windows 使用 node.exe + NODE_OPTIONS 注入
    EPIPE guard，避免把 bash 包装脚本交给 CreateProcess。
    必须在 import playwright / camoufox 之前设置。
    """
    from runtime_platform import apply_playwright_node_env

    apply_playwright_node_env()


_pin_playwright_node()

import asyncio
from greenlet import greenlet
from typing import cast as _tcast

from camoufox.sync_api import Camoufox as _Camoufox, NewBrowser
from playwright._impl._connection import Connection as _PwConnection
from playwright._impl._greenlets import MainGreenlet as _PwMainGreenlet
from playwright._impl._object_factory import create_remote_object as _pw_create_remote
from playwright._impl._playwright import Playwright as _PwImpl
from playwright._impl._transport import PipeTransport as _PwPipeTransport
from playwright.sync_api._generated import Playwright as _SyncPlaywright

from camoufox_adapter import CamoufoxBrowser, CamoufoxPage
from batch_traffic import meter_proxy_url
from retry_policy import browser_start_attempts
from secure_files import ensure_private_dir
from webui.blacklist_store import read_blacklist
from webui.security_utils import redact_log_line, redact_proxy

SUPPORTED_BROWSER_OS = ("windows", "macos", "linux")
DEFAULT_BROWSER_OS = "windows"
_BROWSER_OS_ALIASES = {
    "win": "windows",
    "win32": "windows",
    "windows": "windows",
    "mac": "macos",
    "darwin": "macos",
    "macos": "macos",
    "lin": "linux",
    "linux": "linux",
}
_FP_PROBE_JS = """() => {
  const out = {
    ua: String(navigator.userAgent || ''),
    platform: String(navigator.platform || ''),
    oscpu: String(navigator.oscpu || ''),
    maxTouch: Number(navigator.maxTouchPoints || 0),
    webgl: false,
    vendor: '',
    renderer: '',
    fontsWin: false,
    tz: '',
    langs: '',
    hw: 0,
    screenW: 0,
    screenH: 0,
    availH: 0,
  };
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (gl) {
      out.webgl = true;
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) {
        out.vendor = String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '');
        out.renderer = String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '');
      }
    }
  } catch (e) {}
  try {
    out.fontsWin = !!(document.fonts && (
      document.fonts.check('12px "Segoe UI"') ||
      document.fonts.check('12px "Segoe UI Historic"')
    ));
  } catch (e) {}
  try {
    out.tz = String((Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || '');
  } catch (e) {}
  try {
    out.langs = Array.from(navigator.languages || []).join(',');
  } catch (e) {}
  try {
    out.hw = Number(navigator.hardwareConcurrency || 0);
    out.screenW = Number((screen && screen.width) || 0);
    out.screenH = Number((screen && screen.height) || 0);
    out.availH = Number((screen && screen.availHeight) || 0);
  } catch (e) {}
  return out;
}"""


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def fingerprint_enhance_enabled() -> bool:
    """GROK_FP_ENHANCE=0 关闭本次指纹启动项，回到 locale=en-US / 不约束 screen / 不指定 webgl。"""
    return _env_flag("GROK_FP_ENHANCE", True)


_WEBGL_PAIRS_CACHE: dict = {}
_SKIP_RENDERER_SUBSTR = ("gtx 980", "swiftshader", "basic render driver")


def _pick_webgl_config(browser_os: str) -> Optional[Tuple[str, str]]:
    """从 Camoufox WebGL 池均匀抽样，避开日志扎堆的 GTX 980 和软件渲染器。"""
    os_key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(browser_os, "win")
    try:
        pairs = _WEBGL_PAIRS_CACHE.get(os_key)
        if pairs is None:
            from camoufox.webgl.sample import get_possible_pairs

            pairs = list(get_possible_pairs().get(os_key) or [])
            _WEBGL_PAIRS_CACHE[os_key] = pairs
    except Exception:
        return None
    if not pairs:
        return None
    import random

    preferred = [
        (str(vendor), str(renderer))
        for vendor, renderer in pairs
        if vendor
        and renderer
        and not any(skip in str(renderer).lower() for skip in _SKIP_RENDERER_SUBSTR)
    ]
    pool = preferred or [(str(v), str(r)) for v, r in pairs if v and r]
    if not pool:
        return None
    return random.choice(pool)


def _xvfb_screen_constraint():
    """指纹屏幕不超过当前 Xvfb 1920x1080，避免 screen 大于真实窗口。"""
    try:
        from browserforge.fingerprints import Screen
    except Exception:
        return None
    return Screen(min_width=1280, max_width=1920, min_height=720, max_height=1080)


def parse_screen_spec(raw: object) -> Optional[Tuple[int, int]]:
    """Parse '1920x1080' / '1920X1080'. Invalid values return None."""
    text = str(raw or "").strip().lower().replace(" ", "")
    if "x" not in text:
        return None
    left, right = text.split("x", 1)
    try:
        width, height = int(left), int(right)
    except (TypeError, ValueError):
        return None
    if width < 800 or height < 600 or width > 7680 or height > 4320:
        return None
    return width, height


def fp_timezone_override() -> str:
    """GROK_FP_TIMEZONE=America/Sao_Paulo pins IANA tz; empty keeps geoip tz."""
    return str(os.environ.get("GROK_FP_TIMEZONE") or "").strip()


def fp_screen_choices() -> list:
    """GROK_FP_SCREENS=1920x1080,2560x1440 (or GROK_FP_SCREEN= one size)."""
    raw = str(os.environ.get("GROK_FP_SCREENS") or os.environ.get("GROK_FP_SCREEN") or "").strip()
    if not raw:
        return []
    out = []
    seen = set()
    for part in raw.split(","):
        spec = parse_screen_spec(part)
        if spec and spec not in seen:
            seen.add(spec)
            out.append(spec)
    return out


_fp_screen_seq = 0
_fp_screen_seq_lock = threading.Lock()


_PATCHRIGHT_SCREENS = ((1366, 768), (1536, 864), (1440, 900), (1920, 1080))
_PATCHRIGHT_HARDWARE = ((8, 8), (4, 8), (8, 16), (12, 8))


def pick_fp_screen() -> Optional[Tuple[int, int]]:
    """Round-robin 1080p / 2K so a short test actually covers both sizes."""
    global _fp_screen_seq
    choices = fp_screen_choices()
    if not choices:
        return None
    with _fp_screen_seq_lock:
        idx = _fp_screen_seq
        _fp_screen_seq += 1
    return choices[idx % len(choices)]


def pick_intel_direct3d() -> Tuple[str, str]:
    """Windows Intel Direct3D pair, matching the Patchright signups that passed send-code."""
    fallback = (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0)",
    )
    try:
        pairs = _WEBGL_PAIRS_CACHE.get("win")
        if pairs is None:
            from camoufox.webgl.sample import get_possible_pairs

            pairs = list(get_possible_pairs().get("win") or [])
            _WEBGL_PAIRS_CACHE["win"] = pairs
    except Exception:
        return fallback
    intel = [
        (str(vendor), str(renderer))
        for vendor, renderer in pairs
        if "intel" in str(vendor).lower()
        and "direct3d" in str(renderer).lower()
        and not any(skip in str(renderer).lower() for skip in _SKIP_RENDERER_SUBSTR)
    ]
    if not intel:
        return fallback
    import random

    return random.choice(intel)


def pick_patchright_screen() -> Tuple[int, int]:
    """Rotate a window size that fits the current Xvfb."""
    pinned = pick_fp_screen()
    if pinned and pinned[0] <= 1920 and pinned[1] <= 1080:
        return pinned
    global _fp_screen_seq
    with _fp_screen_seq_lock:
        idx = _fp_screen_seq
        _fp_screen_seq += 1
    return _PATCHRIGHT_SCREENS[idx % len(_PATCHRIGHT_SCREENS)]


def chromium_product_version(chrome_path: str = "") -> str:
    """Real Chromium version so the Windows UA does not disagree with the binary."""
    cached = getattr(_tls, "chrome_version", "")
    if cached:
        return str(cached)
    path = str(chrome_path or os.environ.get("PATCHRIGHT_CHROME") or "").strip()
    if not path:
        for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
            if os.path.isfile(candidate):
                path = candidate
                break
    version = ""
    if path:
        try:
            import subprocess

            out = subprocess.check_output(
                [path, "--product-version"],
                text=True,
                timeout=8,
                stderr=subprocess.DEVNULL,
            )
            version = str(out or "").strip().split()[-1]
        except Exception:
            version = ""
    _tls.chrome_version = version
    return version


def pick_patchright_hardware() -> Tuple[int, int]:
    """Cores and deviceMemory paired with the screen that was just chosen."""
    index = max(_fp_screen_seq - 1, 0)
    return _PATCHRIGHT_HARDWARE[index % len(_PATCHRIGHT_HARDWARE)]


def chrome_client_hints(version: str) -> dict:
    """Same Windows client hints the headed Chromium path sends through CDP."""
    full = str(version or "").strip() or "145.0.0.0"
    major = full.split(".", 1)[0]
    return {
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            {"brand": "Not-A.Brand", "version": "24"},
        ],
        "fullVersionList": [
            {"brand": "Chromium", "version": full},
            {"brand": "Google Chrome", "version": full},
            {"brand": "Not-A.Brand", "version": "24.0.0.0"},
        ],
        "fullVersion": full,
        "platform": "Windows",
        "platformVersion": "15.0.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


def windows_chrome_user_agent(version: str) -> str:
    ver = str(version or "").strip() or "145.0.0.0"
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{ver} Safari/537.36"
    )


def patchright_fingerprint(proxy: str = "", chrome_path: str = "") -> dict:
    """Proxy-region timezone plus a fresh Windows screen and WebGL pair.

    Locale stays en-US. GeoIP language would translate the signup buttons.
    """
    screen = pick_patchright_screen()
    cores, memory = pick_patchright_hardware()
    webgl = _pick_webgl_config("windows") or ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA)")
    version = chromium_product_version(chrome_path)
    fp = {
        "locale": "en-US",
        "languages": ["en-US", "en"],
        "timezone_id": "",
        "geolocation": None,
        "screen": screen,
        "hardware_concurrency": cores,
        "device_memory": memory,
        "webgl_vendor": webgl[0],
        "webgl_renderer": webgl[1],
        "user_agent": windows_chrome_user_agent(version),
        "platform": "Win32",
        "ua_metadata": chrome_client_hints(version),
        "exit_ip": "",
    }
    raw_proxy = str(proxy or "").strip()
    if not raw_proxy:
        return fp
    try:
        exit_ip = _resolve_proxy_exit_ip(raw_proxy, timeout=8.0)
    except Exception:
        return fp
    fp["exit_ip"] = exit_ip
    try:
        from camoufox.geolocation import get_geolocation

        geo = get_geolocation(exit_ip)
        fp["timezone_id"] = str(getattr(geo, "timezone", "") or "")
        fp["geolocation"] = {
            "latitude": float(geo.latitude),
            "longitude": float(geo.longitude),
        }
    except Exception as exc:
        fp["geo_error"] = type(exc).__name__
    return fp


# Orca manual signup HAR, 2026-10-26: Linux Chrome 150.0.7871.250.
HAR_CHROME_FULL = "150.0.7871.250"
HAR_CHROME_MAJOR = "150"
HAR_GREASE_BRAND = "Not;A=Brand"
HAR_GREASE_VERSION = "8"


def castle_har_user_agent(full: str = "") -> str:
    """Linux Chrome UA from the manual HAR. The full build is in the UA string."""
    version = HAR_CHROME_FULL
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        f"Chrome/{version} Safari/537.36"
    )


def castle_har_client_hints(full: str = "", hints: dict | None = None) -> dict:
    """sec-ch-ua from the manual HAR: Not;A=Brand 8 and Chromium 150."""
    base = dict(hints or {})
    version = HAR_CHROME_FULL
    base.update(
        {
            "brands": [
                {"brand": HAR_GREASE_BRAND, "version": HAR_GREASE_VERSION},
                {"brand": "Chromium", "version": HAR_CHROME_MAJOR},
            ],
            "fullVersionList": [
                {"brand": HAR_GREASE_BRAND, "version": f"{HAR_GREASE_VERSION}.0.0.0"},
                {"brand": "Chromium", "version": version},
            ],
            "fullVersion": version,
            "platform": "Linux",
            "platformVersion": "",
            "architecture": "x86",
            "bitness": "64",
            "model": "",
            "mobile": False,
            "wow64": False,
        }
    )
    return base


def castle_sec_ch_headers(hints: dict | None = None) -> dict[str, str]:
    """Client hints for the account fingerprint. Defaults match the Patchright Windows profile."""
    meta = dict(hints or {})
    brands = meta.get("brands") or [
        {"brand": "Chromium", "version": "145"},
        {"brand": "Google Chrome", "version": "145"},
        {"brand": "Not-A.Brand", "version": "24"},
    ]
    platform = str(meta.get("platform") or "Windows")
    mobile = "?1" if meta.get("mobile") else "?0"
    sec_ch_ua = ", ".join(
        f'"{item.get("brand")}";v="{item.get("version")}"' for item in brands if item.get("brand")
    )
    return {
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": mobile,
        "sec-ch-ua-platform": f'"{platform}"',
    }


def castle_node_profile(fp: dict) -> dict:
    """Fields the Node Castle mint may read. Proxy credentials stay out.

    Identity follows the Patchright Windows profile: Chrome 145, Intel Direct3D
    when that is the sampled WebGL pair, vendor and renderer kept distinct.
    """
    screen = fp.get("screen") or (1920, 1080)
    hints = dict(fp.get("ua_metadata") or {})
    if not hints.get("fullVersion"):
        hints = chrome_client_hints("145.0.7632.159")
    vendor = str(fp.get("webgl_vendor") or "")
    renderer = str(fp.get("webgl_renderer") or "")
    if not vendor or not renderer:
        picked = _pick_webgl_config("windows") or (
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0)",
        )
        vendor, renderer = picked
    return {
        "user_agent": str(fp.get("user_agent") or "")
        or windows_chrome_user_agent(str(hints.get("fullVersion") or "145.0.7632.159")),
        "platform": str(fp.get("platform") or "Win32"),
        "locale": str(fp.get("locale") or "en-US"),
        "languages": list(fp.get("languages") or ["en-US", "en"]),
        "hardware_concurrency": int(fp.get("hardware_concurrency") or 8),
        "device_memory": int(fp.get("device_memory") or 8),
        "screen": [int(screen[0]), int(screen[1])],
        "timezone_id": str(fp.get("timezone_id") or ""),
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "ua_metadata": hints,
    }


def set_proxy_rotate_hook(fn: Optional[Callable] = None) -> None:
    """Optional hook start_browser calls after an allowlist/blacklist miss."""
    _tls.proxy_rotate = fn


def try_rotate_proxy(reason: str = "", log_callback=None) -> bool:
    """Call the worker's proxy-rotate hook if one is registered."""
    return _invoke_proxy_rotate(reason or "rotate", log_callback)


def _invoke_proxy_rotate(reason: str, log_callback=None) -> bool:
    hook = getattr(_tls, "proxy_rotate", None)
    if not callable(hook):
        return False
    try:
        hook(reason)
        return True
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 换 sticky 失败: {redact_log_line(str(exc))}")
        return False


def _screen_from_wh(width: int, height: int):
    try:
        from browserforge.fingerprints import Screen
    except Exception:
        return None
    return Screen(min_width=width, max_width=width, min_height=height, max_height=height)


def exit_ip_prefixes() -> list:
    """GROK_EXIT_IP_PREFIXES=104.207,209.50 or 104.207.0.0/16. Empty = no filter."""
    raw = str(os.environ.get("GROK_EXIT_IP_PREFIXES") or "").strip()
    if not raw:
        return []
    out = []
    seen = set()
    for part in raw.split(","):
        token = str(part or "").strip()
        if not token:
            continue
        if "/" in token:
            token = token.split("/", 1)[0].strip()
        bits = [b for b in token.split(".") if b != ""]
        if not bits:
            continue
        # Drop trailing .0 octets from CIDR network addresses: 104.207.0.0 -> 104.207
        while len(bits) > 1 and bits[-1] == "0":
            bits.pop()
        prefix = ".".join(bits) + "."
        if prefix not in seen:
            seen.add(prefix)
            out.append(prefix)
    return out


def exit_ip_allowed(ip: str) -> bool:
    """True if no allowlist, or IPv4 starts with an allowed prefix."""
    prefixes = exit_ip_prefixes()
    if not prefixes:
        return True
    value = str(ip or "").strip()
    if not value:
        return False
    try:
        packed = ipaddress.ip_address(value)
    except Exception:
        return False
    if packed.version != 4:
        return False
    dotted = str(packed) + "."
    return any(dotted.startswith(prefix) for prefix in prefixes)


def resolve_browser_os(raw: Optional[str] = None) -> str:
    """Camoufox 指纹目标 OS。默认 windows，避免 Linux Xvfb 把 UA/字体/WebGL 露成 linux。

    覆盖：GROK_BROWSER_OS=windows|macos|linux
    """
    if raw is None:
        raw = os.environ.get("GROK_BROWSER_OS", DEFAULT_BROWSER_OS)
    value = str(raw or DEFAULT_BROWSER_OS).strip().lower()
    return _BROWSER_OS_ALIASES.get(value, DEFAULT_BROWSER_OS)


def format_fingerprint_log(os_name: str, probe: Optional[dict] = None) -> str:
    """把指纹探测压成一行日志。"""
    probe = probe or {}
    ua = str(probe.get("ua") or "")
    ua_short = ua[:72] + ("…" if len(ua) > 72 else "")
    plat = str(probe.get("platform") or "?")
    renderer = str(probe.get("renderer") or ("ok" if probe.get("webgl") else "none"))[:80]
    leak = []
    blob = " ".join([ua, plat, str(probe.get("oscpu") or "")]).lower()
    if "linux" in blob:
        leak.append("linux-ua")
    if not probe.get("webgl"):
        leak.append("no-webgl")
    if probe.get("fontsWin") is False:
        leak.append("no-segoe")
    try:
        sw = int(probe.get("screenW") or 0)
        sh = int(probe.get("screenH") or 0)
    except (TypeError, ValueError):
        sw, sh = 0, 0
    if sw > 1920 or sh > 1080:
        leak.append("screen>xvfb")
    leak_s = f" leak={','.join(leak)}" if leak else " leak=none"
    extra = ""
    tz = str(probe.get("tz") or "").strip()
    langs = str(probe.get("langs") or "").strip()
    hw = probe.get("hw")
    if tz or langs or hw or sw:
        extra = (
            f" tz={tz or '?'} langs={langs or '?'} hw={hw or '?'} "
            f"screen={sw or '?'}x{sh or '?'}"
        )
    return (
        f"[*] 指纹 os={os_name} platform={plat} "
        f"webgl={renderer} fontsWin={probe.get('fontsWin')} "
        f"ua={ua_short}{extra}{leak_s}"
    )


def probe_browser_fingerprint(raw_page) -> dict:
    """在已启动的 Playwright page 上读 UA / platform / WebGL / Segoe。"""
    try:
        data = raw_page.evaluate(_FP_PROBE_JS)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class SafeCamoufox(_Camoufox):
    """Camoufox 子类，绕过 PlaywrightContextManager 的事件循环检查。

    PlaywrightContextManager.__enter__() 调用 asyncio.get_running_loop()，
    如果当前线程有运行中的 asyncio 事件循环（如 tkinter 或其他库遗留），
    就会报错 "Sync API inside the asyncio loop"。

    此子类直接创建全新的、非运行状态的事件循环，完全绕过该检查。
    通过重写 __enter__()，跳过 get_running_loop() / is_running() 判断，
    直接用 asyncio.new_event_loop() 创建干净的事件循环。
    """

    def __enter__(self):
        # 强制创建全新的事件循环，跳过 get_running_loop() / is_running() 检查
        self._loop = asyncio.new_event_loop()
        self._own_loop = True

        # 复制 PlaywrightContextManager.__enter__() 的 greenlet 调度逻辑
        def _greenlet_main():
            self._loop.run_until_complete(self._connection.run_as_sync())

        dispatcher_fiber = _PwMainGreenlet(_greenlet_main)

        self._connection = _PwConnection(
            dispatcher_fiber,
            _pw_create_remote,
            _PwPipeTransport(self._loop),
            self._loop,
        )

        g_self = greenlet.getcurrent()

        def _callback_wrapper(channel_owner):
            playwright_impl = _tcast(_PwImpl, channel_owner)
            self._playwright = _SyncPlaywright(playwright_impl)
            g_self.switch()

        self._connection.call_on_object_with_known_name("Playwright", _callback_wrapper)
        dispatcher_fiber.switch()

        playwright = self._playwright
        playwright.stop = self.__exit__

        # Camoufox 特有：启动浏览器
        try:
            self.browser = NewBrowser(self._playwright, **self.launch_options)
        except BaseException as e:
            try:
                super().__exit__(type(e), e, e.__traceback__)
            except BaseException:
                pass
            raise
        return self.browser

    def __exit__(self, exc_type, exc, tb):
        """关闭时吞掉 EPIPE / TargetClosed，避免 Node 24 未处理 error 拖死进程。"""
        try:
            return super().__exit__(exc_type, exc, tb)
        except BaseException:
            return True


# 仅允许删除该目录树下的临时 profile，防止误删其它路径
_PROFILE_ROOT_MARKER = "grok-register-camoufox"

_tls = threading.local()
_get_proxy: Optional[Callable[[], dict]] = None
_is_debug: Optional[Callable[[], bool]] = None
_extension_path: str = ""
_start_fail_lock = threading.Lock()
_start_fail_streak = 0
_start_fail_threshold = 3
_har_prune_lock = threading.Lock()
_har_pruned_dirs: set[str] = set()
_EMPTY_HAR_MAX_BYTES = 512
_SESSION_HAR_NAME = re.compile(r"^\d+-\d+-[0-9a-f]{8}\.har$", re.I)


def configure(get_proxies=None, is_debug=None, extension_path=""):
    global _get_proxy, _is_debug, _extension_path
    _get_proxy = get_proxies
    _is_debug = is_debug
    _extension_path = extension_path or ""


def is_browser_pipe_error(exc: object) -> bool:
    """Playwright Node 管道断开、对端复位、或浏览器已关。"""
    msg = str(exc or "")
    low = msg.lower()
    return (
        "epipe" in low
        or "econnreset" in low
        or "connection reset" in low
        or "connection aborted" in low
        or "target closed" in low
        or "targetclosed" in low
        or "browser has been closed" in low
        or "connection closed" in low
        or "pipe closed" in low
        or "playwright connection" in low
    )


def _is_driver_pipe_error(exc: object) -> bool:
    return is_browser_pipe_error(exc)


def get_start_fail_streak() -> int:
    with _start_fail_lock:
        return _start_fail_streak


def _note_start_success():
    global _start_fail_streak
    with _start_fail_lock:
        _start_fail_streak = 0


def _note_start_failure():
    global _start_fail_streak
    with _start_fail_lock:
        _start_fail_streak += 1
        return _start_fail_streak


def _proxies() -> dict:
    if _get_proxy:
        return _get_proxy() or {}
    return {}


def _debug() -> bool:
    return bool(_is_debug()) if _is_debug else False


def active_browser():
    return getattr(_tls, "browser", None)


def active_page():
    return getattr(_tls, "page", None)


def get_exit_ip() -> str:
    """当前线程浏览器启动时解析到的代理出口 IP。"""
    return str(getattr(_tls, "exit_ip", "") or "")


def get_bound_proxy() -> str:
    """当前线程绑定的代理 URL。"""
    return str(getattr(_tls, "bound_proxy", "") or "")


def set_exit_context(proxy: str = "", exit_ip: str = "") -> None:
    _tls.bound_proxy = proxy or ""
    _tls.exit_ip = exit_ip or ""
    if proxy and exit_ip:
        try:
            from webui.proxy_store import note_proxy_exit

            note_proxy_exit(proxy, exit_ip)
        except Exception:
            pass


def clear_exit_context() -> None:
    for k in ("bound_proxy", "exit_ip"):
        if hasattr(_tls, k):
            try:
                delattr(_tls, k)
            except Exception:
                setattr(_tls, k, "")




# Baseline values remain as compatibility constants. Runtime additions live in
# log/blacklist_state.json and are never written into Python source.
_BLOCKED_ASN_SUBSTR = (
    "AS7922",  # Comcast Cable
    "AS5650",  # Frontier Communications
)
_BLOCKED_ISP_SUBSTR = (
    "comcast cable",
    "comcast ip services",
    "frontier communications",
)
_BLOCKED_ASN_NUMS = {5650, 7922}
_asn_cache = {}
_asn_cache_lock = __import__("threading").Lock()


def lookup_exit_meta(ip: str) -> dict:
    """查出口 ISP/AS。强制直连（忽略环境 HTTP_PROXY），优先 ipwho.is。"""
    import json
    import urllib.request
    ip = (ip or "").strip()
    if not ip:
        return {}
    with _asn_cache_lock:
        if ip in _asn_cache:
            return dict(_asn_cache[ip])
    info = {"query": ip}
    # 服务器环境常设 HTTP_PROXY，geo API 走代理会 502/超时；必须直连
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    errors = []
    # 1) ipwho.is over TLS
    try:
        raw = opener.open(f"https://ipwho.is/{ip}", timeout=10).read().decode("utf-8", errors="replace")
        data = json.loads(raw or "{}")
        if data.get("success") is False:
            raise RuntimeError(data.get("message") or "ipwho fail")
        conn = data.get("connection") or {}
        asn_num = conn.get("asn")
        info = {
            "query": ip,
            "status": "success",
            "as": f"AS{asn_num}" if asn_num is not None else "",
            "asn": asn_num,
            "isp": conn.get("isp") or data.get("isp") or "",
            "org": conn.get("org") or "",
            "city": data.get("city") or "",
            "regionName": data.get("region") or "",
            "country": data.get("country") or "",
            "mobile": bool((data.get("type") or "").lower() == "mobile") if False else None,
            "source": "ipwho.is",
        }
    except Exception as exc:
        errors.append(f"ipwho:{exc}")
        # 2) fallback ipapi.co over TLS
        try:
            url = f"https://ipapi.co/{ip}/json/"
            raw = opener.open(url, timeout=10).read().decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
            if data.get("error"):
                raise RuntimeError(data.get("reason") or "ipapi.co fail")
            as_text = str(data.get("asn") or "")
            info = {
                "query": ip,
                "status": "success",
                "as": as_text,
                "isp": data.get("org") or "",
                "org": data.get("org") or "",
                "city": data.get("city") or "",
                "regionName": data.get("region") or "",
                "country": data.get("country_name") or "",
                "source": "ipapi.co",
            }
            m = __import__("re").search(r"AS(\d+)", as_text)
            if m:
                info["asn"] = int(m.group(1))
        except Exception as exc2:
            errors.append(f"ipapi:{exc2}")
            info = {"query": ip, "error": "; ".join(errors)}
    with _asn_cache_lock:
        _asn_cache[ip] = dict(info)
    return info


_guard_ip_block = {}
_guard_ip_block_lock = __import__("threading").Lock()


def block_exit_ip(ip: str, seconds: object = 1800, reason: str = "") -> float:
    """Temporarily skip this residential exit IP after Guard risk."""
    value = str(ip or "").strip()
    if not value:
        return 0.0
    try:
        ttl = max(60, int(seconds or 0) or 1800)
    except (TypeError, ValueError):
        ttl = 1800
    until = __import__("time").time() + ttl
    with _guard_ip_block_lock:
        prev = float(_guard_ip_block.get(value) or 0)
        _guard_ip_block[value] = max(prev, until)
    return until


def is_guard_blocked_exit_ip(ip: str) -> tuple:
    value = str(ip or "").strip()
    if not value:
        return False, 0.0
    now = __import__("time").time()
    with _guard_ip_block_lock:
        until = float(_guard_ip_block.get(value) or 0)
        if until <= now:
            _guard_ip_block.pop(value, None)
            return False, 0.0
        return True, until


def is_blocked_exit_ip(ip: str) -> tuple:
    """若应跳过该出口，返回 (True, reason)；否则 (False, meta摘要)."""
    blocked_ip, until = is_guard_blocked_exit_ip(ip)
    if blocked_ip:
        remain = max(0, int(until - __import__("time").time()))
        return True, f"guard-block {remain}s"
    info = lookup_exit_meta(ip)
    asn = str(info.get("as") or "")
    asn_num = info.get("asn")
    try:
        if asn_num is None:
            m = __import__("re").search(r"AS(\d+)", asn)
            asn_num = int(m.group(1)) if m else None
        else:
            asn_num = int(asn_num)
    except Exception:
        asn_num = None
    isp = str(info.get("isp") or "")
    org = str(info.get("org") or "")
    blob = f"{asn} | {isp} | {org}".lower()
    state = read_blacklist()
    blocked_asns = set(state.get("asns") or _BLOCKED_ASN_NUMS)
    blocked_asn_labels = tuple(f"AS{value}" for value in blocked_asns)
    blocked_isp_keywords = tuple(state.get("isp_keywords") or _BLOCKED_ISP_SUBSTR)
    # 家宽只换 IP，不因 ASN/ISP 整段跳过；Guard 风控后按 TTL 跳过该 IP
    summary = f"{asn or '?'} | {isp or '?'} | {info.get('city') or '?'}"
    return False, summary



def set_browser_session(browser_obj=None, page_obj=None):
    _tls.browser = browser_obj
    _tls.page = page_obj


class _SessionProxy:
    __slots__ = ("_key",)

    def __init__(self, key):
        self._key = key

    def _obj(self):
        return getattr(_tls, self._key, None)

    def __bool__(self):
        return self._obj() is not None

    def __eq__(self, other):
        return self._obj() is other

    def __ne__(self, other):
        return self._obj() is not other

    def __getattr__(self, name):
        obj = self._obj()
        if obj is None:
            raise AttributeError(f"{self._key} is not started")
        return getattr(obj, name)


browser = _SessionProxy("browser")
page = _SessionProxy("page")


def _is_managed_profile_dir(path: str) -> bool:
    """是否为本工具创建的临时 Camoufox 资料目录。"""
    if not path:
        return False
    norm = os.path.normpath(path).replace("\\", "/").lower()
    marker = _PROFILE_ROOT_MARKER.lower()
    if f"/{marker}/" in f"/{norm}/" or norm.rstrip("/").endswith(f"/{marker}"):
        return True
    return "/.browser-profiles/" in f"/{norm}/" or norm.rstrip("/").endswith(
        "/.browser-profiles"
    )


def _profile_root(
    *,
    platform_name: str | None = None,
    environ=None,
    temp_root: str | None = None,
) -> Path:
    platform = os.name if platform_name is None else str(platform_name)
    source = os.environ if environ is None else environ
    if platform == "nt":
        local_app_data = str(source.get("LOCALAPPDATA", "") or "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return ensure_private_dir(base / "GrokRegister" / _PROFILE_ROOT_MARKER)
    base = temp_root if temp_root is not None else tempfile.gettempdir()
    return ensure_private_dir(Path(base) / _PROFILE_ROOT_MARKER)


def _rmtree_with_retry(path: str, max_retries: int = 3, delay: float = 0.5) -> bool:
    """Windows 上文件锁可能导致 rmtree 失败，带重试。

    返回 True 表示最终删除成功。
    """
    for attempt in range(max_retries):
        try:
            shutil.rmtree(path, ignore_errors=False)
            return True
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            # 最后一次尝试：强制忽略错误
            try:
                shutil.rmtree(path, ignore_errors=True)
                return not os.path.isdir(path)
            except Exception:
                return False
    return not os.path.isdir(path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def _cleanup_profile_dir(profile_dir=None) -> None:
    """关闭浏览器后删除临时 user-data，避免 TEMP 堆积。"""
    path = profile_dir if profile_dir is not None else getattr(_tls, "profile_dir", None)
    try:
        if getattr(_tls, "profile_dir", None) and (
            profile_dir is None
            or os.path.normpath(str(getattr(_tls, "profile_dir")))
            == os.path.normpath(str(path or ""))
        ):
            _tls.profile_dir = None
    except Exception:
        pass
    if not path or not _is_managed_profile_dir(str(path)):
        return
    if os.path.isdir(path):
        _rmtree_with_retry(path)


def cleanup_stale_profiles(log_callback=None) -> int:
    """启动时清理上次崩溃 / 强杀残留的临时 profile 目录。

    扫描 TEMP/grok-register-camoufox/ 下的子目录，
    删除所有未被当前进程占用的旧目录。
    返回清理的目录数量。
    """
    roots = [Path(tempfile.gettempdir()) / _PROFILE_ROOT_MARKER]
    if os.name == "nt":
        roots.insert(0, _profile_root())
        roots.append(Path(__file__).resolve().parent / ".browser-profiles")

    current_pid = os.getpid()
    cleaned = 0
    for root in roots:
        if not root.is_dir():
            continue
        ensure_private_dir(root)
        try:
            for entry in os.listdir(root):
                entry_path = root / entry
                if not entry_path.is_dir():
                    continue
                # Unknown names and profiles owned by live processes are kept.
                match = __import__("re").fullmatch(
                    r"(\d+)-\d+-[0-9a-fA-F]{8}", entry
                )
                if not match:
                    continue
                owner_pid = int(match.group(1))
                if owner_pid == current_pid or _pid_alive(owner_pid):
                    continue
                if _rmtree_with_retry(str(entry_path)):
                    cleaned += 1
        except Exception:
            pass

    if cleaned > 0 and log_callback:
        log_callback(f"[*] 启动清理: 已删除 {cleaned} 个残留浏览器资料目录")
    return cleaned



def _normalize_ip_candidate(value: object) -> str:
    text = str(value or "").strip().split()[0] if str(value or "").strip() else ""
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def _resolve_proxy_exit_ip(proxy_str: str, timeout: float = 8.0, log_callback=None) -> str:
    """经代理探测出口公网 IP（比 Camoufox 内置 public_ip 更耐住宅延迟）。

    Camoufox geoip=True 时会自己请求 ipecho/ipify，timeout 仅 5s，
    住宅 sticky 稍慢就 Failed to get IP → 浏览器启动失败。
    这里加长超时、多源探测，成功后把 IP 字符串传给 geoip=，跳过库内探测。
    """
    import warnings

    proxy_str = (proxy_str or "").strip()
    if not proxy_str:
        raise RuntimeError("代理为空，无法探测出口 IP")
    urls = (
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://icanhazip.com",
    )
    last_exc = None
    budget = max(2.0, min(float(timeout), 20.0))
    deadline = time.monotonic() + budget
    clients = []

    def add_requests_client():
        try:
            import requests
            from urllib3.exceptions import InsecureRequestWarning

            def request_get(url, request_timeout):
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=InsecureRequestWarning)
                    return requests.get(
                        url,
                        proxies={"http": proxy_str, "https": proxy_str},
                        timeout=request_timeout,
                        verify=False,
                    )

            clients.append(("requests", request_get))
        except ImportError:
            pass

    def add_curl_client():
        try:
            from curl_cffi import requests as curl_requests

            def curl_get(url, request_timeout):
                return curl_requests.get(
                    url,
                    proxy=proxy_str,
                    timeout=request_timeout,
                    impersonate="chrome",
                    verify=False,
                )

            clients.append(("curl_cffi", curl_get))
        except ImportError:
            pass

    if os.name == "nt":
        add_curl_client()
        add_requests_client()
    else:
        add_requests_client()
        add_curl_client()

    for client_name, request_get in clients:
        hard_fails = 0
        for url in urls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            per_try = max(1.0, min(4.0, remaining))
            try:
                resp = request_get(url, per_try)
                resp.raise_for_status()
                ip = _normalize_ip_candidate(getattr(resp, "text", ""))
                if ip:
                    if log_callback:
                        log_callback(
                            f"[*] 代理出口 IP: {ip} ({client_name}/{url.split('/')[2]})"
                        )
                    return ip
                last_exc = RuntimeError("出口探测返回了非 IP 内容")
            except Exception as exc:
                last_exc = exc
                hard_fails += 1
                if log_callback:
                    log_callback(
                        f"[Debug] 出口 IP 探测失败 {url.split('/')[2]}: "
                        f"{redact_log_line(str(exc))}"
                    )
                if hard_fails >= 2:
                    break
        if time.monotonic() >= deadline:
            break
    raise RuntimeError(
        f"代理出口 IP 探测失败(total_timeout={budget:.1f}s): "
        f"{redact_log_line(str(last_exc))}"
    )


def _build_camoufox_proxy(proxy_str: str) -> dict:
    """把 http://host:port 格式的代理 URL 转换为 Camoufox/Playwright proxy dict。"""
    proxy_str = proxy_str.strip()
    if not proxy_str:
        return {}
    parsed = urlparse(proxy_str)
    if parsed.scheme and parsed.hostname:
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
    else:
        server = proxy_str
    result: dict = {"server": server}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _detect_camoufox_exe() -> str:
    """检测 Camoufox 可执行文件路径，支持新旧两种安装格式。

    新格式（multiversion）：browsers/{repo}/{version}/camoufox.exe + .0.5_FLAG
    旧格式（legacy）：直接在 INSTALL_DIR 下，无 .0.5_FLAG

    旧格式下 installed_verstr() 会报错 "official/stable is not installed"，
    需要传 executable_path + ff_version 绕过该检查。
    """
    import json as _json
    from camoufox.pkgman import INSTALL_DIR, LAUNCH_FILE, OS_NAME

    exe_name = LAUNCH_FILE.get(OS_NAME, "camoufox.exe")
    compat_flag = INSTALL_DIR / ".0.5_FLAG"

    # 新格式：COMPAT_FLAG 存在，让 launch_options() 自行处理
    if compat_flag.exists():
        return ""

    # 旧格式：直接在 INSTALL_DIR 下查找
    legacy_exe = INSTALL_DIR / exe_name
    if legacy_exe.exists():
        return str(legacy_exe)

    return ""


def _detect_ff_version() -> str:
    """从 version.json 读取 Firefox 主版本号（如 "152"）。

    旧格式安装的 version.json 格式：{"version": "152.0.4", "release": "beta.28"}
    新格式还包含 "build" 字段。
    """
    import json as _json
    from camoufox.pkgman import INSTALL_DIR

    version_file = INSTALL_DIR / "version.json"
    if not version_file.exists():
        # 检查 browsers/ 子目录
        browsers_dir = INSTALL_DIR / "browsers"
        if browsers_dir.exists():
            for vf in browsers_dir.rglob("version.json"):
                version_file = vf
                break
    if not version_file.exists():
        return ""

    try:
        data = _json.loads(version_file.read_bytes())
        version_str = str(data.get("version", ""))
        major = version_str.split(".")[0]
        return major if major.isdigit() else ""
    except Exception:
        return ""


def _broken_default_addons():
    """Return Camoufox default addons whose extract dir has no manifest.json."""
    try:
        from camoufox.addons import DefaultAddons, get_addon_path
    except Exception:
        return []
    broken = []
    for addon in DefaultAddons:
        try:
            path = get_addon_path(addon.name)
        except Exception:
            continue
        if os.path.isdir(path) and not os.path.isfile(os.path.join(path, "manifest.json")):
            broken.append(addon)
    return broken



def har_recording_enabled() -> bool:
    """HAR is recorded only in debug mode, or when GROK_RECORD_HAR is set."""
    if str(os.environ.get("GROK_RECORD_HAR") or "").strip():
        return True
    env = str(os.environ.get("GROK_DEBUG_MODE") or "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return _debug()


def prune_record_har_dir(directory: str | os.PathLike[str] | None) -> dict:
    """Drop empty Playwright shells and leftover session HARs; keep captures."""
    dest = Path(str(directory or "")).expanduser()
    removed = 0
    kept = 0
    bytes_freed = 0
    if not str(dest) or not dest.is_dir():
        return {"removed": removed, "kept": kept, "bytes": bytes_freed}
    for path in dest.glob("*.har"):
        name = path.name
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        keep = name.startswith("success-") or name.startswith("signup-protocol")
        if not keep and size > _EMPTY_HAR_MAX_BYTES and not _SESSION_HAR_NAME.match(name):
            keep = True
        if keep:
            kept += 1
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
        bytes_freed += max(size, 0)
    return {"removed": removed, "kept": kept, "bytes": bytes_freed}


def prune_default_har_dirs(force: bool = False) -> dict:
    """Prune the default HAR dir once per process (plus GROK_RECORD_HAR_DIR)."""
    dirs = [str(Path(__file__).resolve().parent / "log" / "browser-hars")]
    extra = str(os.environ.get("GROK_RECORD_HAR_DIR") or "").strip()
    if extra:
        dirs.append(extra)
    summary = {"removed": 0, "kept": 0, "bytes": 0}
    for directory in dirs:
        key = str(Path(directory))
        with _har_prune_lock:
            if not force and key in _har_pruned_dirs:
                continue
            _har_pruned_dirs.add(key)
        stats = prune_record_har_dir(directory)
        summary["removed"] += int(stats.get("removed") or 0)
        summary["kept"] += int(stats.get("kept") or 0)
        summary["bytes"] += int(stats.get("bytes") or 0)
    return summary


def resolve_record_har_path() -> str:
    """Return the Playwright record_har_path for this browser start.

    Recording is off unless debug_mode / GROK_DEBUG_MODE is on, or the
    capture tool set GROK_RECORD_HAR. GROK_RECORD_HAR_DIR (preferred) or a
    directory GROK_RECORD_HAR yields a unique *.har per start so concurrent
    workers do not clobber each other. A *.har file path is used as-is.
    """
    explicit = str(os.environ.get("GROK_RECORD_HAR") or "").strip()
    directory = str(os.environ.get("GROK_RECORD_HAR_DIR") or "").strip()
    path = ""
    if not har_recording_enabled():
        _tls.record_har_path = ""
        _tls.record_har_success_email = ""
        _tls.record_har_saved = ""
        return ""
    explicit_file = (
        explicit.endswith(".har") or Path(explicit).suffix.lower() == ".har"
        if explicit
        else False
    )
    if explicit_file:
        candidate = Path(explicit)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        path = str(candidate)
    elif directory:
        dest_dir = Path(directory)
        dest_dir.mkdir(parents=True, exist_ok=True)
        prune_record_har_dir(dest_dir)
        path = str(
            dest_dir
            / f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}.har"
        )
    elif explicit:
        candidate = Path(explicit)
        candidate.mkdir(parents=True, exist_ok=True)
        prune_record_har_dir(candidate)
        path = str(
            candidate
            / f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}.har"
        )
    else:
        dest_dir = Path(__file__).resolve().parent / "log" / "browser-hars"
        dest_dir.mkdir(parents=True, exist_ok=True)
        prune_record_har_dir(dest_dir)
        path = str(
            dest_dir
            / f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}.har"
        )
    _tls.record_har_path = path
    _tls.record_har_success_email = ""
    _tls.record_har_saved = ""
    return path


def current_record_har_path() -> str:
    return str(getattr(_tls, "record_har_path", "") or "")


def mark_recorded_har_success(email: str) -> None:
    _tls.record_har_success_email = str(email or "").strip()


def last_saved_success_har() -> str:
    return str(getattr(_tls, "record_har_saved", "") or "")


def _persist_success_har(har_path: str, email: str) -> str:
    src = Path(har_path)
    if not src.is_file() or src.stat().st_size <= 0:
        return ""
    dest_dir = Path(
        str(os.environ.get("GROK_RECORD_HAR_DIR") or src.parent)
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._@+-" else "_" for ch in email)
    dest = dest_dir / f"success-{safe}.har"
    shutil.copy2(src, dest)
    return str(dest)


def create_browser_options(unique_profile=True) -> dict:
    """构建 Camoufox 启动参数 dict。

    返回可直接传给 Camoufox(**opts) 的参数字典。
    替代原 DrissionPage 的 ChromiumOptions。

    反检测策略：
    - headless=False：有头模式（headless 更易被检测）
    - humanize=True：人类化鼠标移动 + 点击轨迹（不改时长，不改点击路径）
    - geoip=True：基于代理 IP 匹配时区 / 语言 / 经纬度
    - block_webrtc=True：WebRTC IP 泄漏防护（避免真实 IP 通过 STUN 暴露）
    - os=windows：UA / Client Hints / 字体 / WebGL 渲染器按 Windows 配置文件对齐
      （宿主机即使是 Linux Xvfb 也不走 linux 指纹）
    - 指纹由 BrowserForge 自动生成（匹配 Firefox/Camoufox 引擎）
    - 不设置 disable_coop：会改 COOP，影响 Turnstile iframe 点击
    - GROK_FP_ENHANCE=0 回滚：locale=en-US / 不约束 screen / 不指定 webgl_config
    """
    # GROK_HEADLESS=1 forces headless (needed on some Windows sessions where
    # headed Camoufox dies with GPU process / SW-WR framebuffer crashes).
    # GROK_HEADED=1 forces headed even on Windows.
    headless_env = str(os.environ.get("GROK_HEADLESS", "") or "").strip().lower()
    headed_env = str(os.environ.get("GROK_HEADED", "") or "").strip().lower()
    force_headless = headless_env in {"1", "true", "yes", "on"}
    force_headed = headed_env in {"1", "true", "yes", "on"}
    use_headless = bool(force_headless) and not force_headed
    browser_os = resolve_browser_os()
    enhance = fingerprint_enhance_enabled()

    opts: dict = {
        "headless": use_headless,  # default headed; set GROK_HEADLESS=1 on broken GPU sessions
        "humanize": True,       # 人类化鼠标移动 + 贝塞尔轨迹；时长保持默认
        "geoip": True,          # 基于 IP 匹配时区 / 语言 / 经纬度
        "block_webrtc": True,   # 防止 WebRTC 泄漏真实 IP（即使使用代理）
        "os": browser_os,       # Windows 配置文件：UA + hints + 字体 + WebGL
        "i_know_what_im_doing": True,  # 抑制 Firefox 版本伪装警告（Camoufox 引擎层伪装是预期行为）
    }
    if not enhance:
        # 回滚：保持历史硬编码 locale，避免 geoip 改 UI 语言
        opts["locale"] = "en-US"
    if use_headless or os.name == "nt":
        # Soften GPU requirements on Windows (headed or headless).
        opts["firefox_user_prefs"] = {
            "gfx.webrender.all": False,
            "gfx.webrender.software": True,
            "layers.acceleration.disabled": True,
            "media.hardware-video-decoding.enabled": False,
        }

    # 旧格式安装兼容：传 executable_path 绕过 installed_verstr() 检查
    # 注意：不传 ff_version，让 Camoufox 自动检测版本号
    # 传 ff_version 会导致指纹中版本号与引擎不匹配，Turnstile 检测到不一致会拒绝通过
    # 前提：config.json 中已设置 active_version="." 让 installed_verstr() 正常工作
    exe_path = _detect_camoufox_exe()
    if exe_path:
        opts["executable_path"] = exe_path

    # 代理 + 出口 IP 预解析
    # Camoufox geoip=True 会经代理访问 ipecho/ipify（库内 timeout=5s）。
    # 住宅 sticky 稍慢就 Failed to get IP → 浏览器启动失败。
    # 这里预解析成功后传 geoip="x.x.x.x"，跳过库内 5s 探测。
    proxies = _proxies()
    proxy = str(proxies.get("https") or proxies.get("http") or "").strip()
    if proxy:
        network_proxy = meter_proxy_url(proxy)
        opts["proxy"] = _build_camoufox_proxy(network_proxy)
        try:
            exit_ip = _resolve_proxy_exit_ip(network_proxy, timeout=8.0)
            blocked, meta = is_blocked_exit_ip(exit_ip)
            if blocked:
                set_exit_context(proxy=proxy, exit_ip=exit_ip)
                raise RuntimeError(
                    f"出口IP命中黑名单，将换 sticky: ip={exit_ip} {meta}"
                )
            if not exit_ip_allowed(exit_ip):
                set_exit_context(proxy=proxy, exit_ip=exit_ip)
                raise RuntimeError(
                    f"出口IP命中黑名单，将换 sticky: ip={exit_ip} "
                    f"not-in-allow {','.join(exit_ip_prefixes())}"
                )
            opts["geoip"] = exit_ip
            # enhance：不传 locale，Camoufox 用 geoip 填 timezone/language。
            # 回滚：上面已写死 en-US；handle_locales 会覆盖 geoip 的 locale。
            set_exit_context(proxy=proxy, exit_ip=exit_ip)
            # 把 ISP 摘要挂到 tls 方便结果日志
            try:
                _tls.exit_meta = meta
            except Exception:
                pass
        except Exception as ip_exc:
            # 已是我们的 RuntimeError 直接抛；探测失败也清空
            if "出口IP命中黑名单" in str(ip_exc):
                raise
            set_exit_context(proxy=proxy, exit_ip="")
            raise RuntimeError(
                f"代理不可用或过慢，无法解析出口 IP（将换 sticky）: {redact_log_line(str(ip_exc))}"
            ) from ip_exc
    else:
        set_exit_context(proxy="", exit_ip="")
        opts.setdefault("locale", "en-US")

    pinned = pick_fp_screen()
    if pinned:
        screen = _screen_from_wh(pinned[0], pinned[1])
        if screen is not None:
            opts["screen"] = screen
        # 2K 指纹时窗口仍限制在当前 Xvfb 1920x1080 内，避免画不出来。
        if pinned[0] > 1920 or pinned[1] > 1080:
            opts["window"] = (1920, 1040)
    elif enhance:
        screen = _xvfb_screen_constraint()
        if screen is not None:
            opts["screen"] = screen
    if enhance:
        webgl_pair = _pick_webgl_config(browser_os)
        if webgl_pair:
            opts["webgl_config"] = webgl_pair

    tz_override = fp_timezone_override()
    if tz_override:
        cfg = dict(opts.get("config") or {})
        cfg["timezone"] = tz_override
        opts["config"] = cfg

    # 扩展（Camoufox 使用 addons 参数，加载 Firefox 扩展目录 / xpi）
    if _extension_path and os.path.exists(_extension_path):
        opts["addons"] = [_extension_path]
    # camoufox fetch 失败时会留下空的 addons/UBO，启动时仍去加载并报
    # "manifest.json is missing"。缺清单的默认扩展直接排除。
    broken_defaults = _broken_default_addons()
    if broken_defaults:
        opts["exclude_addons"] = broken_defaults

    # Profile 隔离
    if unique_profile:
        profile_root = _profile_root()
        profile_dir = str(
            profile_root
            / f"{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex[:8]}"
        )
        ensure_private_dir(profile_dir)
        opts["persistent_context"] = True
        opts["user_data_dir"] = profile_dir
        _tls.profile_dir = profile_dir

    har_path = resolve_record_har_path()
    if har_path:
        opts["record_har_path"] = har_path
        opts["record_har_content"] = "embed"
        opts["record_har_mode"] = "full"
        opts["record_har_url_filter"] = "**/accounts.x.ai/**"

    return opts


def start_browser(log_callback=None) -> Tuple[object, object]:
    """启动 Camoufox 浏览器，返回 (CamoufoxBrowser, CamoufoxPage)。

    使用 Camoufox C++ 引擎层指纹伪装：
    - 从编译层修改 Gecko，JS 完全不可检测
    - 内置 BrowserForge 指纹生成器
    - GeoIP 自动匹配时区 / 语言
    - 人类化鼠标轨迹
    """
    last_exc = None
    try:
        prune_default_har_dirs()
    except Exception:
        pass
    attempt_limit = browser_start_attempts()
    if exit_ip_prefixes():
        try:
            from retry_policy import proxy_boot_rotations

            attempt_limit = max(attempt_limit, int(proxy_boot_rotations() or 0) + 1)
        except Exception:
            attempt_limit = max(attempt_limit, 8)
    attempts_made = 0
    for attempt in range(1, attempt_limit + 1):
        attempts_made = attempt
        profile_dir = None
        try:
            opts = create_browser_options(unique_profile=True)
            profile_dir = getattr(_tls, "profile_dir", None)
            har_rec = str(opts.get("record_har_path") or "")
            if log_callback and har_rec:
                log_callback(f"[har] recording {har_rec}")
            if log_callback and isinstance(opts.get("geoip"), str):
                log_callback(f"[Debug] geoip 使用预解析出口 IP: {opts['geoip']}")
            if log_callback:
                tz_used = str((opts.get("config") or {}).get("timezone") or "").strip()
                scr = opts.get("screen")
                scr_s = ""
                if scr is not None:
                    try:
                        scr_s = f"{int(scr.min_width)}x{int(scr.min_height)}"
                    except Exception:
                        scr_s = str(scr)
                if tz_used or scr_s:
                    log_callback(
                        f"[Debug] fp-override tz={tz_used or 'geoip'} "
                        f"screen={scr_s or 'default'}"
                    )
            if log_callback and fingerprint_enhance_enabled():
                wg = opts.get("webgl_config") or ()
                renderer = str(wg[1] if len(wg) > 1 else "-")
                if len(renderer) > 72:
                    renderer = renderer[:72] + "…"
                loc = opts.get("locale") or "geoip"
                log_callback(
                    f"[Debug] fp-enhance locale={loc} webgl={renderer} "
                    f"(GROK_FP_ENHANCE=0 回滚)"
                )

            # SafeCamoufox.__enter__() 直接创建全新事件循环，
            # 完全绕过 PlaywrightContextManager 的 get_running_loop() 检查
            camoufox = SafeCamoufox(**opts)
            browser_context = camoufox.__enter__()

            # Optional shared cache for immutable browser assets. The helper is
            # a no-op unless GROK_STATIC_ASSET_CACHE is explicitly enabled.
            try:
                import static_asset_cache

                static_asset_cache.attach_static_cache(
                    browser_context,
                    log_callback=log_callback,
                )
            except Exception as cache_exc:
                if log_callback:
                    log_callback(
                        f"[static-cache] attach failed: {redact_log_line(str(cache_exc))}"
                    )

            # 获取或创建页面
            raw_pages = (
                browser_context.pages
                if hasattr(browser_context, "pages")
                else []
            )
            if raw_pages:
                raw_page = raw_pages[0]
            else:
                raw_page = browser_context.new_page()

            # Playwright/Camoufox: uncaught pageerror with empty location can crash
            # the Node driver (location.url on undefined). Swallow safely.
            def _safe_pageerror(err):
                try:
                    msg = str(err)[:200]
                except Exception:
                    msg = "pageerror"
                # do not re-raise
                return None
            try:
                raw_page.on("pageerror", _safe_pageerror)
            except Exception:
                pass
            try:
                browser_context.on("pageerror", _safe_pageerror)
            except Exception:
                pass
            # Prefer reading cookies from context even if page is navigating
            try:
                raw_page.set_default_timeout(60000)
            except Exception:
                pass
            try:
                from turnstile_solver import (
                    attach_browser_turnstile_hooks,
                    load_turnstile_settings,
                    should_attach_browser_turnstile_hooks,
                )

                ts = load_turnstile_settings()
                provider = str(ts.get("provider") or "")
                debug_cf = str(os.environ.get("GROK_CF_DEBUG") or "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                if should_attach_browser_turnstile_hooks(provider, debug=debug_cf):
                    attach_browser_turnstile_hooks(
                        raw_page,
                        browser_context,
                        log_callback=log_callback,
                    )
                    if log_callback:
                        log_callback(f"[*] Turnstile widget hook 已安装 provider={provider}")
            except Exception as hook_exc:
                if log_callback:
                    log_callback(f"[Debug] turnstile hooks failed: {hook_exc}")

            page_obj = CamoufoxPage(raw_page, browser_context)
            browser_obj = CamoufoxBrowser(
                browser=None,
                context=browser_context,
                camoufox_instance=camoufox,
            )
            browser_obj.user_data_path = profile_dir or ""

            set_browser_session(browser_obj, page_obj)
            _note_start_success()

            if log_callback and profile_dir:
                log_callback(f"[Debug] 当前浏览器资料目录: {profile_dir}")
            if log_callback:
                eip = get_exit_ip()
                bpx = get_bound_proxy()
                meta = str(getattr(_tls, "exit_meta", "") or "")
                if eip or bpx:
                    extra = f" | {meta}" if meta else ""
                    log_callback(f"[*] 出口IP={eip or '?'} 代理={redact_proxy(bpx) or '?'}{extra}")
                try:
                    probe = probe_browser_fingerprint(raw_page)
                except Exception:
                    probe = {}
                log_callback(format_fingerprint_log(str(opts.get("os") or "?"), probe))
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return browser_obj, page_obj
        except Exception as exc:
            last_exc = exc
            streak = _note_start_failure()
            if log_callback:
                log_callback(
                    f"[Debug] 浏览器启动失败(第{attempt}/{attempt_limit}次, 连续失败{streak}): {redact_log_line(str(exc))}"
                )
            # 同一 sticky 出口探测失败：默认交给上层换口；若挂了 rotate hook 则本层换口再试。
            msg = str(exc)
            if (
                "无法解析出口 IP" in msg
                or "Failed to get IP address" in msg
                or "代理不可用或过慢" in msg
                or "出口IP命中黑名单" in msg
            ):
                if attempt < attempt_limit and _invoke_proxy_rotate(msg, log_callback):
                    continue
                break
            # EPIPE / 驱动管道断开：稍等再起一个新 Node，不要立刻放弃
            if _is_driver_pipe_error(exc) and attempt < attempt_limit:
                time.sleep(min(1.0 * attempt, 3))
                continue
            profile_dir = profile_dir or getattr(_tls, "profile_dir", None)
            try:
                cur = active_browser()
                if cur is not None:
                    cur.quit(del_data=True)
            except Exception:
                pass
            set_browser_session(None, None)
            _cleanup_profile_dir(profile_dir)
            time.sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已尝试{attempts_made}次: {last_exc}")


def stop_browser(force=False):
    if _debug() and not force:
        return
    current = active_browser()
    profile_dir = getattr(_tls, "profile_dir", None)
    har_path = str(getattr(_tls, "record_har_path", "") or "")
    success_email = str(getattr(_tls, "record_har_success_email", "") or "")
    set_browser_session(None, None)
    if current is None:
        _cleanup_profile_dir(profile_dir)
        return
    try:
        current.quit(del_data=True)
    except BaseException:
        pass
    _cleanup_profile_dir(profile_dir)
    if har_path and success_email:
        try:
            saved = _persist_success_har(har_path, success_email)
            if saved:
                _tls.record_har_saved = saved
        except Exception:
            pass
    _tls.record_har_path = ""
    _tls.record_har_success_email = ""


def restart_browser(log_callback=None):
    stop_browser(force=True)
    return start_browser(log_callback=log_callback)


def cleanup_runtime_memory(log_callback=None, reason="定期清理"):
    try:
        if _debug():
            if log_callback:
                log_callback(f"[*] 调试模式：保留浏览器（{reason}）")
            collected = gc.collect()
            if log_callback:
                log_callback(f"[*] Python GC 已回收对象数: {collected}")
            return
        if log_callback:
            log_callback(f"[*] {reason}: 关闭浏览器并清理内存")
        stop_browser(force=True)
        collected = gc.collect()
        if log_callback:
            log_callback(f"[*] Python GC 已回收对象数: {collected}")
    except BaseException:
        try:
            if not _debug():
                stop_browser(force=True)
        except BaseException:
            pass


def refresh_active_page():
    if active_browser() is None:
        restart_browser()
    try:
        browser_obj = active_browser()
        tabs = browser_obj.get_tabs()
        page_obj = tabs[-1] if tabs else browser_obj.new_tab()
        set_browser_session(browser_obj, page_obj)
    except Exception:
        restart_browser()
    return page


def extract_cf_clearance_and_ua(log_callback=None, ensure_grok=True):
    """提取 grok.com 域 cf_clearance + UA。"""
    cf_clearance = ""
    user_agent = ""
    try:
        active = refresh_active_page()
        if active is None:
            return "", ""

        def _read_cf_and_ua(page_obj, grok_only=False):
            clearance = ""
            ua_text = ""
            cookies = page_obj.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                    domain = str(item.get("domain", "")).strip().lower()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()
                    domain = str(getattr(item, "domain", "")).strip().lower()
                if name != "cf_clearance" or not value:
                    continue
                if grok_only and "grok.com" not in domain:
                    continue
                if "grok.com" in domain:
                    clearance = value
                    break
                if not clearance and not grok_only:
                    clearance = value
            try:
                ua = page_obj.run_js("return navigator.userAgent;")
                if ua:
                    ua_text = str(ua).strip()
            except Exception:
                pass
            return clearance, ua_text

        def _page_passed_cf(page_obj):
            try:
                title = str(
                    page_obj.run_js("return document.title || '';") or ""
                ).lower()
                body = str(
                    page_obj.run_js(
                        "return (document.body && (document.body.innerText||'')) || '';"
                    )
                    or ""
                ).lower()
                if "just a moment" in title or "just a moment" in body[:200]:
                    return False
                if "checking your browser" in body[:300]:
                    return False
                return True
            except Exception:
                return False

        cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
        if ensure_grok and not cf_clearance:
            if log_callback:
                log_callback("[*] 未找到 grok.com 的 cf_clearance，打开 grok.com 过盾...")
            try:
                active.get("https://grok.com/")
                try:
                    active.wait.doc_loaded()
                except Exception:
                    pass
                time.sleep(2)
                for _ in range(20):
                    if _page_passed_cf(active):
                        cf_clearance, user_agent = _read_cf_and_ua(
                            active, grok_only=True
                        )
                        if cf_clearance:
                            break
                    time.sleep(1.0)
                if log_callback:
                    if cf_clearance:
                        log_callback("[*] 已取得 grok.com 的 cf_clearance")
                    else:
                        log_callback(
                            "[!] 打开 grok.com 后仍无有效 cf_clearance"
                            "（页面可能仍卡在 Just a moment）"
                        )
            except Exception as nav_exc:
                if log_callback:
                    log_callback(f"[Debug] 打开 grok.com 取 cf_clearance 失败: {nav_exc}")
                cf_clearance, user_agent = _read_cf_and_ua(active, grok_only=True)
    except Exception as exc:
        if log_callback:
            log_callback(f"[Debug] 提取 cf_clearance 失败: {exc}")
    return cf_clearance, user_agent
