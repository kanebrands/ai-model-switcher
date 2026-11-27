"""Test and persist context changes for the active text or vision model."""
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


SERVICES = {"cpu-tiny": "cpu-tiny", "cpu-4b": "cpu-4b", "llm": "llm", "qwen4b": "qwen4b", "huihui": "huihui",
            "flint-iq4": "flint-iq4", "flint-q3": "flint-q3",
            "deepseek": "deepseek-v4-distill", "qwen38": "qwen38-ablit",
            "gemma4": "gemma4-ablit", "vision": "vision"}
HELPER_IMAGE = "docker:cli"
CONTEXT_PRESETS = (4096, 8192, 16384, 32768, 65536, 131072, 262144)


def context_presets(current_tokens, native_limit=None):
    """Offer tuner-accepted sizes no larger than a reported native limit."""
    ceiling = min(262144, native_limit) if isinstance(native_limit, int) and native_limit > 0 else 262144
    return [tokens for tokens in CONTEXT_PRESETS if tokens <= ceiling and tokens != current_tokens]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def context_flag(command):
    for flag in ("--ctx-size", "--max-model-len"):
        if flag in command:
            i = command.index(flag)
            return flag, int(command[i + 1])
    raise ValueError("Active model has no configurable context")


def parallel_slots(command):
    for flag in ("--parallel", "--max-num-seqs"):
        if flag in command:
            return int(command[command.index(flag) + 1])
    return 1


class ContextTuner:
    def __init__(self, db_path, model_manager, api_key):
        self.db_path = db_path
        self.model_manager = model_manager
        self.api_key = api_key
        self.lock = threading.Lock()
        self.busy = False
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS context_jobs (
                id TEXT PRIMARY KEY, mode TEXT NOT NULL, previous_tokens INTEGER NOT NULL,
                requested_tokens INTEGER NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT,
                hardware_fingerprint TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(context_jobs)")}
            if "hardware_fingerprint" not in columns:
                db.execute("ALTER TABLE context_jobs ADD COLUMN hardware_fingerprint TEXT")
            if "parallel_slots" not in columns:
                db.execute("ALTER TABLE context_jobs ADD COLUMN parallel_slots INTEGER NOT NULL DEFAULT 1")
            db.execute("UPDATE context_jobs SET status='interrupted', stage='controller restarted', ended_at=? WHERE status='running'", (now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def _job(self, job_id, **values):
        with self.connect() as db:
            db.execute("UPDATE context_jobs SET " + ", ".join(key + "=?" for key in values) + " WHERE id=?",
                       (*values.values(), job_id))

    def status(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM context_jobs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        return {"busy": self.busy, "job": dict(row) if row else None}

    def contexts(self, hardware_fingerprint=None):
        """Configured contexts, with a tuned label only for completed tests here."""
        with self.connect() as db:
            completed = db.execute("SELECT mode,requested_tokens,hardware_fingerprint,parallel_slots FROM context_jobs "
                                   "WHERE status='completed' ORDER BY ended_at DESC, rowid DESC").fetchall()
        latest = {}
        for row in completed:
            latest.setdefault(row["mode"], row)
        pending = self.status()["job"]
        result = {}
        for mode, (_, container) in MODELS.items():
            if mode == "media":
                continue
            try:
                inspect = docker_api("GET", "/containers/" + container + "/json")
                _, tokens = context_flag(inspect["Config"]["Cmd"])
                slots = parallel_slots(inspect["Config"]["Cmd"])
                running = bool(inspect["State"].get("Running"))
            except (RuntimeError, ValueError, KeyError, IndexError):
                result[mode] = {"tokens": None, "state": "unavailable", "running": False}
                continue
            if pending and pending["status"] == "running" and pending["mode"] == mode:
                result[mode] = {"tokens": None, "state": "test in progress", "running": running}
                continue
            verified = latest.get(mode)
            tuned = bool(verified and verified["requested_tokens"] == tokens and verified["parallel_slots"] == slots
                         and hardware_fingerprint and verified["hardware_fingerprint"] == hardware_fingerprint)
            result[mode] = {"tokens": tokens, "state": "tuned" if tuned else "configured",
                            "running": running}
        return result

    def active(self, mode):
        if mode not in MODELS:
            return None
        container = MODELS[mode][1]
        inspect = docker_api("GET", "/containers/" + container + "/json")
        if not inspect["State"].get("Running"):
            return None
        flag, tokens = context_flag(inspect["Config"]["Cmd"])
        return {"mode": mode, "container": container, "flag": flag, "tokens": tokens,
                "slots": parallel_slots(inspect["Config"]["Cmd"]),
                "alias": MODELS[mode][0]}

    def start(self, mode, tokens):
        if not isinstance(tokens, int) or tokens < 4096 or tokens > 262144 or tokens % 1024:
            raise ValueError("Context must be 4096-262144 tokens in steps of 1024")
        with self.lock:
            if self.busy:
                raise RuntimeError("A context test is already running")
            current = self.active(mode)
            if not current:
                raise ValueError("Select and start a model before tuning its context")
            if current["tokens"] == tokens:
                raise ValueError("The requested context is already configured")
            job_id = str(uuid.uuid4())
            with self.connect() as db:
                fingerprint = self.model_manager.hardware().get("fingerprint")
                db.execute("INSERT INTO context_jobs(id,mode,previous_tokens,requested_tokens,started_at,status,stage,hardware_fingerprint,parallel_slots) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (job_id, mode, current["tokens"], tokens, now(), "running", "queued", fingerprint, current["slots"]))
            self.busy = True
        threading.Thread(target=self._run, args=(job_id, current, tokens), daemon=True).start()
        return job_id

    def _stack_dir(self):
        own = docker_api("GET", "/containers/" + socket.gethostname() + "/json")
        mount = next((item for item in own["Mounts"] if item.get("Destination") == "/app/server.py"), None)
        if not mount or not mount["Source"].endswith("/gpu_control/server.py"):
            raise RuntimeError("Could not identify installed Compose directory")
        return os.path.dirname(os.path.dirname(mount["Source"]))

    def _helper(self, action, service, tokens, job_id):
        stack = self._stack_dir()
        name = "local-ai-context-" + job_id[:12] + "-" + action
        container = None
        try:
            container = docker_api("POST", "/containers/create?name=" + name, {
                "Image": HELPER_IMAGE,
                "Cmd": ["sh", "/stack/gpu_control/tune-context.sh", action, service, str(tokens)],
                "HostConfig": {"Binds": ["/var/run/docker.sock:/var/run/docker.sock", stack + ":/stack"],
                               "NetworkMode": "bridge", "Memory": 768 * 2**20}})["Id"]
            docker_api("POST", "/containers/" + container + "/start")
            for _ in range(300):
                state = docker_api("GET", "/containers/" + container + "/json")["State"]
                if not state.get("Running"):
                    raw = docker_api("GET", "/containers/" + container + "/logs?stdout=1&stderr=1&tail=20")
                    if state.get("ExitCode") != 0:
                        raise RuntimeError("Context helper failed: " + docker_stream_text(raw)[-400:])
                    return
                time.sleep(2)
            raise TimeoutError("Context change exceeded ten minutes")
        finally:
            if container:
                try:
                    docker_api("DELETE", "/containers/" + container + "?force=1")
                except RuntimeError:
                    pass

    def _wait_ready(self, current, tokens, old_id=None):
        for _ in range(200):
            try:
                inspect = docker_api("GET", "/containers/" + current["container"] + "/json")
                health = (inspect["State"].get("Health") or {}).get("Status")
                _, actual = context_flag(inspect["Config"]["Cmd"])
                if (inspect["State"].get("Running") and actual == tokens and
                        (old_id is None or inspect["Id"] != old_id) and health != "unhealthy"):
                    request = urllib.request.Request("http://nginx:8080/v1/models",
                                                     headers={"Authorization": "Bearer " + self.api_key})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        models = json.load(response)
                    item = next((item for item in models.get("data", [])
                                 if item.get("id") == current["alias"]), None)
                    if item:
                        reported = (item.get("meta") or {}).get("n_ctx") or item.get("max_model_len")
                        if reported is None or int(reported) >= tokens:
                            return True
            except (OSError, RuntimeError, ValueError, KeyError):
                pass
            time.sleep(3)
        return False

    def _run(self, job_id, current, tokens):
        old_id = None
        backup = None
        try:
            self._job(job_id, stage="restarting model with requested context")
            if current["mode"] in SERVICES:
                old_id = docker_api("GET", "/containers/" + current["container"] + "/json")["Id"]
                self._helper("apply", SERVICES[current["mode"]], tokens, job_id)
            else:
                backup_name = current["container"] + "-context-rollback-" + job_id[:8]
                row = next(item for item in self.model_manager.installed()
                           if "hf-" + item["candidate_id"] == current["mode"])
                old = docker_api("GET", "/containers/" + current["container"] + "/json")
                docker_api("POST", "/containers/" + current["container"] + "/stop?t=30")
                docker_api("POST", "/containers/" + current["container"] + "/rename?name=" + backup_name)
                backup = backup_name
                docker_api("POST", "/containers/create?name=" + current["container"],
                           self.model_manager.serving_config(row["filename"], row["alias"], tokens, old["Image"],
                                                             row["parallel_slots"], row.get("runtime", "gpu"),
                                                             row.get("format", "gguf"),
                                                             cpu_offload_gb=row.get("cpu_offload_gb", 0),
                                                             hybrid_enabled=bool(row.get("hybrid_enabled", 1))))
                docker_api("POST", "/containers/" + current["container"] + "/start")
            self._job(job_id, stage="waiting for model readiness")
            if not self._wait_ready(current, tokens, old_id):
                raise RuntimeError("Model did not become ready with the requested context")
            if current["mode"] in SERVICES:
                self._helper("commit", SERVICES[current["mode"]], tokens, job_id)
            else:
                with self.connect() as db:
                    db.execute("UPDATE installed_models SET context_tokens=? WHERE container=?",
                               (tokens, current["container"]))
                try:
                    docker_api("DELETE", "/containers/" + backup + "?force=1")
                except RuntimeError:
                    pass
                backup = None
            self._job(job_id, status="completed", stage="context tested and saved", ended_at=now())
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self._job(job_id, stage="restoring previous context")
            try:
                if current["mode"] in SERVICES:
                    self._helper("rollback", SERVICES[current["mode"]], current["tokens"], job_id)
                elif backup:
                    try:
                        docker_api("DELETE", "/containers/" + current["container"] + "?force=1")
                    except RuntimeError:
                        pass
                    docker_api("POST", "/containers/" + backup + "/rename?name=" + current["container"])
                    docker_api("POST", "/containers/" + current["container"] + "/start")
                elif current["mode"] not in SERVICES:
                    docker_api("POST", "/containers/" + current["container"] + "/start")
                if not self._wait_ready(current, current["tokens"]):
                    error += "; previous model did not become ready after rollback"
            except Exception as rollback_error:
                error += "; rollback failed: " + str(rollback_error)[:150]
            self._job(job_id, status="failed", stage="failed", error=error, ended_at=now())
        finally:
            with self.lock:
                self.busy = False
