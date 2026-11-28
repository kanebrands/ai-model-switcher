"""Queue recovery, ownership, and hardware-based task recipes."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import os

from multimodal_jobs import JobStore, CPU_KINDS, GPU_KINDS
from multimodal_profiles import recommend, save, load
with patch.dict(os.environ, {"BENCHMARK_API_KEY": "multimodal-test-key"}):
    from fairness_gateway import is_media_route


class MultimodalTests(unittest.TestCase):
    def test_durable_queue_owner_scope_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "jobs.sqlite3"
            artifacts = Path(folder) / "artifacts"
            store = JobStore(db, artifacts)
            first = store.submit("key-a", "pdf", {"ocr": True}, b"%PDF-example")
            second = store.submit("key-b", "speech", {"input": "hi"})
            self.assertEqual(store.get(first["id"], "key-b"), None)
            self.assertEqual(store.input(first), b"%PDF-example")
            self.assertEqual([job["id"] for job in store.list("key-a")], [first["id"]])
            claimed = store.claim(CPU_KINDS)
            self.assertEqual(claimed["id"], first["id"])
            self.assertEqual(store.claim(GPU_KINDS), None)
            restarted = JobStore(db, artifacts)
            self.assertEqual(restarted.get(first["id"])["status"], "queued")
            claimed = restarted.claim(CPU_KINDS)
            self.assertEqual(claimed["attempts"], 2)
            restarted.complete(first["id"], {"text": "hello"})
            self.assertEqual(restarted.get(first["id"], "key-a")["result"], {"text": "hello"})
            self.assertFalse((artifacts / (first["id"] + ".input")).exists())
            self.assertEqual(restarted.get(second["id"], "key-a"), None)

    def test_artifact_and_invalid_id(self):
        with tempfile.TemporaryDirectory() as folder:
            store = JobStore(Path(folder) / "jobs.sqlite3", Path(folder) / "artifacts")
            job = store.submit("owner", "speech", {"input": "hi"})
            store.complete(job["id"], {"voice": "en"}, b"RIFFdata", "audio/wav")
            self.assertEqual(store.artifact(store.get(job["id"], "owner")), b"RIFFdata")
            with self.assertRaises(ValueError):
                store.get("../etc/passwd")

    def test_profile_recommendations_follow_detected_hardware(self):
        cpu = recommend({"ram_gib": 16, "vram_mib": 0}, ["cpu-4b", "cpu-tiny"], False, True)
        self.assertEqual(cpu[0]["text_mode"], "cpu-4b")
        self.assertFalse(next(row for row in cpu if row["id"] == "creative")["available"])
        gpu = recommend({"ram_gib": 128, "vram_mib": 24576}, ["qwen4b", "cpu-4b"], True, True,
                        ["sd_xl_base_1.0.safetensors"])
        creative = next(row for row in gpu if row["id"] == "creative")
        self.assertTrue(creative["available"])
        self.assertEqual(creative["text_mode"], "qwen4b")
        self.assertEqual(creative["image_checkpoint"], "sd_xl_base_1.0.safetensors")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "active.json"
            save(target, creative)
            self.assertEqual(load(target)["id"], "creative")

    def test_gateway_only_routes_known_task_paths(self):
        self.assertTrue(is_media_route("/v1/documents/parse", "POST"))
        self.assertTrue(is_media_route("/v1/jobs/123/result", "GET"))
        self.assertFalse(is_media_route("/v1/chat/completions", "POST"))
        self.assertFalse(is_media_route("/v1/jobs/123", "POST"))


if __name__ == "__main__":
    unittest.main()
