import os
import tempfile
import unittest
from unittest.mock import patch

from catalog_discovery import CatalogDiscovery, candidate_id, selected_gguf
from engine_updates import EngineUpdates
from model_catalog import ModelManager
from model_views import index


class CatalogRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MODEL_DIR": self.temp.name,
                                        "MODEL_HOST_DIR": self.temp.name,
                                        "MODEL_NETWORK": "local-ai_ai_internal"})
        self.env.start()
        self.manager = ModelManager(self.temp.name + "/catalog.db", "test")
        self.discovery = CatalogDiscovery(self.manager.path, self.manager)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_selects_complete_verified_gguf_not_projector_or_shard(self):
        siblings = [
            {"rfilename": "model-Q4_K_M-00001-of-00002.gguf", "size": 9_000_000_000,
             "lfs": {"sha256": "a" * 64}},
            {"rfilename": "mmproj-Q4_K_M.gguf", "size": 1_000_000_000,
             "lfs": {"sha256": "b" * 64}},
            {"rfilename": "model-Q4_K_M.gguf", "size": 18_000_000_000,
             "lfs": {"sha256": "c" * 64}},
        ]
        self.assertEqual(selected_gguf(siblings)[2], "model-Q4_K_M.gguf")
        self.assertEqual(selected_gguf([{"rfilename": "large-Q4_K_M.gguf", "size": 42_000_000_000,
                                         "lfs": {"sha256": "d" * 64}}])[1], 42_000_000_000)

    def test_search_stores_inkling_with_engine_requirement(self):
        repo = "bartowski/Inkling-2-Mini-GGUF"
        summary = {"id": repo, "createdAt": "2026-11-20T00:00:00Z",
                   "pipeline_tag": "text-generation"}
        info = {"sha": "a" * 40, "siblings": [{"rfilename": "Inkling-2-Mini-Q4_K_M.gguf",
                 "size": 18_660_000_000, "lfs": {"sha256": "b" * 64}}]}
        hardware = {"vram_mib": 24576, "disk_free_gib": 100}
        with patch.object(self.manager, "hardware", return_value=hardware), \
             patch("catalog_discovery.fetch_json", side_effect=lambda url: [summary] if "api/models?" in url else info):
            with self.discovery.connect() as db:
                db.execute("INSERT INTO catalog_refreshes(id,started_at,query,status) VALUES(?,?,?,?)",
                           ("run", "2026-11-26T00:00:00+00:00", "Inkling", "running"))
            self.discovery._refresh("run", "Inkling")
        latest = self.discovery.latest()
        self.assertEqual(latest["status"], "completed")
        self.assertEqual(len(latest["items"]), 1)
        self.assertEqual(latest["items"][0]["min_engine_build"], 12600)
        self.assertEqual(latest["items"][0]["id"], candidate_id(repo, "Inkling-2-Mini-Q4_K_M.gguf"))

    def test_discovered_install_requires_engine_and_ui_disables_button(self):
        candidate = {"id": "discovered-" + "a" * 16, "repo": "bartowski/Inkling-2-Mini-GGUF",
                     "file": "Inkling-Q4_K_M.gguf", "min_engine_build": 12600}
        with self.assertRaisesRegex(ValueError, "requires llama.cpp build"):
            self.manager.start_install(candidate["id"], candidate, 12033)
        hardware = {"gpu": "3090", "vram_mib": 24576, "ram_gib": 125, "disk_free_gib": 100}
        page = index(hardware, [], [], [], "test", discovery={"status": "completed", "query": "Inkling",
                     "items": [{**candidate, "name": "Inkling", "created_at": "2026-11-20",
                                "size_gb": 18.7, "pipeline": "text-generation", "ablit": False,
                                "installed": False}]}, engine={"local_build": 12033})
        self.assertIn("Needs llama.cpp build 12600+", page)
        self.assertIn('action="/models/engine/check"', page)
        self.assertIn('action="/models/refresh"', page)
        self.assertIn('action="/models/hardware/rescan"', page)

    def test_hardware_change_requests_new_discovery(self):
        with self.discovery.connect() as db:
            db.execute("INSERT INTO catalog_refreshes(id,started_at,query,status,hardware_fingerprint) "
                       "VALUES(?,?,?,?,?)", ("old", "2026-11-26T00:00:00+00:00", "", "completed", "old-gpu"))
        hardware = {"fingerprint": "new-gpu", "vram_mib": 32768, "disk_free_gib": 100}
        with patch.object(self.discovery, "start_refresh") as start:
            self.discovery.maybe_refresh(hardware)
        start.assert_called_once_with("", hardware)

    def test_probe_gpu_when_no_model_is_running(self):
        def fake_docker(method, path, body=None):
            if path == "/info":
                return {"MemTotal": 128 * 2**30, "NCPU": 24}
            if method == "POST" and path.startswith("/containers/create?"):
                return {"Id": "probe"}
            if path == "/containers/probe/json":
                return {"State": {"Running": False}}
            if path.startswith("/containers/probe/logs?"):
                return b"logs"
            if method == "GET" and path.startswith("/containers/"):
                return {"State": {"Running": False}}
            return b""
        with patch("model_catalog.docker_api", side_effect=fake_docker), \
             patch("model_catalog.docker_stream_text", return_value="NVIDIA RTX 5090, 32768, GPU-new"):
            first = self.manager.hardware(force=True)
            second = self.manager.hardware()
        self.assertEqual(first["gpu"], "NVIDIA RTX 5090")
        self.assertEqual(first["vram_mib"], 32768)
        self.assertEqual(first["cpu_threads"], 24)
        self.assertEqual(first["fingerprint"], second["fingerprint"])


class EngineCheckTests(unittest.TestCase):
    def test_detects_new_image_by_registry_manifest_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            updates = EngineUpdates(directory + "/engine.db", "test")
            with patch.object(updates, "local_image_id", return_value="sha256:" + "a" * 64), \
                 patch.object(updates, "local_registry_digest", return_value="sha256:" + "a" * 64), \
                 patch("engine_updates.ghcr_tag_digest", return_value="sha256:" + "b" * 64), \
                 patch.object(updates, "local_build", return_value=12033):
                updates._check()
            self.assertTrue(updates.status()["update_available"])
            self.assertEqual(updates.status()["local_build"], 12033)

    def test_current_manifest_is_not_offered_as_an_update(self):
        with tempfile.TemporaryDirectory() as directory:
            updates = EngineUpdates(directory + "/engine.db", "test")
            with patch.object(updates, "local_image_id", return_value="sha256:" + "a" * 64), \
                 patch.object(updates, "local_registry_digest", return_value="sha256:" + "b" * 64), \
                 patch("engine_updates.ghcr_tag_digest", return_value="sha256:" + "b" * 64), \
                 patch.object(updates, "local_build", return_value=12767):
                updates._check()
            self.assertFalse(updates.status()["update_available"])


if __name__ == "__main__":
    unittest.main()
