import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webui.api_projection import etag, paginate, summary
from webui.log_index import LogIndex, warning_event
from webui.status_snapshot import SnapshotWorker


class SnapshotTests(unittest.TestCase):
    def test_summary_is_bounded_and_does_not_forward_unknown_fields(self):
        result = summary({"ok": 1, "error": "secret", "tail": ["secret"], "recent_ok": ["secret"], "process": {"running": True, "cmd": "secret"}, "control": {"token": "secret"}, "unknown": {"email": "person@example.test"}})
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("email", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 20000)
        self.assertTrue(result["process"]["running"])

    def test_etag_stable_and_changes_with_state(self):
        self.assertEqual(etag({"ok": 1, "ts": 1}), etag({"ok": 1, "ts": 2}))
        self.assertNotEqual(etag({"ok": 1}), etag({"ok": 2}))

    def test_pagination_is_bounded_and_handles_empty_page(self):
        self.assertEqual(paginate({"items": []})["items"], [])
        self.assertEqual(len(paginate({"items": list(range(100))}, 1, 100)["items"]), 50)
        self.assertEqual(paginate({"items": [1]}, 2)["items"], [])
        for page in (0, -1, "invalid"):
            with self.assertRaises(ValueError):
                paginate({"items": []}, page)

    def test_unscored_failures_are_warnings_and_scored_failures_stay_errors(self):
        scored = "[-] 注册失败 [其它]: castle SDK empty"
        self.assertIsNone(warning_event(scored))
        warning = warning_event("[!] existingEmail=person@example.test, retry another mailbox")
        self.assertEqual(warning["kind"], "已注册邮箱")
        self.assertNotIn("person@example.test", warning["msg"])
        self.assertEqual(warning_event("[!] 协议收件前仍失败（未用邮箱/打码，不计失败）: tunnel")["kind"], "不计失败")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.log"
            path.write_text(
                "[batch] count=1 workers=1\n"
                "[-] 注册失败 [其它]: castle SDK empty\n"
                "[!] existingEmail=person@example.test, retry another mailbox\n"
                "[!] 资源可重用，同槽位重试 1/1: send-code unavailable\n"
            )
            parsed = LogIndex().read(path)
            self.assertEqual(parsed["fail"], 1)
            self.assertEqual(parsed["recent_fail"][0]["kind"], "其它")
            self.assertEqual([row["kind"] for row in parsed["recent_warn"]], ["可重用重试", "已注册邮箱"])

    def test_traffic_summary_keeps_totals_for_the_homepage_card(self):
        result = summary({
            "traffic": {"batch_id": "batch-1", "bytes_up": 10, "bytes_down": 20, "bytes_total": 30, "running": False},
            "traffic_summary": {
                "batch_count": 2,
                "total_bytes": 90,
                "includes_current": True,
                "bytes_per_batch": 45,
                "bytes_per_success": 30,
                "successful_accounts": 3,
            },
        })
        self.assertTrue(result["traffic"]["batch_id"])
        self.assertEqual(result["traffic_summary"]["total_bytes"], 90)
        self.assertTrue(result["traffic_summary"]["includes_current"])
        self.assertEqual(result["traffic_summary"]["bytes_per_batch"], 45)

    def test_incremental_append_partial_line_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.log"
            path.write_text("[batch] count=3 workers=1\n[+] 注册成功: person@example.test\n")
            index = LogIndex()
            self.assertEqual(index.read(path)["ok"], 1)
            offset = index.offset
            self.assertEqual(index.read(path)["ok"], 1)
            self.assertEqual(index.offset, offset)
            with path.open("a") as destination:
                destination.write("[+] 注册成功: another@example.test")
            self.assertEqual(index.read(path)["ok"], 1)
            with path.open("a") as destination:
                destination.write("\n")
            self.assertEqual(index.read(path)["ok"], 2)
            path.rename(path.with_suffix(".old"))
            path.write_text("[batch] count=1 workers=1\n")
            self.assertEqual(index.read(path)["ok"], 0)
            path.write_text("")
            self.assertEqual(index.read(path)["ok"], 0)

    def test_reads_never_call_producer_and_errors_keep_last_value(self):
        calls = []
        def produce():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("secret")
            return {"ok": 1}
        worker = SnapshotWorker(produce)
        worker.refresh()
        for _ in range(100):
            self.assertEqual(worker.read(), ({"ok": 1}, False))
        self.assertEqual(len(calls), 1)
        worker.refresh()
        self.assertEqual(worker.read(), ({"ok": 1}, True))

    def test_concurrent_refresh_is_singleflight(self):
        entered, done = threading.Event(), threading.Event()
        calls = []
        def produce():
            calls.append(1)
            entered.set()
            done.wait(2)
            return {}
        worker = SnapshotWorker(produce)
        thread = threading.Thread(target=worker.refresh)
        thread.start()
        self.assertTrue(entered.wait(2))
        worker.refresh()
        done.set()
        thread.join(2)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
