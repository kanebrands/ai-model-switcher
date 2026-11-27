import os
import tempfile
import unittest
from unittest.mock import patch

from context_tuning import ContextTuner, context_flag, context_presets
from model_catalog import ModelManager


class ContextTuningTests(unittest.TestCase):
    def test_context_presets_exclude_current_and_reported_native_excess(self):
        self.assertEqual(context_presets(8192, 32768), [4096, 16384, 32768])
        self.assertIn(262144, context_presets(32768))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MODEL_DIR": self.temp.name,
                                        "MODEL_HOST_DIR": self.temp.name,
                                        "MODEL_NETWORK": "local-ai_ai_internal"})
        self.env.start()
        path = self.temp.name + "/context.sqlite3"
        self.manager = ModelManager(path, "test")
        self.tuner = ContextTuner(path, self.manager, "test")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_reads_llama_and_vllm_context(self):
        self.assertEqual(context_flag(["--ctx-size", "32768"]), ("--ctx-size", 32768))
        self.assertEqual(context_flag(["--max-model-len", "65536"]), ("--max-model-len", 65536))
        with self.assertRaises(ValueError):
            context_flag(["--model", "foo"])

    def test_rejects_invalid_or_unchanged_size_without_restarting(self):
        with self.assertRaises(ValueError):
            self.tuner.start("gemma4", 5000)
        try:
            self.tuner.start("gemma4", 5000)
        except ValueError as exc:
            str(exc).encode("latin-1")
        with patch.object(self.tuner, "active", return_value={"mode": "gemma4", "tokens": 32768}):
            with self.assertRaises(ValueError):
                self.tuner.start("gemma4", 32768)
        self.assertFalse(self.tuner.busy)

    def test_active_reads_current_container_command(self):
        inspect = {"State": {"Running": True}, "Config": {"Cmd": ["--ctx-size", "32768"]}}
        with patch("context_tuning.docker_api", return_value=inspect):
            current = self.tuner.active("gemma4")
        self.assertEqual(current["tokens"], 32768)
        self.assertEqual(current["alias"], "local-gemma4-ablit")

    def test_only_completed_matching_hardware_is_labeled_tuned(self):
        with self.tuner.connect() as db:
            db.execute("INSERT INTO context_jobs(id,mode,previous_tokens,requested_tokens,started_at,ended_at,status,stage,hardware_fingerprint) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", ("job", "gemma4", 16384, 32768,
                       "2026-11-26", "2026-11-26", "completed", "saved", "gpu-a"))
        inspect = {"State": {"Running": True}, "Config": {"Cmd": ["--ctx-size", "32768"]}}
        with patch("context_tuning.docker_api", return_value=inspect):
            same = self.tuner.contexts("gpu-a")
            changed = self.tuner.contexts("gpu-b")
        self.assertEqual(same["gemma4"]["state"], "tuned")
        self.assertEqual(changed["gemma4"]["state"], "configured")

    def test_failed_readiness_rolls_back_previous_context(self):
        current = {"mode": "gemma4", "container": "local-ai-gemma4-ablit",
                   "alias": "local-gemma4-ablit", "tokens": 32768}
        with self.tuner.connect() as db:
            db.execute("INSERT INTO context_jobs(id,mode,previous_tokens,requested_tokens,started_at,status,stage) "
                       "VALUES(?,?,?,?,?,?,?)", ("job", "gemma4", 32768, 65536, "2026-11-26", "running", "queued"))
        with patch("context_tuning.docker_api", return_value={"Id": "old-container"}), \
             patch.object(self.tuner, "_helper") as helper, \
             patch.object(self.tuner, "_wait_ready", side_effect=[False, True]):
            self.tuner._run("job", current, 65536)
        self.assertEqual([call.args[0] for call in helper.call_args_list], ["apply", "rollback"])
        self.assertEqual(self.tuner.status()["job"]["status"], "failed")

    def test_success_commits_only_after_readiness(self):
        current = {"mode": "gemma4", "container": "local-ai-gemma4-ablit",
                   "alias": "local-gemma4-ablit", "tokens": 32768}
        with self.tuner.connect() as db:
            db.execute("INSERT INTO context_jobs(id,mode,previous_tokens,requested_tokens,started_at,status,stage) "
                       "VALUES(?,?,?,?,?,?,?)", ("job", "gemma4", 32768, 65536, "2026-11-26", "running", "queued"))
        calls = []
        with patch("context_tuning.docker_api", return_value={"Id": "old-container"}), \
             patch.object(self.tuner, "_helper", side_effect=lambda action, *args: calls.append(action)), \
             patch.object(self.tuner, "_wait_ready", side_effect=lambda *args: calls.append("ready") or True):
            self.tuner._run("job", current, 65536)
        self.assertEqual(calls, ["apply", "ready", "commit"])
        self.assertEqual(self.tuner.status()["job"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
