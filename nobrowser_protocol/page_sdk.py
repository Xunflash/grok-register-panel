# -*- coding: utf-8 -*-
"""Live page SDK snippets copied from protocol_register_once (same Castle/Sentry APIs)."""

CASTLE_MINT_JS = """
async (email) => {
  const html = document.documentElement ? document.documentElement.innerHTML : '';
  const pkMatch = html.match(/"castlePk":"(pk_[^"]+)"/);
  const pk = pkMatch ? pkMatch[1] : '';
  try {
    if (window.Castle && typeof window.Castle.createRequestToken === 'function') {
      const token = await window.Castle.createRequestToken({ method: 'email_password', flow: 'signup', email });
      if (token) return { token: String(token), how: 'Castle.createRequestToken', pk };
    }
  } catch (e) {}
  try {
    if (typeof window._castle === 'function') {
      const token = await window._castle('createRequestToken');
      if (token) return { token: String(token), how: '_castle', pk };
    }
  } catch (e) {}
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

CLICK_VISIBLE_JS = """
({ needles, deny }) => {
  const compact = (s) => String(s || '').replace(/\\s+/g, '').toLowerCase();
  const need = (needles || []).map(compact).filter(Boolean);
  const skip = (deny || []).map(compact).filter(Boolean);
  const isVisible = (n) => {
    if (!n) return false;
    const st = window.getComputedStyle(n);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = n.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const label = (n) => ((n.innerText || n.textContent || n.value || n.getAttribute('aria-label') || n.getAttribute('title') || '') + '').replace(/\\s+/g, ' ').trim();
  const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'));
  let best = null, bestScore = 0, bestText = '';
  for (const n of nodes) {
    if (!isVisible(n) || n.disabled || n.getAttribute('aria-disabled') === 'true') continue;
    const t = label(n);
    const c = compact(t);
    if (!c || skip.some((d) => c.includes(d))) continue;
    let score = 0;
    for (const k of need) {
      if (k && c.includes(k)) score = Math.max(score, k.length);
    }
    if (score > bestScore) { bestScore = score; best = n; bestText = t; }
  }
  if (!best) return '';
  best.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
  best.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
  best.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
  best.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
  best.click();
  return bestText.slice(0, 80);
}
"""

FILL_EMAIL_JS = """
(email) => {
  const visible = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (auto === 'username') return false;
    if (el.closest('[hidden]')) return false;
    const cls = String(el.className || '');
    if (/(^|\\s)hidden(\\s|$)/.test(cls)) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const input = Array.from(document.querySelectorAll(
    '#email, input[data-testid="email"], input[autocomplete="email"]'
  )).find(visible);
  if (!input) return { ok: false, reason: 'no-visible-email' };
  const value = String(email || '');
  input.focus();
  const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  const setter = desc && desc.set;
  const write = (next) => {
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue(input.value);
    if (setter) setter.call(input, next);
    else input.value = next;
  };
  const reactKey = Object.keys(input).find((k) => k.startsWith('__reactProps$'));
  const props = reactKey ? input[reactKey] : null;
  const emit = (next) => {
    write(next);
    const native = new InputEvent('input', {
      bubbles: true,
      cancelable: true,
      data: next,
      inputType: next ? 'insertText' : 'deleteContentBackward',
    });
    const synthetic = {
      target: input,
      currentTarget: input,
      nativeEvent: native,
      preventDefault() {},
      stopPropagation() {},
      isDefaultPrevented() { return false; },
      isPropagationStopped() { return false; },
    };
    if (props && typeof props.onChange === 'function') props.onChange(synthetic);
    else input.dispatchEvent(native);
  };
  emit('');
  emit(value);
  const blur = {
    target: input,
    currentTarget: input,
    nativeEvent: new FocusEvent('blur'),
    preventDefault() {},
    stopPropagation() {},
  };
  if (props && typeof props.onBlur === 'function') props.onBlur(blur);
  else {
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
  }
  return {
    ok: (input.value || '') === value && value.length > 0,
    value: input.value || '',
    id: input.id || '',
    name: input.getAttribute('name') || '',
    auto: input.getAttribute('autocomplete') || '',
  };
}
"""

SUBMIT_EMAIL_JS = """
() => {
  const visible = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (auto === 'username') return false;
    if (el.closest('[hidden]')) return false;
    const cls = String(el.className || '');
    if (/(^|\\s)hidden(\\s|$)/.test(cls)) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const input = Array.from(document.querySelectorAll(
    '#email, input[data-testid="email"], input[autocomplete="email"]'
  )).find(visible) || null;
  const deny = /with email|with apple|with google|with x\\b|使用邮箱|correo|regresar|privacidad|go back|privacy/i;
  const form = input ? input.closest('form') : null;
  const root = form || document;
  const nodes = Array.from(root.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'));
  const hit = nodes.find((n) => {
    if (n.disabled || n.getAttribute('aria-disabled') === 'true') return false;
    if (!visible(n) && n.getAttribute('type') !== 'submit') return false;
    const t = ((n.innerText || n.textContent || n.value || '') + '').replace(/\\s+/g, ' ').trim();
    const testid = (n.getAttribute('data-testid') || '').toLowerCase();
    const type = (n.getAttribute('type') || '').toLowerCase();
    if (deny.test(t)) return false;
    if (type === 'submit') return true;
    if (testid.includes('submit') || testid.includes('continue') || testid === 'sign-up') return true;
    return /sign\\s*up|continue|next|注册|继续|regístrate|registrate|registrieren|inscrire|registrati|inscrever/i.test(t);
  });
  if (hit) {
    hit.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true }));
    hit.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, cancelable: true }));
    hit.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    hit.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
    hit.click();
    return ((hit.innerText || hit.getAttribute('data-testid') || 'clicked') + '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  }
  if (form && typeof form.requestSubmit === 'function') {
    try { form.requestSubmit(); return 'form-submit'; } catch (e) {}
  }
  return input ? 'no-submit' : 'no-visible-email';
}
"""

FILL_OTP_JS = """
(code) => {
  const input = document.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[data-input-otp], input[inputmode="numeric"]');
  if (!input) return { ok: false, reason: 'no-input' };
  input.focus();
  input.click();
  const proto = HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value') && Object.getOwnPropertyDescriptor(proto, 'value').set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) setter.call(input, code); else input.value = code;
  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: code, inputType: 'insertText' }));
  input.dispatchEvent(new InputEvent('input', { bubbles: true, data: code, inputType: 'insertText' }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: !!(input.value), value: String(input.value || ''), name: input.getAttribute('name') || '' };
}
"""

FILL_CONTROL_JS = """
({ selector, value }) => {
  const visible = (el) => {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.closest('[hidden]')) return false;
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const input = Array.from(document.querySelectorAll(selector || '')).find(visible);
  if (!input) return { ok: false, reason: 'no-visible-control' };
  const next = String(value || '');
  input.focus();
  const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  const setter = desc && desc.set;
  const write = (text) => {
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue(input.value);
    if (setter) setter.call(input, text);
    else input.value = text;
  };
  const reactKey = Object.keys(input).find((k) => k.startsWith('__reactProps$'));
  const props = reactKey ? input[reactKey] : null;
  const emit = (text) => {
    write(text);
    const native = new InputEvent('input', { bubbles: true, cancelable: true, data: text, inputType: text ? 'insertText' : 'deleteContentBackward' });
    const synthetic = {
      target: input,
      currentTarget: input,
      nativeEvent: native,
      preventDefault() {},
      stopPropagation() {},
      isDefaultPrevented() { return false; },
      isPropagationStopped() { return false; },
    };
    if (props && typeof props.onChange === 'function') props.onChange(synthetic);
    else input.dispatchEvent(native);
  };
  emit('');
  emit(next);
  if (props && typeof props.onBlur === 'function') {
    props.onBlur({ target: input, currentTarget: input, nativeEvent: new FocusEvent('blur'), preventDefault() {}, stopPropagation() {} });
  } else {
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
  }
  return { ok: (input.value || '') === next && next.length > 0, value: input.value || '', name: input.getAttribute('name') || '' };
}
"""

PROFILE_VISIBLE_JS = """
() => {
  const el = document.querySelector('input[name="givenName"], input[autocomplete="given-name"], input[data-testid="givenName"]');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
"""

INJECT_TURNSTILE_JS = """
(token) => {
  const value = String(token || '').trim();
  if (!value) return { ok: false, reason: 'empty-token' };
  window.__grokCfToken = value;
  const setNative = (input) => {
    const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (desc && desc.set) desc.set.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  let filled = 0;
  document.querySelectorAll('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').forEach((el) => {
    setNative(el);
    filled += 1;
  });
  if (!filled) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'cf-turnstile-response';
    const form = document.querySelector('form') || document.body;
    form.appendChild(input);
    setNative(input);
    filled = 1;
  }
  try {
    if (window.turnstile) window.turnstile.getResponse = () => window.__grokCfToken || value;
  } catch (e) {}
  document.querySelectorAll('[data-callback]').forEach((el) => {
    const name = el.getAttribute('data-callback');
    try {
      if (name && typeof window[name] === 'function') window[name](value);
    } catch (e) {}
  });
  return { ok: true, filled };
}
"""

OTP_VISIBLE_JS = """
() => {
  const n = document.querySelector('input[name="code"], input[autocomplete="one-time-code"], input[data-input-otp]');
  if (!n) return false;
  const r = n.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
"""

DUMP_PAGE_JS = """
() => {
  const vis = (n) => {
    if (!n) return false;
    const r = n.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const text = (n) => ((n.innerText || n.textContent || n.value || '') + '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const emails = Array.from(document.querySelectorAll('input')).map((n) => ({
    type: n.getAttribute('type') || '',
    name: n.getAttribute('name') || '',
    auto: n.getAttribute('autocomplete') || '',
    vis: vis(n),
  }));
  const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(vis).map(text).filter(Boolean).slice(0, 16);
  return { title: document.title, url: location.href, emails: emails.slice(0, 12), buttons };
}
"""
