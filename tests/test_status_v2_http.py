import json
from pathlib import Path
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webui import monitor
from webui.status_snapshot import SnapshotWorker


class StatusHttpTests(unittest.TestCase):
    def setUp(self):
        self.worker = SnapshotWorker(lambda: {"process": {"running": False, "cmd": "private"}, "ok": 2, "tail": ["private"], "recent_ok": []})
        self.worker.refresh()
        self.environment = patch.dict("os.environ", {"MONITOR_TOKEN": "fixture-monitor-token"})
        self.environment.start()
        self.snapshot = patch.object(monitor, "STATUS_WORKER", self.worker)
        self.snapshot.start()
        self.server = monitor.ThreadingHTTPServer(("127.0.0.1", 0), monitor.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.snapshot.stop()
        self.environment.stop()

    def fetch(self, path, token=True, tag=None):
        headers = {"Authorization": "Bearer fixture-monitor-token"} if token else {}
        if tag:
            headers["If-None-Match"] = tag
        request = urllib.request.Request(self.base + path, headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as error:
            return error

    def test_auth_precedes_etag_and_summary_is_bounded(self):
        with self.fetch("/api/status/v2") as response:
            self.assertEqual(response.status, 200)
            body = response.read()
            self.assertNotIn(b"private", body)
            self.assertLess(len(body), 20000)
            self.assertEqual(json.loads(body)["schema_version"], 2)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            tag = response.headers["ETag"]
        with self.fetch("/api/status/v2", tag=tag) as response:
            self.assertEqual(response.status, 304)
            self.assertEqual(response.read(), b"")
        with self.fetch("/api/status/v2", token=False, tag=tag) as response:
            self.assertEqual(response.status, 401)
        with self.fetch("/api/health", token=False) as response:
            self.assertEqual(response.status, 200)

    def test_warmup_and_invalid_log_pages(self):
        for page in ("0", "-1", "invalid"):
            with self.fetch("/api/logs?page=" + page) as response:
                self.assertEqual(response.status, 400)
        with self.worker.lock:
            self.worker.value = None
        with self.fetch("/api/status/v2") as response:
            self.assertEqual(response.status, 503)
        with self.fetch("/") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
