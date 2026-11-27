"""Model downloads keep their order and names without stopping the UI."""
import os
import tempfile
import unittest
from unittest.mock import patch

from media_catalog import MediaManager
from benchmark_views import shell
from model_catalog import ModelManager, model_display_name, preferred_alias


class OperationQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "MODEL_DIR": self.temp.name, "MODEL_HOST_DIR": self.temp.name,
            "MODEL_NETWORK": "local-ai_ai_internal", "MEDIA_MODEL_DIR": self.temp.name,
            "MEDIA_HOST_DIR": self.temp.name,
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_readable_alias_and_existing_alias_unchanged_in_display(self):
        candidate = {"id": "discovered-2bda260643809970", "repo": "ggml-org/Inkling-2-Mini-GGUF",
                     "file": "Inkling-2-Mini-Q4_K_M.gguf"}
        self.assertEqual(preferred_alias(candidate), "local-inkling-2-mini-q4-k-m")
        self.assertTrue(preferred_alias(candidate, {"local-inkling-2-mini-q4-k-m"}).startswith("local-inkling-2-mini-q4-k-m-"))
        self.assertEqual(model_display_name({**candidate, "filename": candidate["file"]}), "Inkling 2 Mini · Q4_K_M")

    def test_operation_page_polls_without_meta_reload(self):
        page = shell("Hugging Face model recommendations", "<p>Work in progress</p>", refresh=True)
        self.assertIn('data-operation-monitor="/operations/status?scope=models"', page)
        self.assertIn('src="/assets/operation_poll.js"', page)
        self.assertNotIn('http-equiv="refresh"', page)

    def test_text_install_requests_are_queued_in_order(self):
        manager = ModelManager(self.temp.name + "/models.db", "key")
        hardware = {"gpu": "NVIDIA", "gpu_provider": "nvidia", "vram_mib": 24576,
                    "ram_gib": 128, "disk_free_gib": 100}
        metadata = {"revision": "a" * 40, "size": 1_000_000_000, "sha256": "b" * 64}
        with patch.object(manager, "hardware", return_value=hardware), \
             patch.object(manager, "_metadata", return_value=metadata), \
             patch("model_catalog.threading.Thread") as worker:
            first = manager.start_install("huihui-qwen35-9b")
            second = manager.start_install("qwen35-9b")
            self.assertEqual(worker.call_count, 1)
            self.assertEqual(manager.jobs()[0]["id"], second)
            self.assertEqual(manager.jobs()[0]["status"], "queued")
            self.assertIn("behind", manager.jobs()[0]["stage"])
            with self.assertRaisesRegex(ValueError, "already queued"):
                manager.start_install("qwen35-9b")
            manager._finish_operation(int(metadata["size"] * 1.2))
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(worker.call_args.kwargs["args"][0], second)
            manager._finish_operation(int(metadata["size"] * 1.2))
            self.assertFalse(manager.busy)
            self.assertEqual(manager.reserved_bytes, 0)
            self.assertNotEqual(first, second)

    def test_media_install_requests_are_queued_in_order(self):
        manager = MediaManager(self.temp.name + "/media.db")
        hardware = {"vram_mib": 24576, "ram_gib": 128}
        def metadata(candidate):
            return [{"destination": "checkpoints/" + candidate["id"] + ".safetensors", "size": 10}]
        with patch.object(manager, "metadata", side_effect=metadata), \
             patch("media_catalog.threading.Thread") as worker:
            first = manager.start_install("sdxl-base", hardware)
            second = manager.start_install("sdxl-turbo", hardware)
            self.assertEqual(worker.call_count, 1)
            self.assertIn("behind", manager.jobs()[0]["stage"])
            with self.assertRaisesRegex(ValueError, "already queued"):
                manager.start_install("sdxl-turbo", hardware)
            self.assertEqual(manager.pending_installs[0][0], second)
            self.assertNotEqual(second, first)


if __name__ == "__main__":
    unittest.main()
