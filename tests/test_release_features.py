import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webui.api_projection import PageConflict, paginate, summary
from webui.log_retention import RotatingPipe, prune_closed_logs
from webui.maintenance_gate import MaintenanceGate
from webui.static_assets import compile_assets


class ReleaseFeaturesTests(unittest.TestCase):
    def test_atomic_maintenance_lease(self):
        gate = MaintenanceGate()
        self.assertTrue(gate.begin())
        self.assertFalse(gate.set_paused(True)["drained"])
        self.assertFalse(gate.begin())
        gate.end()
        self.assertTrue(gate.snapshot()["drained"])

    def test_page_revision_rejects_concurrent_change(self):
        first = paginate({"items": [1, 2]})
        with self.assertRaises(PageConflict):
            paginate({"items": [1, 2, 3]}, 2, 1, first["generation"])

    def test_status_parity_is_numeric_and_bounded(self):
        value = summary({"control": {"mode": "orch", "guard_token": "secret"}, "traffic": {"batch_id": "private", "bytes_total": 100, "url": "private"}, "rates": {"1h": {"ok": 3}}, "ended": {"success": 3}, "tail": ["access_token=secretvalue https://user:pass@example.test/x", "ok"]})
        self.assertEqual(value["control"]["mode"], "orch")
        self.assertEqual(value["traffic"]["bytes_total"], 100)
        self.assertEqual(value["tail"][-1], "ok")
        self.assertLessEqual(len(value["tail"]), 40)
        self.assertNotIn("private", json.dumps(value))
        self.assertNotIn("secret", json.dumps(value))
        self.assertLess(len(json.dumps(value)), 20000)

    def test_static_assets_are_content_addressed(self):
        page, assets = compile_assets("<style>body{color:red}</style><script>void 0;</script>", {})
        self.assertNotIn("<style>", page)
        self.assertEqual(len(assets), 2)
        changed, _ = compile_assets("<style>body{color:blue}</style><script>void 0;</script>", {})
        self.assertNotEqual(page, changed)

    def test_pipe_rotation_keeps_child_writing_and_bounds_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.log"
            output = RotatingPipe(path, max_bytes=1024, backups=2)
            process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write('sample\\n' * 1000)"], stdout=output)
            output.close()
            self.assertEqual(process.wait(timeout=5), 0)
            output.thread.join(5)
            self.assertFalse(output.thread.is_alive())
            self.assertIsNone(output.error)
            self.assertLessEqual(sum(item.stat().st_size for item in Path(directory).iterdir()), 3072)
            self.assertTrue(path.read_text().endswith("sample\n"))
            prune_closed_logs(directory, protected=[path], max_bytes=1, max_age=0)
            self.assertTrue(path.exists())
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
