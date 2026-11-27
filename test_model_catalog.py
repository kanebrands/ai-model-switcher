import os
import tempfile
import unittest
from unittest.mock import patch

from model_catalog import ModelManager, BY_ID, context_for_hardware
from model_views import index


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "MODEL_DIR": self.temp.name,
            "MODEL_HOST_DIR": self.temp.name,
            "MODEL_NETWORK": "local-ai_ai_internal",
        })
        self.env.start()
        self.manager = ModelManager(self.temp.name + "/models.sqlite3", "test")

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_top_ten_fit_and_abliterated_preference(self):
        hardware = {"gpu": "NVIDIA GeForce RTX 3090", "vram_mib": 24576,
                    "ram_gib": 62.7, "disk_free_gib": 100}
        metadata = {"revision": "a" * 40, "size": 8_000_000_000, "sha256": "b" * 64}
        with patch.object(self.manager, "hardware", return_value=hardware), \
             patch.object(self.manager, "_metadata", return_value=metadata), \
             patch.object(self.manager, "_baseline_rates", return_value=[(9, False, 90), (3, True, 150)]):
            _, preferred = self.manager.recommendations(True)
            _, neutral = self.manager.recommendations(False)
        self.assertEqual(len(preferred), 10)
        self.assertTrue(preferred[0]["ablit"])
        self.assertNotEqual(preferred[1]["id"], neutral[1]["id"])
        self.assertIn("–", preferred[0]["estimated_tps"])

    def test_gpu_fit_excludes_large_weights(self):
        hardware = {"gpu": "NVIDIA GeForce RTX 3090", "vram_mib": 24576,
                    "ram_gib": 62.7, "disk_free_gib": 100}
        metadata = {"revision": "a" * 40, "size": 23_000_000_000, "sha256": "b" * 64}
        with patch.object(self.manager, "hardware", return_value=hardware), \
             patch.object(self.manager, "_metadata", return_value=metadata), \
             patch.object(self.manager, "_baseline_rates", return_value=[]):
            _, suggestions = self.manager.recommendations()
        self.assertEqual(suggestions, [])

    def test_cpu_search_requires_explicit_memory_override(self):
        candidate = {"id": "discovered-" + "a" * 16, "repo": "other/large-GGUF",
                     "file": "large-Q8_0.gguf", "runtime": "cpu"}
        hardware = {"vram_mib": None, "ram_gib": 8, "disk_free_gib": 100}
        metadata = {"revision": "a" * 40, "size": 6_000_000_000, "sha256": "b" * 64}
        with patch.object(self.manager, "hardware", return_value=hardware), \
             patch.object(self.manager, "_metadata", return_value=metadata):
            with self.assertRaisesRegex(ValueError, "Try anyway"):
                self.manager.start_install(candidate["id"], candidate)
            with patch("model_catalog.threading.Thread") as worker:
                job = self.manager.start_install(candidate["id"], candidate, allow_unfit=True)
        self.assertTrue(job)
        self.assertTrue(worker.called)
        config = self.manager.serving_config("large.gguf", "local-large", 4096, runtime="cpu")
        self.assertNotIn("DeviceRequests", config["HostConfig"])
        self.assertNotIn("--n-gpu-layers", config["Cmd"])

    def test_memory_override_does_not_bypass_disk_check(self):
        candidate = {"id": "discovered-" + "b" * 16, "repo": "other/large-GGUF",
                     "file": "large-Q8_0.gguf", "runtime": "cpu"}
        with patch.object(self.manager, "hardware", return_value={"vram_mib": None, "ram_gib": 8, "disk_free_gib": 1}), \
             patch.object(self.manager, "_metadata", return_value={"revision": "a" * 40,
                                                                  "size": 6_000_000_000, "sha256": "b" * 64}):
            with self.assertRaisesRegex(ValueError, "Insufficient model storage"):
                self.manager.start_install(candidate["id"], candidate, allow_unfit=True)

    def test_installed_models_leave_room_for_available_recommendations(self):
        from model_catalog import CATALOG
        hardware = {"gpu": "NVIDIA GeForce RTX 3090", "vram_mib": 24576,
                    "ram_gib": 62.7, "disk_free_gib": 100}
        metadata = {"revision": "a" * 40, "size": 8_000_000_000, "sha256": "b" * 64}
        with self.manager.connect() as db:
            for candidate in CATALOG[:10]:
                db.execute("INSERT INTO installed_models(candidate_id,repo,filename,revision,sha256,size,installed_at,alias,container,context_tokens) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (candidate["id"], candidate["repo"], candidate["file"], "a" * 40,
                            "b" * 64, 8_000_000_000, "2026-11-26", "test", "test", 32768))
        with patch.object(self.manager, "hardware", return_value=hardware), \
             patch.object(self.manager, "_metadata", return_value=metadata), \
             patch.object(self.manager, "_baseline_rates", return_value=[]):
            _, suggestions = self.manager.recommendations()
        self.assertEqual(len(suggestions), 10)
        self.assertEqual(sum(not item["installed"] for item in suggestions), 2)

    def test_hardware_context_target_and_installed_context(self):
        candidate = BY_ID["huihui-qwen35-9b"]
        self.assertEqual(context_for_hardware(candidate, 5_627_045_248, 24576), 65536)
        self.assertEqual(context_for_hardware(candidate, 21_000_000_000, 24576), 16384)
        hardware = {"gpu": "NVIDIA GeForce RTX 3090", "vram_mib": 24576,
                    "ram_gib": 62.7, "disk_free_gib": 100}
        metadata = {"revision": "a" * 40, "size": 5_627_045_248, "sha256": "b" * 64}
        with self.manager.connect() as db:
            db.execute("INSERT INTO installed_models(candidate_id,repo,filename,revision,sha256,size,installed_at,alias,container,context_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (candidate["id"], candidate["repo"], candidate["file"], metadata["revision"], metadata["sha256"], metadata["size"], "2026-11-07", "test", "test", 32768))
        with patch.object(self.manager, "hardware", return_value=hardware), \
             patch.object(self.manager, "_metadata", return_value=metadata), \
             patch.object(self.manager, "_baseline_rates", return_value=[]):
            _, suggestions = self.manager.recommendations()
        installed = next(item for item in suggestions if item["id"] == candidate["id"])
        self.assertTrue(installed["installed"])
        self.assertEqual(installed["context_tokens"], 32768)
        page = index(hardware, suggestions, self.manager.installed(), [], "token")
        self.assertIn("Context on this hardware", page)
        self.assertIn("32,768 tokens (configured)", page)
        self.assertIn("<button type=\"submit\" disabled>Installed</button>", page)


if __name__ == "__main__":
    unittest.main()
