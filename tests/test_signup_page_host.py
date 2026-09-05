# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import inspect

from register_flow import (  # noqa: E402
    _reload_signup_and_open_email,
    signup_page_is_accounts_host,
)


def test_accounts_host_accepts_signup_url():
    assert signup_page_is_accounts_host(
        "https://accounts.x.ai/sign-up?redirect=grok-com"
    )


def test_accounts_host_rejects_xcom_oauth_with_xai_redirect_query():
    url = (
        "https://x.com/i/oauth2/authorize?response_type=code"
        "&client_id=V3NubFR4MTR6S0RjenkwZjBfOWI6MTpjaQ"
        "&redirect_uri=https%3A%2F%2Faccounts.x.ai%2Fexchange-token%2F"
        "&scope=tweet.read+users.read+users.email+offline.access"
    )
    assert not signup_page_is_accounts_host(url)


def test_accounts_host_rejects_blank_and_grok():
    assert not signup_page_is_accounts_host("about:blank")
    assert not signup_page_is_accounts_host("https://grok.com/")
    assert not signup_page_is_accounts_host("")


def test_reload_signup_navigates_when_not_accounts_host():
    source = inspect.getsource(_reload_signup_and_open_email)
    assert "signup_page_is_accounts_host" in source
    assert "page.get(SIGNUP_URL)" in source
