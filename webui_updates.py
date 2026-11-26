"""Detect and apply Open WebUI image updates without changing model workloads."""
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.request
import uuid

from benchmarks import docker_api, docker_stream_text


IMAGE = "ghcr.io/open-webui/open-webui:main"
CONTAINER = "local-ai-open-webui-1"
HELPER_IMAGE = "docker:cli"
CHECK_INTERVAL = dt.timedelta(hours=6)
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
ACCEPT = ("application/vnd.oci.image.index.v1+json, "
          "application/vnd.docker.distribution.manifest.list.v2+json, "
          "application/vnd.oci.image.manifest.v1+json, "
          "application/vnd.docker.distribution.manifest.v2+json")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def upstream_image_digests(architecture="amd64", os_name="linux"):
    """Return all registry identities Docker may use for this platform's image."""
    repository = "open-webui/open-webui"
    token_url = "https://ghcr.io/token?scope=repository:" + repository + ":pull&service=ghcr.io"
    with urllib.request.urlopen(token_url, timeout=20) as response:
        token = json.load(response)["token"]

    def manifest(reference):
        request = urllib.request.Request(
            "https://ghcr.io/v2/" + repository + "/manifests/" + reference,
            headers={"Authorization": "Bearer " + token, "Accept": ACCEPT,
                     "User-Agent": "local-ai-model-switcher/2"})
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response), response.headers.get("Docker-Content-Digest", "")

    tagged, tag_digest = manifest("main")
    digests = {tag_digest} if SHA256.fullmatch(tag_digest) else set()
    if "manifests" in tagged:
        matches = [item for item in tagged["manifests"]
                   if item.get("platform", {}).get("os") == os_name
                   and item.get("platform", {}).get("architecture") == architecture]
        if len(matches) != 1 or not SHA256.fullmatch(matches[0].get("digest", "")):
            raise ValueError("GHCR did not provide one matching Open WebUI platform image")
        digests.add(matches[0]["digest"])
        tagged, _ = manifest(matches[0]["digest"])
    image_id = tagged.get("config", {}).get("digest", "")
    if not SHA256.fullmatch(image_id):
        raise ValueError("GHCR did not provide an Open WebUI image config digest")
    digests.add(image_id)
    return digests


class WebUIUpdates:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.checking = False
        self.busy = False
        self.check = {"checked_at": None, "running_id": None, "upstream_id": None,
                      "update_available": None, "error": None}
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS webui_update_jobs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, stage TEXT NOT NULL, old_image TEXT,
                new_image TEXT, backup_path TEXT, error TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS webui_update_dismissals (
                job_id TEXT PRIMARY KEY, dismissed_at TEXT NOT NULL)""")
            db.execute("UPDATE webui_update_jobs SET status='interrupted', ended_at=?, stage='controller restarted' WHERE status='running'",
                       (utc_now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _job(self, job_id, **values):
        with self.connect() as db:
            db.execute("UPDATE webui_update_jobs SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))

    def latest_job(self):
        with self.connect() as db:
            row = db.execute("""SELECT jobs.*, dismissals.dismissed_at FROM webui_update_jobs AS jobs
                                LEFT JOIN webui_update_dismissals AS dismissals ON dismissals.job_id=jobs.id
                                ORDER BY jobs.started_at DESC, jobs.rowid DESC LIMIT 1""").fetchone()
            return dict(row) if row else None

    def dismiss_result(self, job_id):
        """Persist dismissal of the latest finished update result only."""
        with self.connect() as db:
            row = db.execute("SELECT id, status FROM webui_update_jobs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
            if not row or row["id"] != job_id or row["status"] not in ("completed", "failed", "interrupted"):
                return False
            db.execute("INSERT OR IGNORE INTO webui_update_dismissals(job_id,dismissed_at) VALUES(?,?)",
                       (job_id, utc_now()))
            return True

    def running_image_id(self):
        image_id = docker_api("GET", "/containers/" + CONTAINER + "/json")["Image"]
        if not SHA256.fullmatch(image_id):
            raise ValueError("Open WebUI container has no valid image ID")
        return image_id

    def status(self):
        with self.lock:
            result = {**self.check, "checking": self.checking, "busy": self.busy}
        result["job"] = self.latest_job()
        return result

    def check_async(self, force=False):
        with self.lock:
            if self.checking or self.busy:
                return False
            checked = self.check["checked_at"]
            if not force and checked:
                try:
                    if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(checked) < CHECK_INTERVAL:
                        return False
                except ValueError:
                    pass
            self.checking = True
        threading.Thread(target=self._check, daemon=True).start()
        return True

    def _check(self):
        try:
            container = docker_api("GET", "/containers/" + CONTAINER + "/json")
            running_id = container["Image"]
            image = docker_api("GET", "/images/" + running_id + "/json")
            upstream_ids = upstream_image_digests(image.get("Architecture", "amd64"), image.get("Os", "linux"))
            result = {"checked_at": utc_now(), "running_id": running_id,
                      "upstream_id": sorted(upstream_ids)[0], "update_available": running_id not in upstream_ids,
                      "error": None}
        except Exception as exc:
            result = {"checked_at": utc_now(), "running_id": None, "upstream_id": None,
                      "update_available": None, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
        with self.lock:
            self.check = result
            self.checking = False

    def _stack_dir(self):
        own = docker_api("GET", "/containers/" + socket.gethostname() + "/json")
        mount = next((item for item in own["Mounts"] if item.get("Destination") == "/app/server.py"), None)
        if not mount or not mount["Source"].endswith("/gpu_control/server.py"):
            raise RuntimeError("Could not identify the installed Compose directory")
        return os.path.dirname(os.path.dirname(mount["Source"]))

    def _helper(self, old_image, job_id):
        stack = self._stack_dir()
        container = None
        name = "local-ai-webui-update-" + job_id[:12]
        try:
            container = docker_api("POST", "/containers/create?name=" + name, {
                "Image": HELPER_IMAGE,
                "Cmd": ["sh", "/stack/gpu_control/update-webui.sh"],
                "Env": ["OLD_IMAGE_ID=" + old_image, "JOB_ID=" + job_id,
                        "BACKUP_HOST_ROOT=" + stack + "/gpu_control/results/webui-backups"],
                "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock",
                                         stack + ":/stack:ro",
                                         stack + "/gpu_control/results:/backups"],
                               "NetworkMode": "bridge", "Memory": 768 * 2**20}})["Id"]
            docker_api("POST", "/containers/" + container + "/start")
            for attempt in range(1350):
                state = docker_api("GET", "/containers/" + container + "/json")["State"]
                if not state.get("Running"):
                    raw = docker_api("GET", "/containers/" + container + "/logs?stdout=1&stderr=1&tail=30")
                    log = docker_stream_text(raw)
                    if state.get("ExitCode") != 0:
                        raise RuntimeError("Open WebUI update failed: " + log[-700:])
                    return log
                if attempt % 10 == 0:
                    self._job(job_id, stage="backing up data, pulling image, and checking WebUI health")
                time.sleep(2)
            raise TimeoutError("Open WebUI update exceeded 45 minutes; inspect the helper container")
        finally:
            if container:
                try:
                    docker_api("DELETE", "/containers/" + container + "?force=1")
                except RuntimeError:
                    pass

    def start_update(self):
        with self.lock:
            if self.busy or self.checking:
                raise RuntimeError("Open WebUI check or update already running")
            checked = self.check["checked_at"]
            if (not self.check["update_available"] or not checked or
                    dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(checked) > CHECK_INTERVAL):
                raise ValueError("No current Open WebUI update detected; reload the switcher first")
            old = self.running_image_id()
            if old != self.check["running_id"]:
                raise ValueError("Open WebUI image changed; reload the switcher first")
            job_id = str(uuid.uuid4())
            backup_path = self._stack_dir() + "/gpu_control/results/webui-backups/" + job_id
            with self.connect() as db:
                db.execute("INSERT INTO webui_update_jobs(id,started_at,status,stage,old_image,backup_path) VALUES(?,?,?,?,?,?)",
                           (job_id, utc_now(), "running", "queued", old, backup_path))
            self.busy = True
        threading.Thread(target=self._update, args=(job_id, old), daemon=True).start()
        return job_id

    def _update(self, job_id, old):
        try:
            self._job(job_id, stage="starting Open WebUI update")
            self._helper(old, job_id)
            new = self.running_image_id()
            if new == old:
                raise RuntimeError("Open WebUI still runs the old image")
            self._job(job_id, status="completed", stage="Open WebUI updated and healthy",
                      new_image=new, ended_at=utc_now())
        except Exception as exc:
            self._job(job_id, status="failed", stage="update failed", ended_at=utc_now(),
                      error=f"{type(exc).__name__}: {str(exc)[:700]}")
        finally:
            with self.lock:
                self.busy = False
                self.check["checked_at"] = None
            self.check_async(force=True)
