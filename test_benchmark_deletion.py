import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import BenchmarkManager


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "BENCHMARK_DB": self.temp.name + "/runs.sqlite3",
            "BENCHMARK_API_KEY": "test",
            "BENCHMARK_COMFY_PASSWORD": "test",
        })
        self.env.start()
        self.manager = BenchmarkManager(lambda _: None)
        with self.manager.connect() as db:
            for key, date, status in (
                ("old", "2026-10-31T23:59:59+00:00", "completed"),
                ("boundary", "2026-11-01T00:00:00+00:00", "completed"),
                ("new", "2026-11-02T00:00:00+00:00", "completed"),
            ):
                db.execute("INSERT INTO runs(id,started_at,mode,model_id,status,stage) VALUES(?,?,?,?,?,?)",
                           (key, date, "qwen4b", "local-qwen4b", status, "finished"))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_delete_one_run(self):
        self.assertEqual(self.manager.delete_run("new"), 1)
        self.assertIsNone(self.manager.get_run("new"))
        self.assertEqual(self.manager.delete_run("new"), 0)

    def test_delete_before_utc_date_excludes_boundary(self):
        self.assertEqual(self.manager.delete_before("2026-11-01T00:00:00+00:00"), 1)
        self.assertIsNone(self.manager.get_run("old"))
        self.assertIsNotNone(self.manager.get_run("boundary"))
        self.assertIsNotNone(self.manager.get_run("new"))

    def test_delete_model_removes_its_completed_runs_only(self):
        with self.manager.connect() as db:
            db.execute("INSERT INTO runs(id,started_at,mode,model_id,status,stage) VALUES(?,?,?,?,?,?)",
                       ("active", "2026-11-03T00:00:00+00:00", "qwen4b", "local-qwen4b", "running", "running"))
            db.execute("INSERT INTO runs(id,started_at,mode,model_id,status,stage) VALUES(?,?,?,?,?,?)",
                       ("other", "2026-11-03T00:00:00+00:00", "other", "other-model", "completed", "finished"))
        self.assertIsNone(self.manager.delete_model("local-qwen4b"))
        self.assertEqual(len(self.manager.model_runs("local-qwen4b")), 4)
        with self.manager.connect() as db:
            db.execute("UPDATE runs SET status='completed' WHERE id='active'")
        self.assertEqual(self.manager.delete_model("local-qwen4b"), 4)
        self.assertEqual(self.manager.model_runs("local-qwen4b"), [])
        self.assertIsNotNone(self.manager.get_run("other"))


if __name__ == "__main__":
    unittest.main()
