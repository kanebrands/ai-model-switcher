"""Test model generation slots and restore the previous container on failure."""
import datetime as dt
import json
import os
import socket
import sqlite3
import threading
import time
import urllib.request
import uuid

from benchmarks import MODELS, docker_api, docker_stream_text
from context_tuning import SERVICES, parallel_slots as slot_count


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class ParallelTuner:
    def __init__(self, db_path, model_manager, api_key):
        self.db_path = db_path
        self.model_manager = model_manager
        self.api_key = api_key
        self.lock = threading.Lock()
        self.busy = False
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS parallel_jobs (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, previous_slots INTEGER NOT NULL,
                requested_slots INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
                status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
                hardware_fingerprint TEXT)""")
            db.execute("UPDATE parallel_jobs SET status='interrupted', stage='controller restarted', ended_at=? WHERE status='running'", (now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _job(self, job_id, **values):
        with self.connect() as db:
            db.execute("UPDATE parallel_jobs SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))

    def status(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM parallel_jobs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        return {"busy": self.busy, "job": dict(row) if row else None}

    def active(self, mode):
        if not mode or mode == "media" or mode not in MODELS:
            return None
        container = MODELS[mode][1]
        inspect = docker_api("GET", "/containers/" + container + "/json")
        if not inspect["State"].get("Running"):
            return None
        return {"mode": mode, "container": container, "slots": slot_count(inspect["Config"]["Cmd"]),
                "alias": MODELS[mode][0]}

    def start(self, mode, slots):
        if not isinstance(slots, int) or not 1 <= slots <= 8:
            raise ValueError("Parallel slots must be 1–8")
        with self.lock:
            if self.busy:
                raise RuntimeError("A parallel slot test is already running")
            current = self.active(mode)
            if not current:
                raise ValueError("Start one text model before changing parallel slots")
            if current["slots"] == slots:
                raise ValueError("That model already has the requested slot count")
            job_id = str(uuid.uuid4())
            fingerprint = self.model_manager.hardware().get("fingerprint")
            with self.connect() as db:
                db.execute("INSERT INTO parallel_jobs(id,mode,previous_slots,requested_slots,started_at,status,stage,hardware_fingerprint) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (job_id, mode, current["slots"], slots, now(), "running", "queued", fingerprint))
            self.busy = True
        threading.Thread(target=self._run, args=(job_id, current, slots), daemon=True).start()
        return job_id

    def _stack_dir(self):
        own = docker_api("GET", "/containers/" + socket.gethostname() + "/json")
        mount = next((item for item in own["Mounts"] if item.get("Destination") == "/app/server.py"), None)
        if not mount or not mount["Source"].endswith("/gpu_control/server.py"):
            raise RuntimeError("Could not identify installed Compose directory")
        return os.path.dirname(os.path.dirname(mount["Source"]))

    def _helper(self, action, service, slots, job_id):
        stack = self._stack_dir()
        name = "local-ai-parallel-" + job_id[:12] + "-" + action
        container = None
        try:
            container = docker_api("POST", "/containers/create?name=" + name, {
                "Image": "docker:cli",
                "Cmd": ["sh", "/stack/gpu_control/tune-parallel.sh", action, service, str(slots)],
                "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock", stack + ":/stack"],
                               "NetworkMode": "bridge", "Memory": 768 * 2**20}})["Id"]
            docker_api("POST", "/containers/" + container + "/start")
            for _ in range(300):
                state = docker_api("GET", "/containers/" + container + "/json")["State"]
                if not state.get("Running"):
                    raw = docker_api("GET", "/containers/" + container + "/logs?stdout=1&stderr=1&tail=20")
                    if state.get("ExitCode") != 0:
                        raise RuntimeError("Parallel helper failed: " + docker_stream_text(raw)[-400:])
                    return
                time.sleep(2)
            raise TimeoutError("Parallel change exceeded ten minutes")
        finally:
            if container:
                try:
                    docker_api("DELETE", "/containers/" + container + "?force=1")
                except RuntimeError:
                    pass

    def _wait_ready(self, current, slots, old_id=None):
        for _ in range(200):
            try:
                inspect = docker_api("GET", "/containers/" + current["container"] + "/json")
                health = (inspect["State"].get("Health") or {}).get("Status")
                if (inspect["State"].get("Running") and slot_count(inspect["Config"]["Cmd"]) == slots and
                        (old_id is None or inspect["Id"] != old_id) and health != "unhealthy"):
                    models_path = "/vision/v1/models" if current["mode"] == "vision" else "/v1/models"
                    request = urllib.request.Request("http://nginx:8080" + models_path,
                                                     headers={"Authorization": "Bearer " + self.api_key})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        models = json.load(response)
                    if current["alias"] in [item.get("id") for item in models.get("data", [])]:
                        return True
            except (OSError, RuntimeError, ValueError, KeyError):
                pass
            time.sleep(3)
        return False

    def _run(self, job_id, current, slots):
        old_id = None
        backup = None
        try:
            self._job(job_id, stage="restarting model with requested generation slots")
            if current["mode"] in SERVICES:
                old_id = docker_api("GET", "/containers/" + current["container"] + "/json")["Id"]
                self._helper("apply", SERVICES[current["mode"]], slots, job_id)
            else:
                row = next(item for item in self.model_manager.installed()
                           if "hf-" + item["candidate_id"] == current["mode"])
                old = docker_api("GET", "/containers/" + current["container"] + "/json")
                backup_name = current["container"] + "-parallel-rollback-" + job_id[:8]
                docker_api("POST", "/containers/" + current["container"] + "/stop?t=30")
                docker_api("POST", "/containers/" + current["container"] + "/rename?name=" + backup_name)
                backup = backup_name
                docker_api("POST", "/containers/create?name=" + current["container"],
                           self.model_manager.serving_config(row["filename"], row["alias"],
                                                             row["context_tokens"], old["Image"], slots))
                docker_api("POST", "/containers/" + current["container"] + "/start")
            self._job(job_id, stage="waiting for model readiness")
            if not self._wait_ready(current, slots, old_id):
                raise RuntimeError("Model did not become ready with the requested generation slots")
            if current["mode"] in SERVICES:
                self._helper("commit", SERVICES[current["mode"]], slots, job_id)
            else:
                with self.connect() as db:
                    db.execute("UPDATE installed_models SET parallel_slots=? WHERE container=?",
                               (slots, current["container"]))
                try:
                    docker_api("DELETE", "/containers/" + backup + "?force=1")
                except RuntimeError:
                    pass
                backup = None
            self._job(job_id, status="completed", stage="parallel slots tested and saved", ended_at=now())
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._job(job_id, stage="restoring previous generation slots")
            try:
                if current["mode"] in SERVICES:
                    self._helper("rollback", SERVICES[current["mode"]], current["slots"], job_id)
                elif backup:
                    try:
                        docker_api("DELETE", "/containers/" + current["container"] + "?force=1")
                    except RuntimeError:
                        pass
                    docker_api("POST", "/containers/" + backup + "/rename?name=" + current["container"])
                    docker_api("POST", "/containers/" + current["container"] + "/start")
                else:
                    try:
                        docker_api("POST", "/containers/" + current["container"] + "/start")
                    except RuntimeError:
                        pass
                if not self._wait_ready(current, current["slots"]):
                    error += "; previous model did not become ready after rollback"
            except Exception as rollback_error:
                error += "; rollback failed: " + str(rollback_error)[:150]
            self._job(job_id, status="failed", stage="failed", error=error, ended_at=now())
        finally:
            with self.lock:
                self.busy = False
