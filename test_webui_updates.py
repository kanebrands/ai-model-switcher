"""Checks for accurate Open WebUI update offers and safe update starts."""
import datetime as dt
import tempfile
import unittest
from unittest.mock import patch

from webui_updates import WebUIUpdates, upstream_image_digests


class FakeResponse:
    def __init__(self, payload, digest=""):
        self.payload = payload
        self.headers = {"Docker-Content-Digest": digest}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        import json
        return json.dumps(self.payload).encode()


class WebUIUpdateTests(unittest.TestCase):
    def test_resolves_index_platform_and_config_digests(self):
        image_id = "sha256:" + "c" * 64
        descriptor = "sha256:" + "d" * 64
        index_digest = "sha256:" + "f" * 64
        payloads = [
            {"token": "test-token"},
            {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"}, "digest": descriptor},
                           {"platform": {"os": "linux", "architecture": "arm64"},
                            "digest": "sha256:" + "e" * 64}]},
            {"config": {"digest": image_id}},
        ]
        responses = [FakeResponse(payloads[0]), FakeResponse(payloads[1], index_digest), FakeResponse(payloads[2])]
        with patch("webui_updates.urllib.request.urlopen", side_effect=responses) as open_url:
            self.assertEqual(upstream_image_digests(), {index_digest, descriptor, image_id})
        self.assertEqual(open_url.call_count, 3)

    def test_unchanged_image_is_not_offered_and_new_image_is(self):
        with tempfile.TemporaryDirectory() as directory:
            updates = WebUIUpdates(directory + "/tests.db")
            current = "sha256:" + "a" * 64
            def fake_docker(_method, path, _body=None):
                if path.startswith("/containers/"):
                    return {"Image": current}
                return {"Architecture": "amd64", "Os": "linux"}
            with patch("webui_updates.docker_api", side_effect=fake_docker), \
                 patch("webui_updates.upstream_image_digests", return_value={current}):
                updates._check()
            self.assertFalse(updates.status()["update_available"])
            with patch("webui_updates.docker_api", side_effect=fake_docker), \
                 patch("webui_updates.upstream_image_digests", return_value={"sha256:" + "b" * 64}):
                updates._check()
            self.assertTrue(updates.status()["update_available"])

    def test_running_index_digest_is_already_current(self):
        with tempfile.TemporaryDirectory() as directory:
            updates = WebUIUpdates(directory + "/tests.db")
            index = "sha256:" + "f" * 64
            config = "sha256:" + "c" * 64
            def fake_docker(_method, path, _body=None):
                return {"Image": index} if path.startswith("/containers/") else {"Architecture": "amd64", "Os": "linux"}
            with patch("webui_updates.docker_api", side_effect=fake_docker), \
                 patch("webui_updates.upstream_image_digests", return_value={index, config}):
                updates._check()
            self.assertFalse(updates.status()["update_available"])

    def test_start_requires_fresh_offer_and_unchanged_running_image(self):
        with tempfile.TemporaryDirectory() as directory:
            updates = WebUIUpdates(directory + "/tests.db")
            with self.assertRaisesRegex(ValueError, "No current Open WebUI update"):
                updates.start_update()
            updates.check.update({"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                  "update_available": True, "running_id": "sha256:" + "a" * 64})
            with patch.object(updates, "running_image_id", return_value="sha256:" + "b" * 64):
                with self.assertRaisesRegex(ValueError, "image changed"):
                    updates.start_update()

    def test_dismissal_persists_and_does_not_hide_a_later_result(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = directory + "/tests.db"
            updates = WebUIUpdates(db_path)
            with updates.connect() as db:
                db.execute("INSERT INTO webui_update_jobs(id,started_at,status,stage) VALUES(?,?,?,?)",
                           ("first", "2026-11-26T15:00:00+00:00", "completed", "healthy"))
            self.assertTrue(updates.dismiss_result("first"))
            self.assertIsNotNone(updates.latest_job()["dismissed_at"])
            self.assertIsNotNone(WebUIUpdates(db_path).latest_job()["dismissed_at"])
            with updates.connect() as db:
                db.execute("INSERT INTO webui_update_jobs(id,started_at,status,stage) VALUES(?,?,?,?)",
                           ("second", "2026-11-27T15:00:00+00:00", "completed", "healthy"))
            self.assertFalse(updates.dismiss_result("first"))
            self.assertIsNone(updates.latest_job()["dismissed_at"])
            self.assertTrue(updates.dismiss_result("second"))


if __name__ == "__main__":
    unittest.main()
