# -*- coding: utf-8 -*-
"""Per-round Castle environment for the Node mint.

Style names come from one real Chromium open. The account fingerprint comes
from the same Windows profile the headed signup path already uses. pk and the
script dpl stay on the HTTP signup page read inside the mint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from browser_session import castle_node_profile, patchright_fingerprint, pick_intel_direct3d

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STYLE_FILE = ROOT / "scripts" / "castle-style-props.json"

_STYLE_JS = """() => {
  const root = getComputedStyle(document.documentElement);
  const body = document.body ? getComputedStyle(document.body) : null;
  const src = !body || root.length >= body.length ? root : body;
  const names = [];
  for (let i = 0; i < src.length; i++) names.push(String(src[i] || ""));
  const sheets = [];
  const list = document.styleSheets || [];
  for (let i = 0; i < list.length && sheets.length < 10; i++) {
    let rules;
    try { rules = list[i].cssRules; } catch (e) { continue; }
    if (!rules) continue;
    const rule = rules.length ? rules[0] : null;
    sheets.push({
      selector: String(rule && rule.selectorText || "").slice(0, 64),
      css_text_len: String(rule && rule.cssText || "").length,
      rule_count: rules.length,
    });
  }
  const marks = performance.getEntriesByType("mark").slice(0, 10).map((mark) => String(mark.name || ""));
  return { names: names.filter(Boolean), sheets, marks: marks.filter(Boolean) };
}"""


def write_castle_fingerprint(path: Path, proxy: str = "", chrome_path: str = "") -> dict:
    """Write one account profile. The file has no proxy URL."""
    profile = patchright_fingerprint(proxy, chrome_path)
    vendor, renderer = pick_intel_direct3d()
    profile["webgl_vendor"] = vendor
    profile["webgl_renderer"] = renderer
    profile = castle_node_profile(profile)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return profile


def harvest_castle_style_names(
    proxy: str,
    dest: Optional[Path] = None,
    chrome_path: str = "",
) -> int:
    """Open the signup page once and store the computed-style property names."""
    from patchright.sync_api import sync_playwright

    from nobrowser_protocol.patchright_engine import (
        GROK_HOME_URL,
        PATCHRIGHT_WEBGL_ARGS,
        _accounts_signup_url,
        _chrome_path,
        _proxy_for_patchright,
    )

    target = Path(dest or os.environ.get("CASTLE_STYLE_FILE") or DEFAULT_STYLE_FILE)
    chrome = str(chrome_path or _chrome_path() or "").strip()
    fp = patchright_fingerprint(proxy, chrome)
    headed = bool(os.environ.get("DISPLAY") or os.environ.get("PATCHRIGHT_HEADED"))
    width, height = fp["screen"]
    args = [f"--window-size={width},{height}", *PATCHRIGHT_WEBGL_ARGS]
    if not headed:
        args.append("--headless=new")
    launch_kw: dict = {"headless": not headed, "args": args}
    proxy_cfg = _proxy_for_patchright(proxy)
    if proxy_cfg:
        launch_kw["proxy"] = proxy_cfg
    if chrome:
        launch_kw["executable_path"] = chrome
    context_kw: dict = {
        "locale": "en-US",
        "user_agent": fp["user_agent"],
        "viewport": {"width": int(width), "height": int(height)},
    }
    if fp.get("timezone_id"):
        context_kw["timezone_id"] = fp["timezone_id"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kw)
        context = browser.new_context(**context_kw)
        page = context.new_page()
        page.goto(
            _accounts_signup_url(),
            wait_until="domcontentloaded",
            timeout=45000,
            referer=GROK_HOME_URL,
        )
        page.wait_for_timeout(4000)
        names = page.evaluate(_STYLE_JS) or []
        context.close()
        browser.close()
    payload = names if isinstance(names, dict) else {"names": names, "sheets": []}
    cleaned = [str(name) for name in (payload.get("names") or []) if str(name or "").strip()]
    sheets = []
    for row in payload.get("sheets") or []:
        if not isinstance(row, dict):
            continue
        sheets.append(
            {
                "selector": str(row.get("selector") or "")[:64],
                "css_text_len": int(row.get("css_text_len") or 0),
                "rule_count": int(row.get("rule_count") or 0),
            }
        )
    marks = [str(name) for name in (payload.get("marks") or []) if str(name or "").strip()][:10]
    if len(cleaned) < 100:
        raise RuntimeError(f"style harvest short: {len(cleaned)}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"names": cleaned, "sheets": sheets, "marks": marks}, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(cleaned)


def prepare_castle_round(proxy: str, style_path: Optional[Path] = None, chrome_path: str = "") -> int:
    """One browser open before a registration round. Returns the style-name count."""
    return harvest_castle_style_names(proxy, style_path, chrome_path)
