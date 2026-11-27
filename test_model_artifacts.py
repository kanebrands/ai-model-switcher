"""Regression tests for multi-file models and hardware-specific serving."""
import os
import tempfile
import unittest
from unittest.mock import patch

from model_artifacts import artifact_sets
from model_catalog import ModelManager, VULKAN_GGUF_IMAGE
from catalog_discovery import CatalogDiscovery


def sibling(name, size, digest="a"):
    return {"rfilename": name, "size": size, "lfs": {"sha256": digest * 64}}


class ArtifactTests(unittest.TestCase):
    def test_complete_gguf_shards_are_one_variant(self):
        files = [sibling("Q4/model-00002-of-00002.gguf", 200_000_000, "b"),
                 sibling("Q4/model-00001-of-00002.gguf", 200_000_000),
                 sibling("Q8/model-00001-of-00002.gguf", 200_000_000),
                 sibling("mmproj.gguf", 200_000_000)]
        variants = artifact_sets(files)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["format"], "gguf-sharded")
        self.assertEqual(variants[0]["file"], "Q4/model-00001-of-00002.gguf")
        self.assertEqual(variants[0]["size"], 400_000_000)

    def test_safetensors_requires_complete_weights_config_and_tokenizer(self):
        files = [sibling("model-00001-of-00002.safetensors", 200_000_000),
                 sibling("model-00002-of-00002.safetensors", 200_000_000, "b"),
                 sibling("config.json", 1000), sibling("tokenizer.json", 1000)]
        config = {"architectures": ["Qwen2ForCausalLM"],
                  "quantization_config": {"quant_method": "awq"}}
        variants = artifact_sets(files, config)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]["quantization"], "awq")
        self.assertEqual(len(variants[0]["files"]), 4)
        self.assertEqual(artifact_sets(files[:1] + files[2:], config), [])
        self.assertEqual(artifact_sets(files, {"quantization_config": {"quant_method": "unknown"}}), [])

    def test_unsafe_paths_are_excluded(self):
        self.assertEqual(artifact_sets([sibling("../model.gguf", 200_000_000),
                                        sibling("/absolute.gguf", 200_000_000)]), [])

    def test_serving_uses_vulkan_and_vllm_without_nvidia_request(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "MODEL_DIR": directory, "MODEL_HOST_DIR": directory, "MODEL_NETWORK": "test"}):
            manager = ModelManager(directory + "/models.db", "secret")
            hardware = {"gpu_provider": "amd", "render_devices": ["/dev/dri/renderD128"]}
            gguf = manager.serving_config("hf-shards/model-00001-of-00002.gguf", "alias", 8192,
                                          runtime="vulkan", model_format="gguf-sharded", hardware=hardware)
            self.assertEqual(gguf["Image"], VULKAN_GGUF_IMAGE)
            self.assertEqual(gguf["HostConfig"]["Devices"][0]["PathOnHost"], "/dev/dri/renderD128")
            self.assertNotIn("DeviceRequests", gguf["HostConfig"])
            safe = manager.serving_config("hf-safe", "alias", 8192, runtime="cpu", model_format="safetensors")
            self.assertTrue(safe["Image"].startswith("vllm/vllm-openai-cpu:latest-"))
            self.assertIn("--max-model-len", safe["Cmd"])
            self.assertIn("--max-num-seqs", safe["Cmd"])
            self.assertNotIn("DeviceRequests", safe["HostConfig"])

    def test_search_lists_sharded_and_safetensors_and_allows_memory_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "MODEL_DIR": directory, "MODEL_HOST_DIR": directory, "MODEL_NETWORK": "test"}):
            manager = ModelManager(directory + "/models.db", "secret")
            search = CatalogDiscovery(manager.path, manager)
            repo = "owner/example"
            info = {"sha": "a" * 40, "pipeline_tag": "text-generation", "config": {"architectures": ["Qwen2ForCausalLM"]},
                    "siblings": [sibling("Q4/model-00001-of-00002.gguf", 2_000_000_000),
                                 sibling("Q4/model-00002-of-00002.gguf", 2_000_000_000, "b"),
                                 sibling("model.safetensors", 6_000_000_000, "c"),
                                 sibling("config.json", 1000), sibling("tokenizer.json", 1000)]}
            hardware = {"gpu_provider": None, "vram_mib": None, "ram_gib": 8, "disk_free_gib": 100}
            with patch.object(manager, "hardware", return_value=hardware), \
                 patch("catalog_discovery.fetch_json", return_value=info):
                with search.connect() as db:
                    db.execute("INSERT INTO model_searches(id,started_at,query,status) VALUES(?,?,?,?)",
                               ("scan", "2026-11-27T00:00:00+00:00", repo, "running"))
                search._search("scan", repo)
            items = search.latest_search()["items"]
            self.assertEqual({item["format"] for item in items}, {"gguf-sharded", "safetensors"})
            self.assertTrue(all(search.candidate(item["id"]) for item in items))
            safe = next(item for item in items if item["format"] == "safetensors")
            with patch.object(manager, "hardware", return_value=hardware), \
                 patch.object(manager, "_metadata", return_value={key: safe[key] for key in
                                                                ("revision", "size", "sha256", "format", "files", "quantization")}), \
                 patch("model_catalog.threading.Thread"):
                with self.assertRaisesRegex(ValueError, "Try anyway"):
                    manager.start_install(safe["id"], safe)
                self.assertTrue(manager.start_install(safe["id"], safe, allow_unfit=True))

    def test_amd_and_intel_select_vulkan_for_gguf_and_cpu_for_safetensors(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "MODEL_DIR": directory, "MODEL_HOST_DIR": directory, "MODEL_NETWORK": "test"}):
            manager = ModelManager(directory + "/models.db", "secret")
            search = CatalogDiscovery(manager.path, manager)
            repo = "owner/example"
            info = {"sha": "a" * 40, "pipeline_tag": "text-generation", "config": {},
                    "siblings": [sibling("model.gguf", 200_000_000),
                                 sibling("model.safetensors", 200_000_000, "b"),
                                 sibling("config.json", 1000), sibling("tokenizer.json", 1000)]}
            for provider in ("amd", "intel"):
                hardware = {"gpu_provider": provider, "render_devices": ["/dev/dri/renderD128"],
                            "vram_mib": None, "ram_gib": 32, "disk_free_gib": 100}
                with patch.object(manager, "hardware", return_value=hardware), \
                     patch("catalog_discovery.fetch_json", return_value=info):
                    with search.connect() as db:
                        db.execute("INSERT INTO model_searches(id,started_at,query,status) VALUES(?,?,?,?)",
                                   (provider, "2026-11-27T00:00:00+00:00", repo, "running"))
                    search._search(provider, repo)
                with search.connect() as db:
                    row = db.execute("SELECT items_json FROM model_searches WHERE id=?", (provider,)).fetchone()
                import json
                items = json.loads(row[0])
                self.assertEqual({item["format"]: item["runtime"] for item in items},
                                 {"gguf": "vulkan", "safetensors": "cpu"})


if __name__ == "__main__":
    unittest.main()
