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

    def post(url, data, timeout):
        calls.append((url, data.get("scope", ""), timeout))
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
    assert any("IMAP.AccessAsUser.All" in scope for _url, scope, _timeout in calls)
    report = public_report("person@example.test", len(old), result, True)
    encoded = json.dumps(report)
    assert old not in encoded
    assert new not in encoded
    assert report["saved"] is True
    assert report["expires_in"] == 3599


if __name__ == "__main__":
    test_imap_channel_rotates_after_graph_scope_refusal()
    print("OK refresh outlook pool rt")
