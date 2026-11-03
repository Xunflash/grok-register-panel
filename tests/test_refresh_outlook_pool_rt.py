"""The pool refresh script rotates an IMAP token without printing it."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.refresh_outlook_pool_rt import (  # noqa: E402
    IMAP_SCOPE,
    exchange,
    public_report,
)


class _Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def test_imap_channel_rotates_after_graph_scope_refusal():
    old = "M.old-refresh-token-value"
    new = "M.new-refresh-token-value"
    calls = []

    def post(url, data, timeout, proxies=None):
        calls.append((url, data.get("scope", ""), timeout, proxies))
        if "IMAP.AccessAsUser.All" in data.get("scope", ""):
            return _Response(200, {
                "access_token": "access-token-value",
                "refresh_token": new,
                "expires_in": 3599,
                "scope": IMAP_SCOPE,
            })
        return _Response(400, {"error": "invalid_grant", "error_description": "AADSTS70000"})

    result = exchange("client-id", old, post=post)
    assert result["ok"] is True
    assert result["channel"] == "imap"
    assert result["rotated"] is True
    assert result["new_refresh_token"] == new
    assert any("IMAP.AccessAsUser.All" in scope for _url, scope, _timeout, _proxies in calls)
    report = public_report("person@example.test", len(old), result, True)
    encoded = json.dumps(report)
    assert old not in encoded
    assert new not in encoded
    assert report["saved"] is True
    assert report["expires_in"] == 3599


def test_include_used_keeps_marked_rows_and_skips_them_by_default(tmp_path=None):
    import tempfile

    from scripts.refresh_outlook_pool_rt import pick_accounts

    with tempfile.TemporaryDirectory() as tmp:
        inventory = Path(tmp) / "pool.txt"
        used = Path(tmp) / "pool.txt.used"
        inventory.write_text(
            "fresh@example.test----pw----cid----rt-fresh\n"
            "spent@example.test----pw----cid----rt-spent\n",
            encoding="utf-8",
        )
        used.write_text("spent@example.test----send_code_switch\n", encoding="utf-8")
        unused = pick_accounts(str(inventory), str(used), [], 10)
        both = pick_accounts(str(inventory), str(used), [], 10, include_used=True)
    assert [item["email"] for item in unused] == ["fresh@example.test"]
    assert {item["email"] for item in both} == {"fresh@example.test", "spent@example.test"}


if __name__ == "__main__":
    test_imap_channel_rotates_after_graph_scope_refusal()
    test_include_used_keeps_marked_rows_and_skips_them_by_default()
    print("OK refresh outlook pool rt")
