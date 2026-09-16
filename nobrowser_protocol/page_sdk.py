# -*- coding: utf-8 -*-
"""Live page SDK snippets copied from protocol_register_once (same Castle/Sentry APIs)."""

CASTLE_MINT_JS = """
async (email) => {
  const html = document.documentElement ? document.documentElement.innerHTML : '';
  const pkMatch = html.match(/"castlePk":"(pk_[^"]+)"/);
  const pk = pkMatch ? pkMatch[1] : '';
  let mint = null;
  const seen = new Set();
  const consider = (bag) => {
    if (!bag || typeof bag !== 'object') return;
    if (typeof bag.createRequestToken === 'function') mint = bag.createRequestToken;
    else if (bag.value && typeof bag.value.createRequestToken === 'function') mint = bag.value.createRequestToken;
    else if (bag._currentValue && typeof bag._currentValue.createRequestToken === 'function') mint = bag._currentValue.createRequestToken;
  };
  const walk = (fiber, depth) => {
    if (!fiber || depth > 80 || seen.has(fiber) || mint) return;
    seen.add(fiber);
    consider(fiber.memoizedProps);
    consider(fiber.pendingProps);
    consider(fiber.memoizedState);
    consider(fiber.stateNode);
    consider(fiber.type);
    try { walk(fiber.child, depth + 1); } catch (e) {}
    if (mint) return;
    try { walk(fiber.sibling, depth + 1); } catch (e) {}
  };
  const roots = [document.getElementById('__next'), document.querySelector('[data-reactroot]'), document.body];
  for (const root of roots) {
    if (!root) continue;
    for (const key of Object.keys(root)) {
      if (key.startsWith('__react')) walk(root[key], 0);
    }
  }
  if (!mint) return { token: '', how: 'react_missing', pk };
  try {
    const token = await mint({ method: 'email_password', flow: 'signup', email });
    return { token: String(token || ''), how: 'useCastle.createRequestToken', pk };
  } catch (e) {
    return { token: '', how: 'mint_error:' + String((e && e.message) || e).slice(0, 120), pk };
  }
}
"""

SENTRY_JS = """
() => {
  const out = { sentry_trace: '', baggage: '', how: 'missing' };
  try {
    const S = window.Sentry;
    if (S && typeof S.getTraceData === 'function') {
      const d = S.getTraceData() || {};
      out.sentry_trace = String(d['sentry-trace'] || d.sentry_trace || '');
      out.baggage = String(d.baggage || '');
      out.how = 'Sentry.getTraceData';
      return out;
    }
  } catch (e) {}
  return out;
}
"""

SITEKEY_JS = """
() => {
  const keys = [];
  const push = (v) => {
    v = String(v || '').trim();
    if (/^0x[0-9A-Za-z_-]{8,}$/.test(v)) keys.push(v);
  };
  document.querySelectorAll('[data-sitekey]').forEach((el) => push(el.getAttribute('data-sitekey')));
  document.querySelectorAll('iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"]').forEach((el) => {
    const src = String(el.src || '');
    const m = src.match(/0x4[0-9A-Za-z_-]{8,}/);
    if (m) push(m[0]);
  });
  return keys[0] || '';
}
"""

PAGE_POST_JS = """
async ({ url, body, headers }) => {
  const hdrs = Object.assign({ 'content-type': 'application/json' }, headers || {});
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: hdrs,
    body: JSON.stringify(body || {}),
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) {}
  return {
    status: res.status,
    ok: res.ok,
    text: String(text || '').slice(0, 2000),
    json,
    sent_header_names: Object.keys(hdrs),
  };
}
"""
