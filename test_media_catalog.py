import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from media_catalog import MEDIA_CATALOG, MediaManager, estimated_benchmarks, safe_destination
from model_views import index


class MediaCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"MEDIA_MODEL_DIR": self.temp.name,
                                        "MEDIA_HOST_DIR": self.temp.name})
        self.env.start()
        self.manager = MediaManager(self.temp.name + "/jobs.sqlite3")
        self.hardware = {"gpu": "NVIDIA GeForce RTX 3090", "vram_mib": 24576,
                         "ram_gib": 62.7, "disk_free_gib": 100}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_ten_curated_image_video_bundles_and_safe_paths(self):
        self.assertEqual(len(MEDIA_CATALOG), 11)
        self.assertEqual(sum(item["kind"] == "Image" for item in MEDIA_CATALOG), 8)
        self.assertEqual(sum(item["kind"] == "Video" for item in MEDIA_CATALOG), 3)
        for candidate in MEDIA_CATALOG:
            for _, _, destination in candidate["files"]:
                self.assertTrue(safe_destination(destination), destination)
        self.assertFalse(safe_destination("../checkpoints/model.safetensors"))

    def test_hardware_filter_and_installed_indicator(self):
        def metadata(candidate):
            return [{"destination": candidate["files"][0][2], "size": 2, "sha256": hashlib.sha256(b"ok").hexdigest()}]
        installed = next(item for item in MEDIA_CATALOG if item["id"] == "sdxl-base")
        destination = os.path.join(self.temp.name, installed["files"][0][2])
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as stream:
            stream.write(b"ok")
        with patch.object(self.manager, "metadata", side_effect=metadata):
            items = self.manager.recommendations(self.hardware)
            plain = self.manager.recommendations(self.hardware, prefer_ablit=False)
            smaller = self.manager.recommendations({**self.hardware, "vram_mib": 8192})
        self.assertEqual(len(items), 10)
        self.assertIn("z-image-turbo-ablit", {item["id"] for item in items})
        self.assertNotIn("z-image-turbo", {item["id"] for item in items})
        self.assertIn("z-image-turbo", {item["id"] for item in plain})
        self.assertNotIn("z-image-turbo-ablit", {item["id"] for item in plain})
        self.assertTrue(next(item for item in items if item["id"] == "sdxl-base")["installed"])
        self.assertNotIn("wan22-5b", {item["id"] for item in smaller})
        page = index(self.hardware, [], [], [], "token", media_suggestions=items, comfy_url="http://host:8188")
        self.assertIn("Image and video models for ComfyUI", page)
        self.assertIn("30s 720p video", page)
        self.assertIn("30s 1080p video", page)
        self.assertIn("~30–90 min; 6 clips", page)
        self.assertIn("Z-Image Turbo + abliterated Qwen encoder", page)
        self.assertIn("disabled>Installed</button>", page)
        self.assertIn("/models/media/install", page)
        self.assertIn("Open ComfyUI", page)

    def test_estimates_are_not_presented_as_measured_for_other_hardware(self):
        candidate = next(item for item in MEDIA_CATALOG if item["id"] == "wan22-5b")
        self.assertEqual(estimated_benchmarks(candidate, self.hardware)[2], "~30–90 min; 6 clips")
        self.assertEqual(estimated_benchmarks(candidate, {**self.hardware, "gpu": "Unknown GPU"}),
                         ("Uncalibrated",) * 4)

    def test_install_verifies_and_places_file_in_comfyui_folder(self):
        data = b"ok"
        item = {"repo": "test/repo", "source": "weights.safetensors",
                "destination": "checkpoints/weights.safetensors", "revision": "a" * 40,
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        with self.manager.connect() as db:
            db.execute("INSERT INTO media_jobs(id,candidate_id,started_at,status,stage) VALUES(?,?,?,?,?)",
                       ("test-job", "sdxl-base", "2026-11-08", "queued", "queued"))

        def docker(method, path, body=None):
            if path.startswith("/containers/create"):
                self.assertIn("/models/.media_downloads/test--repo", body["Cmd"])
                return {"Id": "download"}
            if path == "/containers/download/start":
                stage = os.path.join(self.temp.name, ".media_downloads/test--repo/weights.safetensors")
                os.makedirs(os.path.dirname(stage), exist_ok=True)
                with open(stage, "wb") as stream:
                    stream.write(data)
            if path == "/containers/download/json":
                return {"State": {"Running": False, "ExitCode": 0}}
            return None

        with patch("media_catalog.docker_api", side_effect=docker):
            self.manager._install("test-job", [item])
        with open(os.path.join(self.temp.name, item["destination"]), "rb") as stream:
            self.assertEqual(stream.read(), data)
        self.assertEqual(self.manager.jobs()[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
