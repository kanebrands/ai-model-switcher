"""Check GHCR for llama.cpp updates and apply them only after a UI action."""
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid

from benchmarks import MODELS, docker_api, docker_stream_text


IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda13"
REPO = "ggml-org/llama.cpp"
HELPER_IMAGE = "docker:cli"
CHECK_INTERVAL = dt.timedelta(hours=6)
BUILTIN_LLAMA_MODES = ("huihui", "flint-iq4", "flint-q3", "deepseek", "qwen38", "gemma4")


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def registry_json(url, token, accept):
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token,
                                                    "Accept": accept, "User-Agent": "local-ai-model-switcher/2"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def ghcr_tag_digest():
    token_url = "https://ghcr.io/token?" + urllib.parse.urlencode({"scope": "repository:" + REPO + ":pull", "service": "ghcr.io"})
    with urllib.request.urlopen(token_url, timeout=20) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request("https://ghcr.io/v2/" + REPO + "/manifests/server-cuda13",
                                     headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
                                              "User-Agent": "local-ai-model-switcher/2"})
    with urllib.request.urlopen(request, timeout=20) as response:
        digest = response.headers.get("Docker-Content-Digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
        raise ValueError("GHCR did not provide an image manifest digest")
    return digest


class EngineUpdates:
    def __init__(self, db_path, api_key):
        self.db_path = db_path
        self.api_key = api_key
        self.lock = threading.Lock()
        self.checking = False
        self.busy = False
        self.check = {"checked_at": None, "local_id": None, "upstream_id": None,
                      "local_build": None, "update_available": None, "error": None}
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS engine_jobs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, stage TEXT NOT NULL, old_image TEXT, new_image TEXT, error TEXT)""")
            db.execute("UPDATE engine_jobs SET status='interrupted', ended_at=?, stage='controller restarted' WHERE status='running'",
                       (utc_now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _job(self, job_id, **values):
        with self.connect() as db:
            db.execute("UPDATE engine_jobs SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))

    def latest_job(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM engine_jobs ORDER BY started_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def local_image_id(self):
        return docker_api("GET", "/images/" + urllib.parse.quote(IMAGE, safe="") + "/json")["Id"]

    def local_registry_digest(self):
        details = docker_api("GET", "/images/" + urllib.parse.quote(IMAGE, safe="") + "/json")
        prefix = IMAGE.split(":", 1)[0] + "@"
        digest = next((item[len(prefix):] for item in details.get("RepoDigests", [])
                       if item.startswith(prefix)), None)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest or ""):
            raise ValueError("Local llama.cpp image has no GHCR repository digest; pull it again")
        return digest

    def local_build(self):
        name = "local-ai-engine-version-" + uuid.uuid4().hex[:12]
        container = None
        try:
            container = docker_api("POST", "/containers/create?name=" + name, {
                "Image": IMAGE, "Entrypoint": ["/app/llama-server"], "Cmd": ["--version"],
                "HostConfig": {"NetworkMode": "none", "Memory": 256 * 2**20}})["Id"]
            docker_api("POST", "/containers/" + container + "/start")
            for _ in range(20):
                state = docker_api("GET", "/containers/" + container + "/json")["State"]
                if not state.get("Running"):
                    break
                time.sleep(.5)
            raw = docker_api("GET", "/containers/" + container + "/logs?stdout=1&stderr=1&tail=10")
            match = re.search(r"build\s+(\d+)", docker_stream_text(raw))
            return int(match.group(1)) if match else None
        finally:
            if container:
                docker_api("DELETE", "/containers/" + container + "?force=1")

    def status(self):
        with self.lock:
            result = {**self.check, "checking": self.checking, "busy": self.busy}
        result["job"] = self.latest_job()
        return result

    def check_async(self, force=False):
        with self.lock:
            if self.checking or self.busy:
                return False
            checked = self.check.get("checked_at")
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
            local = self.local_image_id()
            local_digest = self.local_registry_digest()
            upstream = ghcr_tag_digest()
            build = self.local_build()
            result = {"checked_at": utc_now(), "local_id": local, "upstream_id": upstream,
                      "local_build": build, "update_available": local_digest != upstream, "error": None}
        except Exception as exc:
            result = {"checked_at": utc_now(), "local_id": None, "upstream_id": None,
                      "local_build": None, "update_available": None,
                      "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
        with self.lock:
            self.check = result
            self.checking = False

    def _stack_dir(self):
        own = docker_api("GET", "/containers/" + socket.gethostname() + "/json")
        mount = next((item for item in own["Mounts"] if item.get("Destination") == "/app/server.py"), None)
        if not mount or not mount["Source"].endswith("/gpu_control/server.py"):
            raise RuntimeError("Could not identify the installed Compose directory")
        return os.path.dirname(os.path.dirname(mount["Source"]))

    def _helper(self, mode, old_image, job_id):
        stack = self._stack_dir()
        name = "local-ai-engine-update-" + job_id[:12] + "-" + mode
        container = None
        try:
            container = docker_api("POST", "/containers/create?name=" + name, {
                "Image": HELPER_IMAGE, "Cmd": ["sh", "/stack/gpu_control/update-engine.sh", mode],
                "Env": ["OLD_IMAGE_ID=" + old_image],
                "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock",
                                         stack + ":/stack:ro"],
                               "NetworkMode": "bridge", "Memory": 768 * 2**20}})["Id"]
            docker_api("POST", "/containers/" + container + "/start")
            for _ in range(900):
                state = docker_api("GET", "/containers/" + container + "/json")["State"]
                if not state.get("Running"):
                    raw = docker_api("GET", "/containers/" + container + "/logs?stdout=1&stderr=1&tail=20")
                    log = docker_stream_text(raw)
                    if state.get("ExitCode") != 0:
                        raise RuntimeError("Engine helper failed: " + log[-500:])
                    return log
                if _ % 6 == 0:
                    self._job(job_id, stage="pulling image and recreating engine containers")
                time.sleep(2)
            raise TimeoutError("Engine update exceeded 30 minutes")
        finally:
            if container:
                try:
                    docker_api("DELETE", "/containers/" + container + "?force=1")
                except RuntimeError:
                    pass

    def _active_builtin(self):
        for mode in BUILTIN_LLAMA_MODES:
            try:
                state = docker_api("GET", "/containers/" + MODELS[mode][1] + "/json")["State"]
                if state.get("Running"):
                    return mode
            except RuntimeError:
                continue
        return None

    def _wait_ready(self, mode):
        if not mode:
            return True
        expected = MODELS[mode][0]
        for _ in range(90):
            try:
                request = urllib.request.Request("http://nginx:8080/v1/models",
                                                 headers={"Authorization": "Bearer " + self.api_key})
                with urllib.request.urlopen(request, timeout=5) as response:
                    models = json.load(response)
                if expected in [item.get("id") for item in models.get("data", [])]:
                    return True
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(3)
        return False

    def start_update(self):
        with self.lock:
            if self.busy or self.checking:
                raise RuntimeError("Engine check or update already running")
            if not self.check.get("update_available"):
                raise ValueError("No engine update is currently detected; check again first")
            old = self.local_image_id()
            if old != self.check["local_id"]:
                raise ValueError("Local engine image changed; check again")
            job_id = str(uuid.uuid4())
            with self.connect() as db:
                db.execute("INSERT INTO engine_jobs(id,started_at,status,stage,old_image) VALUES(?,?,?,?,?)",
                           (job_id, utc_now(), "running", "queued", old))
            self.busy = True
        threading.Thread(target=self._update, args=(job_id, old), daemon=True).start()
        return job_id

    def _update(self, job_id, old):
        active = self._active_builtin()
        try:
            self._job(job_id, stage="pulling and applying llama.cpp image")
            self._helper("apply", old, job_id)
            new = self.local_image_id()
            self._job(job_id, new_image=new, stage="checking active model")
            if new == old:
                raise RuntimeError("Registry image did not change")
            if not self._wait_ready(active):
                self._job(job_id, stage="active model failed; restoring previous engine")
                self._helper("rollback", old, job_id)
                raise RuntimeError("Updated engine did not make the active model ready; previous image restored")
            self._job(job_id, status="completed", stage="engine updated; managed models update when next selected",
                      ended_at=utc_now())
        except Exception as exc:
            self._job(job_id, status="failed", stage="failed", ended_at=utc_now(),
                      error=f"{type(exc).__name__}: {str(exc)[:500]}")
        finally:
            with self.lock:
                self.busy = False
                self.check["checked_at"] = None
            self.check_async(force=True)
