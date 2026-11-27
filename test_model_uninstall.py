"""The uninstall action must remove only inactive, switcher-owned files."""
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import MODELS
from model_catalog import ModelManager
from model_views import uninstall_form


class ModelUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {
            "MODEL_DIR": self.temp.name, "MODEL_HOST_DIR": self.temp.name,
            "MODEL_NETWORK": "local-ai_ai_internal",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = ModelManager(os.path.join(self.temp.name, "models.sqlite3"), "test")
        self.candidate = "test-owned-model"
        self.alias = "local-hf-test-owned-model"
        self.container = "local-ai-hf-test-owned-model"

    def install_row(self, filename="owned.gguf", model_format="gguf"):
        with self.manager.connect() as db:
            manifest = [{"path": filename.split("/", 1)[1]}] if "/" in filename else []
            db.execute("INSERT INTO installed_models(candidate_id,repo,filename,revision,sha256,size,installed_at,alias,container,context_tokens,format,files_json) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (self.candidate, "owner/repo", filename, "a" * 40, "b" * 64, 5,
                        "2026-11-27", self.alias, self.container, 4096, model_format, json.dumps(manifest)))
        MODELS["hf-" + self.candidate] = (self.alias, self.container)
        self.addCleanup(MODELS.pop, "hf-" + self.candidate, None)

    def test_removes_stopped_model_and_keeps_unrelated_files(self):
        self.install_row()
        target = pathlib.Path(self.temp.name, "owned.gguf")
        target.write_bytes(b"model")
        unrelated = pathlib.Path(self.temp.name, "other.gguf")
        unrelated.write_bytes(b"keep")
        inspect = {"State": {"Running": False}, "Config": {"Image": "llama:test"}}
        with patch("model_catalog.docker_api", return_value=inspect) as docker:
            self.assertEqual(self.manager.uninstall(self.candidate, self.alias), self.alias)
        self.assertFalse(target.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.manager.installed(), [])
        self.assertNotIn("hf-" + self.candidate, MODELS)
        self.assertTrue(any(call.args[0] == "DELETE" for call in docker.call_args_list))

    def test_refuses_running_model_without_changes(self):
        self.install_row()
        target = pathlib.Path(self.temp.name, "owned.gguf")
        target.write_bytes(b"model")
        with patch("model_catalog.docker_api", return_value={"State": {"Running": True}}):
            with self.assertRaisesRegex(RuntimeError, "Switch to another workload"):
                self.manager.uninstall(self.candidate, self.alias)
        self.assertTrue(target.exists())
        self.assertEqual(len(self.manager.installed()), 1)

    def test_docker_failure_restores_files_and_record(self):
        self.install_row("hf-test-owned-model/config.json", "safetensors")
        folder = pathlib.Path(self.temp.name, "hf-test-owned-model")
        folder.mkdir()
        (folder / "config.json").write_text("{}")
        def docker(method, path, *args):
            if method == "DELETE":
                raise RuntimeError("Docker unavailable")
            return {"State": {"Running": False}}
        with patch("model_catalog.docker_api", side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, "Docker unavailable"):
                self.manager.uninstall(self.candidate, self.alias)
        self.assertTrue((folder / "config.json").exists())
        self.assertEqual(len(self.manager.installed()), 1)

    def test_extra_file_blocks_directory_removal(self):
        self.install_row("hf-test-owned-model/model-00001-of-00002.gguf", "gguf-sharded")
        folder = pathlib.Path(self.temp.name, "hf-test-owned-model")
        folder.mkdir()
        (folder / "model-00001-of-00002.gguf").write_bytes(b"model")
        (folder / "my-notes.txt").write_text("keep")
        with self.assertRaisesRegex(ValueError, "extra files"):
            self.manager.uninstall(self.candidate, self.alias)
        self.assertTrue((folder / "my-notes.txt").exists())
        self.assertEqual(len(self.manager.installed()), 1)

    def test_confirmation_and_symlink_protect_storage(self):
        self.install_row()
        target = pathlib.Path(self.temp.name, "owned.gguf")
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = pathlib.Path(outside_dir.name, "uninstall-outside-fixture")
        outside.write_bytes(b"keep")
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "confirm"):
            self.manager.uninstall(self.candidate, "wrong")
        with self.assertRaisesRegex(ValueError, "outside managed storage"):
            self.manager.uninstall(self.candidate, self.alias)
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertEqual(len(self.manager.installed()), 1)

    def test_active_model_has_no_uninstall_button(self):
        row = {"candidate_id": self.candidate, "alias": self.alias}
        self.assertNotIn("/models/uninstall", uninstall_form(row, "csrf", {self.candidate}))
        self.assertIn("/models/uninstall", uninstall_form(row, "csrf"))

    def test_rename_inactive_api_id_and_rollback_on_create_failure(self):
        self.install_row()
        inspect = {"State": {"Running": False}, "Config": {"Image": "llama:test"}}
        with patch("model_catalog.docker_api", return_value=inspect) as docker:
            new_alias = self.manager.rename_alias(self.candidate, "local-readable-model")
        self.assertEqual(new_alias, "local-readable-model")
        self.assertEqual(self.manager.installed()[0]["alias"], new_alias)
        self.assertEqual(MODELS["hf-" + self.candidate][0], new_alias)
        self.assertTrue(any(call.args[0] == "DELETE" for call in docker.call_args_list))

        def fail_create(method, path, *args):
            if path.startswith("/containers/create?"):
                raise RuntimeError("create failed")
            return inspect
        with patch("model_catalog.docker_api", side_effect=fail_create) as docker:
            with self.assertRaisesRegex(RuntimeError, "create failed"):
                self.manager.rename_alias(self.candidate, "local-another-model")
        self.assertEqual(self.manager.installed()[0]["alias"], new_alias)
        self.assertTrue(any("/rename?name=" + self.container in call.args[1]
                            for call in docker.call_args_list if call.args[0] == "POST"))


if __name__ == "__main__":
    unittest.main()
