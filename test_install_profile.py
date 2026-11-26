"""Installer selections remain explicit and work without an NVIDIA GPU."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import install_profile


ROOT = Path(__file__).parent


class InstallProfileTests(unittest.TestCase):
    def test_cpu_only_menu(self):
        self.assertEqual([item[0] for item in install_profile.eligible(8, 0)], ["cpu-tiny"])
        self.assertEqual([item[0] for item in install_profile.eligible(16, 0)],
                         ["cpu-tiny", "cpu-4b"])

    def test_gpu_models_require_ram_and_vram(self):
        choices = [item[0] for item in install_profile.eligible(128, 24)]
        self.assertIn("huihui", choices)
        self.assertIn("llm", choices)
        self.assertNotIn("flint-iq4", choices)
        self.assertNotIn("media", [item[0] for item in install_profile.eligible(128, 0)])

    def test_multiple_choices_and_invalid_selection(self):
        available = install_profile.eligible(16, 0)
        self.assertEqual(install_profile.select("2,cpu-tiny,2", available), ["cpu-4b", "cpu-tiny"])
        with self.assertRaisesRegex(ValueError, "Unavailable"):
            install_profile.select("qwen4b", available)
        with self.assertRaises(ValueError):
            install_profile.select("", available)

    def test_cli_requires_a_choice_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            env_file = Path(folder) / ".env"
            env_file.write_text("AI_API_KEY=keep-me\n")
            command = ["python3", str(ROOT / "install_profile.py"), str(env_file), "16", "0"]
            no_choice = subprocess.run(command, input="", text=True, capture_output=True, check=False)
            self.assertNotEqual(no_choice.returncode, 0)
            self.assertNotIn("AI_SELECTED_MODELS", env_file.read_text())
            chosen = subprocess.run(command, env={**os.environ, "AI_MODEL_SELECTION": "1,2"},
                                    text=True, capture_output=True, check=False)
            self.assertEqual(chosen.returncode, 0, chosen.stderr)
            self.assertIn("AI_SELECTED_MODELS=cpu-tiny,cpu-4b", env_file.read_text())
            self.assertIn("AI_START_MODEL=cpu-tiny", env_file.read_text())
            self.assertIn("AI_API_KEY=keep-me", env_file.read_text())

    def test_cpu_only_start_uses_only_selected_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("AI_SELECTED_MODELS=cpu-tiny\nAI_START_MODEL=cpu-tiny\n")
            (root / "start-selected.sh").write_bytes((ROOT / "start-selected.sh").read_bytes())
            fake = root / "sudo"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$AI_TEST_COMMANDS\"\n")
            fake.chmod(0o755)
            commands = root / "commands"
            result = subprocess.run(["bash", str(root / "start-selected.sh")],
                                    env={**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"],
                                         "AI_TEST_COMMANDS": str(commands)},
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = commands.read_text().splitlines()
            self.assertIn("docker compose --env-file .env --profile cpu-tiny up -d --no-deps cpu-tiny", lines)
            self.assertNotIn("docker compose --env-file .env --profile coder up -d --no-deps llm", lines)


if __name__ == "__main__":
    unittest.main()
