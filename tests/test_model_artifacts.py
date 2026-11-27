"""Regression tests for multi-file models and hardware-specific serving."""
import os
import tempfile
import unittest
from unittest.mock import patch

from model_artifacts import artifact_sets
from model_catalog import ModelManager, VULKAN_GGUF_IMAGE
from catalog_discovery import CatalogDiscovery
from hybrid_memory import memory_plan
from model_views import index


def sibling(name, size, digest="a"):
    return {"rfilename": name, "size": size, "lfs": {"sha256": digest * 64}}


class ArtifactTests(unittest.TestCase):
    def test_hybrid_plan_uses_gpu_and_ram_without_consuming_all_host_ram(self):
        hardware = {"gpu_provider": "nvidia", "vram_mib": 24576,
                    "ram_gib": 128, "disk_free_gib": 200}
        assisted = memory_plan(hardware, 40 * 2**30, "safetensors", True)
        self.assertEqual(assisted["runtime"], "gpu")
        self.assertTrue(assisted["fits"])
        self.assertTrue(assisted["hybrid_used"])
        self.assertGreater(assisted["offload_gb"], 0)
        self.assertEqual(memory_plan(hardware, 40 * 2**30, "safetensors", False)["runtime"], "cpu")
        self.assertFalse(memory_plan(hardware, 140 * 2**30, "safetensors", True)["fits"])

    def test_hybrid_preference_persists_and_vllm_offload_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "MODEL_DIR": directory, "MODEL_HOST_DIR": directory, "MODEL_NETWORK": "test"}):
            manager = ModelManager(directory + "/models.db", "secret")
            self.assertTrue(manager.hybrid_enabled())
            manager.set_hybrid_enabled(False)
            self.assertFalse(ModelManager(directory + "/models.db", "secret").hybrid_enabled())
            manager.set_hybrid_enabled(True)
            gpu = manager.serving_config("hf-safe", "alias", 4096, runtime="gpu",
                                         model_format="safetensors", cpu_offload_gb=20)
            self.assertEqual(gpu["Cmd"][gpu["Cmd"].index("--cpu-offload-gb") + 1], "20")
            no_offload = manager.serving_config("hf-safe", "alias", 4096, runtime="gpu",
                                                model_format="safetensors", cpu_offload_gb=20,
                                                hybrid_enabled=False)
            self.assertNotIn("--cpu-offload-gb", no_offload["Cmd"])
            gguf = manager.serving_config("model.gguf", "alias", 4096, runtime="gpu",
                                          hybrid_enabled=False)
            self.assertIn("--fit", gguf["Cmd"])

    def test_model_page_shows_default_and_apply_action_for_existing_model(self):
        hardware = {"gpu": "NVIDIA 3090", "gpu_provider": "nvidia", "vram_mib": 24576,
                    "ram_gib": 128, "disk_free_gib": 100}
        installed = [{"candidate_id": "discovered-" + "a" * 16, "alias": "alias", "repo": "owner/model",
                      "format": "safetensors", "runtime": "cpu", "hybrid_enabled": 0,
                      "cpu_offload_gb": 0, "size": 40 * 2**30, "sha256": "a" * 64,
                      "context_tokens": 4096, "installed_at": "2026-11-27"}]
        page = index(hardware, [], installed, [], "token")
        self.assertIn('name="enabled" value="1" checked', page)
        self.assertIn('action="/models/hybrid/apply"', page)
        self.assertIn("Apply saved preference", page)
        off_page = index(hardware, [], installed, [], "token", hybrid_enabled=False)
        self.assertNotIn('name="enabled" value="1" checked', off_page)

    def test_installed_model_reconfiguration_saves_only_after_container_creation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "MODEL_DIR": directory, "MODEL_HOST_DIR": directory, "MODEL_NETWORK": "test"}):
            manager = ModelManager(directory + "/models.db", "secret")
            candidate = "discovered-" + "a" * 16
            with manager.connect() as db:
                db.execute("INSERT INTO installed_models(candidate_id,repo,filename,revision,sha256,size,installed_at,alias,container,context_tokens,runtime,format,hybrid_enabled) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (candidate, "owner/model", "hf-model", "a" * 40, "b" * 64, 40 * 2**30,
                            "2026-11-27", "alias", "container", 4096, "cpu", "safetensors", 0))
            hardware = {"gpu_provider": "nvidia", "vram_mib": 24576, "ram_gib": 128, "disk_free_gib": 100}
            with patch.object(manager, "hardware", return_value=hardware), \
                 patch("model_catalog.threading.Thread") as worker:
                job = manager.start_reconfigure_hybrid(candidate)
            args = worker.call_args.kwargs["args"]
            calls = []
            def fake_docker(method, path, body=None):
                calls.append((method, path, body))
                if method == "GET" and path == "/containers/container/json":
                    return {"State": {"Running": False}}
                return {}
            with patch.object(manager, "_ensure_image"), patch("model_catalog.docker_api", side_effect=fake_docker):
                manager._reconfigure_hybrid(*args)
            row = manager.installed()[0]
            self.assertEqual(row["runtime"], "gpu")
            self.assertGreater(row["cpu_offload_gb"], 0)
            self.assertEqual(manager.jobs()[0]["status"], "completed")
            self.assertTrue(any(path.startswith("/containers/create?name=container") for _, path, _ in calls))

    def test_reconfiguration_failure_restores_previous_container_and_db_setting(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "MODEL_DIR": directory, "MODEL_HOST_DIR": directory, "MODEL_NETWORK": "test"}):
            manager = ModelManager(directory + "/models.db", "secret")
            candidate = "discovered-" + "b" * 16
            with manager.connect() as db:
                db.execute("INSERT INTO installed_models(candidate_id,repo,filename,revision,sha256,size,installed_at,alias,container,context_tokens,runtime,format,hybrid_enabled) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (candidate, "owner/model", "hf-model", "a" * 40, "b" * 64, 40 * 2**30,
                            "2026-11-27", "alias", "container", 4096, "cpu", "safetensors", 0))
            hardware = {"gpu_provider": "nvidia", "vram_mib": 24576, "ram_gib": 128, "disk_free_gib": 100}
            with patch.object(manager, "hardware", return_value=hardware), \
                 patch("model_catalog.threading.Thread") as worker:
                manager.start_reconfigure_hybrid(candidate)
            args = worker.call_args.kwargs["args"]
            calls = []
            def fake_docker(method, path, body=None):
                calls.append(path)
                if method == "GET" and path == "/containers/container/json":
                    return {"State": {"Running": False}}
                if method == "POST" and path == "/containers/create?name=container":
                    raise RuntimeError("create failed")
                return {}
            with patch.object(manager, "_ensure_image"), patch("model_catalog.docker_api", side_effect=fake_docker):
                manager._reconfigure_hybrid(*args)
            self.assertEqual(manager.installed()[0]["runtime"], "cpu")
            self.assertEqual(manager.jobs()[0]["status"], "failed")
            self.assertIn("previous setting restored", manager.jobs()[0]["stage"])
            self.assertTrue(any("/rename?name=container" in path for path in calls))

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
