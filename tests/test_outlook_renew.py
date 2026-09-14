# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from email_providers import outlook_rt
from email_providers.outlook_renew import (
    fill_password_change_form,
    generate_password,
    parse_microsoft_forms,
    renew_inventory,
    renew_one,
)


class FakeResponse:
    def __init__(self, url, text="", status=200, headers=None, payload=None):
        self.url = url
        self.text = text
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text or "{}")


class FakeSession:
    def __init__(self, proxy=""):
        self.proxy = proxy
        self.calls = []

    def get(self, url, timeout=30, allow_redirects=True):
        self.calls.append(("GET", url))
        if "authorize" in url:
            return FakeResponse(
                url,
                text=(
                    '<script>var t="sFTTag";</script>'
                    '<input name="PPFT" value="token1"/>'
                    '"urlPost":"https://login.live.com/ppsecure/post.srf"'
                    '"sCtx":"ctx1"'
                ),
            )
        if "password/Change" in url:
            return FakeResponse(
                url,
                text=(
                    '<form action="https://account.live.com/password/Change" method="post">'
                    '<input name="PPFT" value="ppt"/>'
                    '<input name="iOldPassword" value=""/>'
                    '<input name="iNewPassword" value=""/>'
                    '<input name="iConfirmNewPassword" value=""/>'
                    "</form>"
                ),
            )
        return FakeResponse(url, text="")

    def post(self, url, data=None, timeout=30, allow_redirects=True):
        self.calls.append(("POST", url, dict(data or {})))
        if "ppsecure/post.srf" in url:
            return FakeResponse("http://localhost/?code=AUTHCODE")
        if "password/Change" in url:
            return FakeResponse(url, text="Your password has been changed")
        if "oauth2/v2.0/token" in url:
            return FakeResponse(
                url,
                payload={"refresh_token": "M.C5_NEW_RT", "access_token": "at"},
            )
        return FakeResponse(url, text="")


def test_generate_password_differs_and_has_mix():
    old = "oldpass1234"
    new = generate_password(old)
    assert new != old
    assert len(new) >= 10
    assert any(c.isupper() for c in new)
    assert any(c.islower() for c in new)
    assert any(c.isdigit() for c in new)


def test_fill_password_change_form_named_fields():
    filled = fill_password_change_form(
        {
            "PPFT": "x",
            "iOldPassword": "",
            "iNewPassword": "",
            "iConfirmNewPassword": "",
        },
        "oldpw",
        "Newpw12345A",
    )
    assert filled["iOldPassword"] == "oldpw"
    assert filled["iNewPassword"] == "Newpw12345A"
    assert filled["iConfirmNewPassword"] == "Newpw12345A"
    assert filled["PPFT"] == "x"


def test_parse_microsoft_forms_action():
    html = '<form action="/submit" method="post"><input name="PPFT" value="t"/></form>'
    forms, inputs = parse_microsoft_forms(html, "https://login.live.com/")
    assert forms[0]["action"] == "https://login.live.com/submit"
    assert inputs["ppft"] == "t"


def test_renew_one_change_password_and_inventory_write():
    with tempfile.TemporaryDirectory() as tmp:
        inv = Path(tmp) / "stock.txt"
        inv.write_text(
            "keep@outlook.com----oldpw----cid----OLD_RT\n",
            encoding="utf-8",
        )
        account = {
            "email": "keep@outlook.com",
            "password": "oldpw",
            "client_id": "cid",
            "refresh_token": "OLD_RT",
        }
        result = renew_one(
            account,
            mode="change-password",
            session_factory=FakeSession,
            inventory_path=str(inv),
        )
        assert result["refresh_token"] == "M.C5_NEW_RT"
        assert result["password"] != "oldpw"
        line = inv.read_text(encoding="utf-8").strip()
        parts = line.split("----")
        assert parts[0] == "keep@outlook.com"
        assert parts[1] == result["password"]
        assert parts[3] == "M.C5_NEW_RT"


def test_renew_inventory_skips_used_and_limits():
    with tempfile.TemporaryDirectory() as tmp:
        inv = Path(tmp) / "stock.txt"
        inv.write_text(
            "used@outlook.com----pw1----cid----RT1\n"
            "one@outlook.com----pw2----cid----RT2\n"
            "two@outlook.com----pw3----cid----RT3\n",
            encoding="utf-8",
        )
        (Path(tmp) / "stock.txt.used").write_text("used@outlook.com\n", encoding="utf-8")
        stats = renew_inventory(
            str(inv),
            mode="change-password",
            only_dead=False,
            unused_only=True,
            limit=1,
            session_factory=FakeSession,
            http_post=lambda *args, **kwargs: SimpleNamespace(
                json=lambda: {
                    "access_token": "at",
                    "refresh_token": "M.C5_NEW_RT",
                    "scope": "https://graph.microsoft.com/Mail.Read offline_access",
                },
                text="{}",
                status_code=200,
            ),
        )
        assert stats["ok"] == 1
        assert stats["fail"] == 0
        text = inv.read_text(encoding="utf-8")
        assert "M.C5_NEW_RT" in text
        assert "used@outlook.com----pw1----cid----RT1" in text


def test_update_inventory_credentials_keeps_used_file():
    with tempfile.TemporaryDirectory() as tmp:
        inv = Path(tmp) / "stock.txt"
        inv.write_text("a@outlook.com----pw----cid----RT\n", encoding="utf-8")
        used = Path(tmp) / "stock.txt.used"
        used.write_text("a@outlook.com\n", encoding="utf-8")
        assert outlook_rt.update_inventory_credentials(
            str(inv),
            "a@outlook.com",
            refresh_token="NEW",
            password="PW2",
            client_id="cid",
        )
        assert "a@outlook.com----PW2----cid----NEW" in inv.read_text(encoding="utf-8")
        assert used.read_text(encoding="utf-8") == "a@outlook.com\n"


if __name__ == "__main__":
    test_generate_password_differs_and_has_mix()
    test_fill_password_change_form_named_fields()
    test_parse_microsoft_forms_action()
    test_renew_one_change_password_and_inventory_write()
    test_renew_inventory_skips_used_and_limits()
    test_update_inventory_credentials_keeps_used_file()
    print("OK outlook renew")
