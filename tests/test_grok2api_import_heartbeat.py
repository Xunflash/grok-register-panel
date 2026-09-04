# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grok2api_client import Grok2APIClient  # noqa: E402


def test_iter_sse_events_reports_comments_and_complete():
    comments: list[str] = []
    lines = [
        ": heartbeat",
        "event: progress",
        "data: {\"completed\":1,\"total\":1,\"phase\":\"importing\"}",
        "",
        "event: complete",
        "data: {\"created\":1,\"updated\":0,\"skipped\":0,\"failed\":0,\"synced\":0,\"syncFailed\":0}",
        "",
    ]
    events = list(
        Grok2APIClient._iter_sse_events(lines, on_comment=comments.append)
    )
    assert comments == ["heartbeat"]
    kinds = [event for event, _payload in events]
    assert kinds == ["progress", "complete"]
    complete = events[-1][1]
    assert complete["created"] == 1
    assert complete["synced"] == 0


def test_import_auth_paths_uses_same_http_paths():
    assert Grok2APIClient.IMPORT_PATHS["grok_build"] == "/api/admin/v1/accounts/import"
    assert Grok2APIClient.IMPORT_PATHS["grok_web"] == "/api/admin/v1/accounts/web/import"
    assert Grok2APIClient.IMPORT_PATHS["grok_console"] == "/api/admin/v1/accounts/console/import"


if __name__ == "__main__":
    test_iter_sse_events_reports_comments_and_complete()
    test_import_auth_paths_uses_same_http_paths()
    print("OK grok2api import heartbeat")
