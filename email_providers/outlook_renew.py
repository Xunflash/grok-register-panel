# -*- coding: utf-8 -*-
"""Renew Outlook Graph refresh tokens from existing inventory passwords.

Two modes:
- rt-only: log in with the current password and mint a new Graph RT
- change-password: change the MSA password, then mint a new Graph RT

Password change is best-effort HTTP against account.live.com. Accounts that
hit identity proofs / interruption are skipped, not retried in a loop.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import string
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

from email_providers import outlook_rt

CLIENT_ID = outlook_rt.DEFAULT_CLIENT_ID
REDIRECT_URI = "http://localhost"
SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
AUTHORIZE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
PASSWORD_CHANGE_URLS = (
    "https://account.live.com/password/Change",
    "https://account.live.com/password/Change.aspx",
)
UI_LOCALE = os.environ.get("OUTLOOK_UI_LOCALE", "en-US").strip() or "en-US"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class MicrosoftFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: List[Dict[str, Any]] = []
        self.inputs: Dict[str, str] = {}
        self._form: Optional[Dict[str, Any]] = None

    def handle_starttag(self, tag, attrs):
        attributes = {str(key).lower(): value or "" for key, value in attrs}
        if tag.lower() == "form":
            if self._form is not None:
                self.forms.append(self._form)
            self._form = {
                "action": attributes.get("action", ""),
                "method": attributes.get("method", "post").lower(),
                "inputs": {},
            }
        elif tag.lower() == "input":
            name = attributes.get("name", "")
            if not name:
                return
            value = attributes.get("value", "")
            self.inputs[name.lower()] = value
            if self._form is not None:
                self._form["inputs"][name] = value

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def close(self):
        super().close()
        if self._form is not None:
            self.forms.append(self._form)
            self._form = None


def parse_microsoft_forms(text: str, base_url: str = "") -> tuple[list[dict], dict[str, str]]:
    parser = MicrosoftFormParser()
    parser.feed(text or "")
    parser.close()
    forms = []
    for form in parser.forms:
        action = html.unescape(form["action"] or base_url)
        forms.append({**form, "action": urllib.parse.urljoin(base_url, action)})
    return forms, parser.inputs


def generate_password(old: str = "", length: int = 12) -> str:
    length = max(10, min(24, int(length or 12)))
    alphabet = string.ascii_letters + string.digits
    for _ in range(30):
        chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
        ]
        chars.extend(secrets.choice(alphabet) for _ in range(length - 3))
        secrets.SystemRandom().shuffle(chars)
        password = "".join(chars)
        if password != str(old or "") and not str(old or "").lower() in password.lower():
            return password
    return "A" + secrets.token_hex(5)


def fill_password_change_form(inputs: Dict[str, str], old: str, new: str) -> Dict[str, str]:
    filled = dict(inputs or {})
    unmatched: List[str] = []
    for name in list(filled):
        low = name.lower()
        if any(token in low for token in ("old", "current", "prev")):
            filled[name] = old
        elif any(token in low for token in ("confirm", "retype", "verify", "repeat")):
            filled[name] = new
        elif "new" in low and ("pass" in low or "pwd" in low):
            filled[name] = new
        elif "pass" in low or "pwd" in low:
            unmatched.append(name)
    if unmatched:
        unmatched.sort()
        if len(unmatched) >= 3:
            filled[unmatched[0]] = old
            filled[unmatched[1]] = new
            filled[unmatched[2]] = new
        elif len(unmatched) == 2:
            filled[unmatched[0]] = new
            filled[unmatched[1]] = new
        elif len(unmatched) == 1:
            filled[unmatched[0]] = new
    return filled


def _redirect_url(response: Any, location: str) -> str:
    return urllib.parse.urljoin(getattr(response, "url", "") or "", html.unescape(location or ""))


def _extract_ppft(text: str, page_inputs: Dict[str, str]) -> str:
    match = re.search(r'sFTTag.*?value=\\?"([^"\\]+)', text or "")
    if match:
        return match.group(1)
    return str(page_inputs.get("ppft") or "")


def _extract_url_post(text: str) -> str:
    match = re.search(r'"urlPost"\s*:\s*"([^"]+)"', text or "")
    if not match:
        return ""
    return match.group(1).replace("\\u0026", "&")


def _code_from_url(url: str) -> Optional[str]:
    if "localhost" not in (url or "") or "code=" not in (url or ""):
        return None
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return (params.get("code") or [None])[0]


def _error_from_url(url: str) -> str:
    if "localhost" not in (url or "") or "error" not in (url or ""):
        return ""
    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return str(parsed.get("error_description", parsed.get("error", [""]))[0] or "")


class _LocalRedirect:
    def __init__(self, url: str):
        self.url = url
        self.text = ""
        self.status_code = 200
        self.headers = {}


def _authorize_url() -> str:
    return (
        f"{AUTHORIZE_URL}"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI, safe='')}"
        f"&scope={urllib.parse.quote(SCOPE)}"
        f"&response_mode=query"
        f"&mkt={urllib.parse.quote(UI_LOCALE)}"
        f"&ui_locales={urllib.parse.quote(UI_LOCALE)}"
    )


def _follow_login_challenges(session: Any, response: Any, timeout: int = 30) -> Any:
    current = response
    for _ in range(15):
        while getattr(current, "status_code", 0) in (301, 302, 303, 307):
            location = _redirect_url(current, current.headers.get("Location", ""))
            if "localhost" in location:
                return _LocalRedirect(location)
            current = session.get(location, timeout=timeout, allow_redirects=False)
        url = getattr(current, "url", "") or ""
        text = getattr(current, "text", "") or ""
        if _code_from_url(url) or _error_from_url(url):
            return current
        if "consent/update" in url.lower():
            match = re.search(r"ServerData\s*=\s*(\{.*?\});", text, re.DOTALL)
            if not match:
                raise RuntimeError("Consent/Update 无 ServerData")
            data = json.loads(match.group(1))
            current = session.post(
                url,
                data={
                    "ucaction": "Yes",
                    "client_id": data.get("sClientId", ""),
                    "scope": data.get("sRawInputScopes", ""),
                    "cscope": data.get("sRawInputGrantedScopes", ""),
                    "canary": data.get("sCanary", ""),
                },
                timeout=timeout,
                allow_redirects=False,
            )
            continue
        if "proofs/add" in url.lower():
            forms, _ = parse_microsoft_forms(text, url)
            if not forms:
                raise RuntimeError("proofs/Add 需要安全信息，已跳过失败")
            payload = dict(forms[0]["inputs"])
            payload["action"] = "Skip"
            current = session.post(
                forms[0]["action"],
                data=payload,
                timeout=timeout,
                allow_redirects=False,
            )
            continue
        lowered = text.lower()
        if "dosubmit" in lowered or ("fmhf" in lowered and "onload" in lowered):
            forms, _ = parse_microsoft_forms(text, url)
            if forms:
                current = session.post(
                    forms[0]["action"],
                    data=forms[0]["inputs"],
                    timeout=timeout,
                    allow_redirects=True,
                )
                continue
        forms, _ = parse_microsoft_forms(text, url)
        if forms:
            payload = dict(forms[0]["inputs"])
            action = forms[0]["action"]
            if "consent" in action.lower() or "consent" in url.lower():
                payload["ucaccept"] = "Yes"
            current = session.post(action, data=payload, timeout=timeout, allow_redirects=False)
            continue
        break
    return current


def login_with_password(session: Any, email: str, password: str, timeout: int = 30) -> Any:
    response = session.get(_authorize_url(), timeout=timeout, allow_redirects=True)
    text = response.text or ""
    forms, page_inputs = parse_microsoft_forms(text, response.url)
    flow_token = _extract_ppft(text, page_inputs)
    post_url = _extract_url_post(text)
    if not post_url:
        credential = next(
            (form for form in forms if "ppft" in {name.lower() for name in form["inputs"]}),
            None,
        )
        post_url = (credential or {}).get("action") or "https://login.live.com/ppsecure/post.srf"
    else:
        post_url = urllib.parse.urljoin(response.url, html.unescape(post_url))
    if not flow_token:
        raise RuntimeError("登录页没有 PPFT")
    ctx = ""
    match = re.search(r'"sCtx"\s*:\s*"([^"]+)"', text)
    if match:
        ctx = match.group(1)
    posted = session.post(
        post_url,
        data={
            "login": email,
            "loginfmt": email,
            "passwd": password,
            "PPFT": flow_token,
            "ctx": ctx,
            "type": "11",
            "LoginOptions": "3",
            "i13": "0",
            "CookieDisclosure": "0",
            "IsFidoSupported": "0",
            "isSignupPost": "0",
            "i19": "16393",
        },
        timeout=timeout,
        allow_redirects=True,
    )
    followed = _follow_login_challenges(session, posted, timeout=timeout)
    url = getattr(followed, "url", "") or ""
    text = getattr(followed, "text", "") or ""
    blob = f"{url} {text}".lower()
    if _code_from_url(url):
        return followed
    if "interrupt/credentialaction" in url.lower():
        raise RuntimeError("Microsoft 登录中断：要求绑定恢复邮箱")
    if any(token in blob for token in (
        "password is incorrect",
        "incorrect password",
        "that password is incorrect",
        "密码不正确",
        "帐户或密码不正确",
        "account or password is incorrect",
    )):
        raise RuntimeError("登录密码不正确")
    if "login.live.com" in url.lower() and "passwd" in text.lower():
        raise RuntimeError(f"登录未完成，仍停在登录页: {url[:120]}")
    return followed


def exchange_code(session: Any, code: str, timeout: int = 30) -> str:
    response = session.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
        },
        timeout=timeout,
    )
    payload = {}
    try:
        payload = response.json() if hasattr(response, "json") else json.loads(response.text or "{}")
    except Exception:
        payload = {}
    refresh = str(payload.get("refresh_token") or "").strip()
    if not refresh:
        err = payload.get("error_description") or payload.get("error") or response.text
        raise RuntimeError(f"换 token 失败: {str(err)[:180]}")
    return refresh


def obtain_refresh_token(session: Any, email: str, password: str, timeout: int = 30) -> str:
    current = login_with_password(session, email, password, timeout=timeout)
    code = _code_from_url(getattr(current, "url", "") or "")
    if not code:
        err = _error_from_url(getattr(current, "url", "") or "")
        if err:
            raise RuntimeError(f"OAuth 拒绝: {err[:180]}")
        current = session.get(_authorize_url(), timeout=timeout, allow_redirects=False)
        current = _follow_login_challenges(session, current, timeout=timeout)
        code = _code_from_url(getattr(current, "url", "") or "")
    if not code:
        raise RuntimeError("未拿到 authorization code")
    return exchange_code(session, code, timeout=timeout)


def _password_change_succeeded(url: str, text: str) -> bool:
    blob = f"{url} {text}".lower()
    markers = (
        "password has been changed",
        "password was changed",
        "your password has been updated",
        "we've updated your password",
        "you can now use your new password",
        "密码已更改",
        "密码已修改",
        "password/changed",
        "changepasswordsuccess",
    )
    return any(marker in blob for marker in markers)


def change_password(session: Any, old_password: str, new_password: str, timeout: int = 30) -> None:
    last_error = "改密页不可达"
    for url in PASSWORD_CHANGE_URLS:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        page_url = getattr(response, "url", url) or url
        text = getattr(response, "text", "") or ""
        if any(token in page_url.lower() for token in ("interrupt", "identity", "recover", "abuse")):
            last_error = f"改密被中断: {page_url[:120]}"
            continue
        forms, page_inputs = parse_microsoft_forms(text, page_url)
        if not forms:
            last_error = "改密页没有表单"
            continue
        form = forms[0]
        payload = fill_password_change_form(dict(form["inputs"]), old_password, new_password)
        if "ppft" not in {name.lower() for name in payload} and page_inputs.get("ppft"):
            payload["PPFT"] = page_inputs["ppft"]
        posted = session.post(form["action"], data=payload, timeout=timeout, allow_redirects=True)
        posted_url = getattr(posted, "url", "") or ""
        posted_text = getattr(posted, "text", "") or ""
        if _password_change_succeeded(posted_url, posted_text):
            return
        if any(token in posted_text.lower() for token in ("does not match", "don't match", "do not match")):
            raise RuntimeError(f"新密码两次不一致 fields={list(payload)[:12]}")
        if any(token in posted_text.lower() for token in ("incorrect password", "密码不正确", "current password")):
            raise RuntimeError(f"改密拒绝当前密码 page={posted_url[:120]} fields={list(payload)[:12]}")
        last_error = f"改密未确认成功: {posted_url[:120]} fields={list(payload)[:12]}"
    raise RuntimeError(last_error)


def rt_still_works(account: Dict[str, str], http_post: Callable) -> bool:
    try:
        outlook_rt.refresh_access_token(
            http_post,
            dict(account),
            persist_refresh=False,
        )
        return True
    except Exception:
        return False


def new_session(proxy: str = ""):
    import requests

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": f"{UI_LOCALE},en;q=0.8",
    })
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
        session.trust_env = False
    else:
        session.trust_env = True
    return session


def renew_one(
    account: Dict[str, str],
    *,
    mode: str = "rt-only",
    session_factory: Callable = new_session,
    proxy: str = "",
    http_post: Optional[Callable] = None,
    inventory_path: str = "",
) -> Dict[str, str]:
    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "").strip()
    if not email or "@" not in email:
        raise RuntimeError("缺少邮箱")
    if not password:
        raise RuntimeError("库存没有密码，无法登录重拿 RT")
    session = session_factory(proxy)
    new_password = password
    if mode == "change-password":
        login_with_password(session, email, password)
        new_password = generate_password(password)
        change_password(session, password, new_password)
        if inventory_path:
            outlook_rt.update_inventory_credentials(
                inventory_path,
                email,
                password=new_password,
                client_id=str(account.get("client_id") or CLIENT_ID),
            )
        refresh = obtain_refresh_token(session, email, new_password)
    else:
        refresh = obtain_refresh_token(session, email, password)
    if http_post is not None:
        outlook_rt.refresh_access_token(
            http_post,
            {
                "email": email,
                "password": new_password,
                "client_id": CLIENT_ID,
                "refresh_token": refresh,
            },
            persist_refresh=False,
        )
    if inventory_path:
        outlook_rt.update_inventory_credentials(
            inventory_path,
            email,
            refresh_token=refresh,
            password=new_password,
            client_id=str(account.get("client_id") or CLIENT_ID),
        )
    return {
        "email": email,
        "password": new_password,
        "client_id": str(account.get("client_id") or CLIENT_ID),
        "refresh_token": refresh,
        "mode": mode,
    }


def renew_inventory(
    inventory_path: str,
    *,
    mode: str = "rt-only",
    only_dead: bool = True,
    unused_only: bool = False,
    limit: int = 0,
    emails: Optional[List[str]] = None,
    proxy: str = "",
    session_factory: Callable = new_session,
    http_post: Optional[Callable] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, int]:
    wanted = {item.strip().lower() for item in (emails or []) if str(item).strip()}
    accounts = outlook_rt.load_inventory(inventory_path)
    used = set()
    if unused_only:
        used = outlook_rt._load_used(outlook_rt.used_path_for(inventory_path))
    stats = {"ok": 0, "skip": 0, "fail": 0, "wrote": 0}
    emit = log or (lambda _msg: None)
    processed = 0
    if http_post is None:
        import requests

        http_post = requests.post
    for account in accounts:
        email = account["email"]
        if wanted and email.lower() not in wanted:
            continue
        if unused_only and email.lower() in used:
            continue
        if not str(account.get("password") or "").strip():
            stats["skip"] += 1
            emit(f"[skip] {email} 没有密码")
            continue
        if only_dead and account.get("refresh_token") and rt_still_works(account, http_post):
            stats["skip"] += 1
            emit(f"[skip] {email} RT 仍可用")
            continue
        if limit and processed >= limit:
            break
        processed += 1
        try:
            result = renew_one(
                account,
                mode=mode,
                session_factory=session_factory,
                proxy=proxy,
                http_post=http_post,
                inventory_path=inventory_path,
            )
            wrote = outlook_rt.update_inventory_credentials(
                inventory_path,
                email,
                refresh_token=result["refresh_token"],
                password=result["password"],
                client_id=result["client_id"],
            )
            stats["ok"] += 1
            if wrote:
                stats["wrote"] += 1
            emit(f"[ok] {email} mode={mode} rt_len={len(result['refresh_token'])}")
        except Exception as exc:
            stats["fail"] += 1
            emit(f"[fail] {email} {exc}")
        time.sleep(0.2)
    return stats
