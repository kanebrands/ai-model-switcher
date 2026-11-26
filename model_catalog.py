"""Hardware-scored, allowlisted Hugging Face GGUF downloads for the switcher."""
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from benchmarks import MODELS, docker_api, docker_exec, docker_stream_text


# These are reviewed GGUF files, not arbitrary user-supplied repositories or code.
# Features describe intended use, not measured performance on this host.
CATALOG = (
    {"id": "huihui-qwen35-9b", "name": "Huihui Qwen3.5 9B abliterated", "repo": "mradermacher/Huihui-Qwen3.5-9B-abliterated-GGUF", "file": "Huihui-Qwen3.5-9B-abliterated.Q4_K_M.gguf", "active_b": 9, "moe": False, "ablit": True, "features": "Fast email classification, summarization, light coding", "priority": 86},
    {"id": "huihui-qwen35-27b", "name": "Huihui Qwen3.5 27B abliterated", "repo": "mradermacher/Huihui-Qwen3.5-27B-abliterated-GGUF", "file": "Huihui-Qwen3.5-27B-abliterated.Q4_K_M.gguf", "active_b": 27, "moe": False, "ablit": True, "features": "Coding, detailed writing, reasoning", "priority": 82},
    {"id": "huihui-qwen35-35b-a3b", "name": "Huihui Qwen3.5 35B-A3B abliterated", "repo": "mradermacher/Huihui-Qwen3.5-35B-A3B-abliterated-GGUF", "file": "Huihui-Qwen3.5-35B-A3B-abliterated.IQ4_XS.gguf", "active_b": 3, "moe": True, "ablit": True, "features": "MoE reasoning, coding, long documents", "priority": 89},
    {"id": "qwen35-9b-ablit", "name": "Qwen3.5 9B abliterated", "repo": "mradermacher/Qwen3.5-9B-abliterated-GGUF", "file": "Qwen3.5-9B-abliterated.Q4_K_M.gguf", "active_b": 9, "moe": False, "ablit": True, "features": "Fast general chat, classification, code snippets", "priority": 78},
    {"id": "qwen35-27b-ablit", "name": "Qwen3.5 27B abliterated", "repo": "mradermacher/Qwen3.5-27B-abliterated-GGUF", "file": "Qwen3.5-27B-abliterated.Q4_K_M.gguf", "active_b": 27, "moe": False, "ablit": True, "features": "General reasoning, coding, writing", "priority": 79},
    {"id": "gemma4-12b-ablit", "name": "Gemma 4 12B abliterated", "repo": "mradermacher/gemma-4-12B-it-abliterated-GGUF", "file": "Gemma-4-12b-it-Abliterated.Q4_K_M.gguf", "active_b": 12, "moe": False, "ablit": True, "features": "General text, coding, document analysis", "priority": 77},
    {"id": "qwen35-9b", "name": "Qwen3.5 9B", "repo": "unsloth/Qwen3.5-9B-GGUF", "file": "Qwen3.5-9B-Q4_K_M.gguf", "active_b": 9, "moe": False, "ablit": False, "features": "Fast email classification, summarization, light coding", "priority": 85},
    {"id": "qwen35-27b", "name": "Qwen3.5 27B", "repo": "bartowski/Qwen_Qwen3.5-27B-GGUF", "file": "Qwen_Qwen3.5-27B-Q4_K_M.gguf", "active_b": 27, "moe": False, "ablit": False, "features": "Coding, reasoning, detailed writing", "priority": 82},
    {"id": "qwen35-35b-a3b", "name": "Qwen3.5 35B-A3B", "repo": "unsloth/Qwen3.5-35B-A3B-GGUF", "file": "Qwen3.5-35B-A3B-UD-IQ4_XS.gguf", "active_b": 3, "moe": True, "ablit": False, "features": "MoE reasoning, coding, long documents", "priority": 88},
    {"id": "gpt-oss-20b", "name": "GPT-OSS 20B", "repo": "unsloth/gpt-oss-20b-GGUF", "file": "gpt-oss-20b-Q4_K_M.gguf", "active_b": 4, "moe": True, "ablit": False, "features": "Tool calling, coding, general reasoning", "priority": 81},
    {"id": "gemma4-26b-a4b", "name": "Gemma 4 26B-A4B", "repo": "bartowski/google_gemma-4-26B-A4B-it-GGUF", "file": "google_gemma-4-26B-A4B-it-Q4_K_M.gguf", "active_b": 4, "moe": True, "ablit": False, "features": "MoE reasoning, coding, document analysis", "priority": 84},
    {"id": "gemma4-31b", "name": "Gemma 4 31B", "repo": "bartowski/google_gemma-4-31B-it-GGUF", "file": "google_gemma-4-31B-it-Q4_K_M.gguf", "active_b": 31, "moe": False, "ablit": False, "features": "Detailed reasoning, coding, writing; slower dense model", "priority": 72},
)
BY_ID = {item["id"]: item for item in CATALOG}
REFERENCE_ACTIVE_B = {"local-coder": (3, True), "local-qwen4b": (4, False),
                      "local-huihui": (3, True), "local-flint-iq4": (2.5, True),
                      "local-flint-q3": (2.5, True), "local-deepseek-v4-distill": (9, False),
                      "local-qwen38-ablit": (3, True), "local-gemma4-ablit": (4, True)}
GGUF_IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda13"
DOWNLOAD_IMAGE = "vllm/vllm-openai:v0.32.0-ubuntu2404"
NATIVE_CONTEXT = {"gpt-oss-20b": 131072}
DEFAULT_NATIVE_CONTEXT = 262144  # Qwen3.5 and Gemma 4 catalog families


def context_for_hardware(candidate, size_bytes, vram_mib):
    """Choose a conservative serving target; this is not a tested maximum."""
    if not vram_mib:
        return None
    headroom_gib = (vram_mib * 2**20 - size_bytes) / 2**30
    if headroom_gib < 4:
        return None
    target = 65536 if headroom_gib >= 12 else 32768 if headroom_gib >= 6 else 16384
    return min(target, NATIVE_CONTEXT.get(candidate["id"], DEFAULT_NATIVE_CONTEXT))


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def hub_metadata(candidate):
    url = "https://huggingface.co/api/models/" + urllib.parse.quote(candidate["repo"], safe="/") + "/revision/main?blobs=true"
    request = urllib.request.Request(url, headers={"User-Agent": "local-ai-model-switcher/1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        info = json.load(response)
    if info.get("gated") or not re.fullmatch(r"[0-9a-f]{40}", info.get("sha", "")):
        raise ValueError("Repository is gated or missing a commit hash")
    blob = next((file for file in info.get("siblings", []) if file.get("rfilename") == candidate["file"]), None)
    if not blob:
        raise ValueError("Selected GGUF is unavailable on Hugging Face")
    size = blob.get("size") or (blob.get("lfs") or {}).get("size")
    digest = (blob.get("lfs") or {}).get("sha256")
    if not isinstance(size, int) or not 100_000_000 < size < 100_000_000_000 or not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise ValueError("Hugging Face did not return verified GGUF size and SHA256")
    return {"revision": info["sha"], "size": size, "sha256": digest}


class ModelManager:
    def __init__(self, path, api_key):
        self.path = path
        self.api_key = api_key
        self.model_dir = os.environ["MODEL_DIR"]
        self.host_dir = os.environ["MODEL_HOST_DIR"]
        self.network = os.environ["MODEL_NETWORK"]
        self.lock = threading.Lock()
        self.hardware_lock = threading.Lock()
        self.hardware_cache = None
        self.hardware_cache_at = 0
        self.busy = False
        self.cache = {}
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS installed_models (
                candidate_id TEXT PRIMARY KEY, repo TEXT NOT NULL, filename TEXT NOT NULL,
                revision TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                installed_at TEXT NOT NULL, alias TEXT NOT NULL, container TEXT NOT NULL,
                context_tokens INTEGER)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(installed_models)")}
            if "context_tokens" not in columns:
                db.execute("ALTER TABLE installed_models ADD COLUMN context_tokens INTEGER")
                # Earlier installs were created with a fixed 32K context.
                db.execute("UPDATE installed_models SET context_tokens=32768 WHERE context_tokens IS NULL")
            if "parallel_slots" not in columns:
                db.execute("ALTER TABLE installed_models ADD COLUMN parallel_slots INTEGER NOT NULL DEFAULT 1")
            db.execute("""CREATE TABLE IF NOT EXISTS model_jobs (
                id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT)""")
            db.execute("UPDATE model_jobs SET status='interrupted', stage='controller restarted', ended_at=? WHERE status IN ('queued','running')", (now(),))
            db.execute("""CREATE TABLE IF NOT EXISTS hardware_state (
                id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL,
                changed_at TEXT NOT NULL)""")
        for item in self.installed():
            MODELS["hf-" + item["candidate_id"]] = (item["alias"], item["container"])

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def installed(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM installed_models ORDER BY installed_at DESC")]

    def jobs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM model_jobs ORDER BY started_at DESC LIMIT 10")]

    def _job(self, job_id, **values):
        with self.connect() as db:
            columns = ", ".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE model_jobs SET {columns} WHERE id=?", (*values.values(), job_id))

    def _probe_hardware(self):
        info = docker_api("GET", "/info")
        ram = info["MemTotal"]
        gpu_name, vram_mib, gpu_uuid = None, None, None
        query = ["nvidia-smi", "--query-gpu=name,memory.total,uuid", "--format=csv,noheader,nounits"]
        output = None
        for _, container in MODELS.values():
            try:
                state = docker_api("GET", f"/containers/{container}/json")["State"]
                if not state.get("Running"):
                    continue
                output = docker_exec(container, query)
                break
            except (RuntimeError, ValueError, IndexError, KeyError):
                continue
        if not output:
            # Probe independently of the selected model. This also works just
            # after a GPU replacement, before any model container is running.
            container_id = None
            try:
                container_id = docker_api("POST", "/containers/create?name=local-ai-hardware-" + uuid.uuid4().hex[:12], {
                    "Image": GGUF_IMAGE, "Entrypoint": ["nvidia-smi"], "Cmd": query[1:],
                    "HostConfig": {"NetworkMode": "none", "Memory": 256 * 2**20,
                                   "DeviceRequests": [{"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}]}})["Id"]
                docker_api("POST", f"/containers/{container_id}/start")
                for _ in range(20):
                    state = docker_api("GET", f"/containers/{container_id}/json")["State"]
                    if not state.get("Running"):
                        break
                    time.sleep(.25)
                raw = docker_api("GET", f"/containers/{container_id}/logs?stdout=1&stderr=1&tail=10")
                output = docker_stream_text(raw)
            except (RuntimeError, KeyError):
                pass
            finally:
                if container_id:
                    try:
                        docker_api("DELETE", f"/containers/{container_id}?force=1")
                    except RuntimeError:
                        pass
        if output:
            try:
                gpu_name, vram, gpu_uuid = [part.strip() for part in output.splitlines()[0].split(",", 2)]
                vram_mib = int(vram)
            except (ValueError, IndexError):
                gpu_name, vram_mib, gpu_uuid = None, None, None
        cpu_model = None
        try:
            with open("/proc/cpuinfo", encoding="utf-8") as stream:
                cpu_model = next((line.split(":", 1)[1].strip() for line in stream if line.startswith("model name")), None)
        except OSError:
            pass
        signature = {"gpu": gpu_name, "gpu_uuid": gpu_uuid, "vram_mib": vram_mib,
                     "ram_bytes": ram, "cpu_model": cpu_model, "cpu_threads": info.get("NCPU")}
        fingerprint = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()[:16]
        with self.connect() as db:
            row = db.execute("SELECT fingerprint,changed_at FROM hardware_state WHERE id=1").fetchone()
            changed_at = row["changed_at"] if row and row["fingerprint"] == fingerprint else now()
            db.execute("INSERT INTO hardware_state(id,fingerprint,changed_at) VALUES(1,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET fingerprint=excluded.fingerprint,changed_at=excluded.changed_at",
                       (fingerprint, changed_at))
        return {"gpu": gpu_name, "gpu_uuid": gpu_uuid, "vram_mib": vram_mib,
                "cpu_model": cpu_model, "cpu_threads": info.get("NCPU"),
                "ram_gib": round(ram / 2**30, 1), "fingerprint": fingerprint,
                "changed_at": changed_at, "checked_at": now()}

    def hardware(self, force=False):
        with self.hardware_lock:
            if force or self.hardware_cache is None or time.monotonic() - self.hardware_cache_at >= 60:
                self.hardware_cache = self._probe_hardware()
                self.hardware_cache_at = time.monotonic()
            result = dict(self.hardware_cache)
        disk = shutil.disk_usage(self.model_dir).free
        result["disk_free_gib"] = round(disk / 2**30, 1)
        return result

    def _metadata(self, candidate):
        cached = self.cache.get(candidate["id"])
        if cached and time.monotonic() - cached[0] < 1800:
            return cached[1]
        value = hub_metadata(candidate)
        self.cache[candidate["id"]] = (time.monotonic(), value)
        return value

    def _baseline_rates(self, gpu):
        if not gpu:
            return []
        with self.connect() as db:
            rows = db.execute("SELECT model_id, system_json, results_json FROM runs WHERE status='completed' ORDER BY started_at DESC LIMIT 100").fetchall()
        rates, seen = [], set()
        for row in rows:
            alias = row["model_id"]
            if alias in seen or alias not in REFERENCE_ACTIVE_B:
                continue
            system = json.loads(row["system_json"] or "{}")
            if (system.get("gpu") or {}).get("name") != gpu:
                continue
            value = (json.loads(row["results_json"] or "{}").get("generation") or {}).get("end_to_end_tokens_per_second")
            if isinstance(value, (float, int)) and value > 0:
                active, moe = REFERENCE_ACTIVE_B[alias]
                rates.append((active, moe, value))
                seen.add(alias)
        return rates

    def recommendations(self, prefer_ablit=True, hardware=None):
        hardware = hardware or self.hardware()
        installed = {item["candidate_id"]: item for item in self.installed()}
        rates = self._baseline_rates(hardware["gpu"])
        def get(candidate):
            if candidate["id"] in installed:
                item = installed[candidate["id"]]
                return candidate, {"revision": item["revision"], "size": item["size"], "sha256": item["sha256"]}, None
            try:
                metadata = self._metadata(candidate)
                return candidate, metadata, None
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                return candidate, None, f"Hugging Face metadata unavailable: {type(exc).__name__}"
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            fetched = list(executor.map(get, CATALOG))
        suggestions = []
        for candidate, metadata, error in fetched:
            if error:
                continue
            size = metadata["size"]
            vram = hardware["vram_mib"]
            fits = bool(vram and size + 4 * 2**30 <= vram * 2**20)
            if candidate["id"] not in installed and (not fits or size * 1.2 > hardware["disk_free_gib"] * 2**30):
                continue
            comparable = [rate * (active / candidate["active_b"]) ** .35 for active, moe, rate in rates if moe == candidate["moe"]]
            estimate = None
            if comparable:
                middle = statistics.median(comparable)
                estimate = f"{max(1, round(middle * .55))}–{round(middle * 1.45)}"
            score = candidate["priority"] + (18 if prefer_ablit and candidate["ablit"] else 0)
            score -= max(0, (size / (vram * 2**20) - .7) * 20)
            suggestions.append({**candidate, **metadata, "size_gb": round(size / 1e9, 1),
                                "estimated_tps": estimate, "score": score,
                                "installed": candidate["id"] in installed, "fits": fits,
                                "context_tokens": (installed[candidate["id"]].get("context_tokens") or 32768)
                                    if candidate["id"] in installed else context_for_hardware(candidate, size, vram)})
        # Keep room for models the user can actually install. Installed models
        # remain ranked by score, but cannot consume every recommendation slot.
        suggestions.sort(key=lambda item: (-item["score"], item["name"]))
        available = [item for item in suggestions if not item["installed"]]
        reserved = available[:3]
        reserved_ids = {item["id"] for item in reserved}
        chosen = reserved + [item for item in suggestions if item["id"] not in reserved_ids][:10 - len(reserved)]
        chosen.sort(key=lambda item: (-item["score"], item["name"]))
        return hardware, chosen

    def start_install(self, candidate_id, candidate=None, engine_build=None):
        candidate = candidate or BY_ID.get(candidate_id)
        if not candidate:
            raise ValueError("Unknown catalog model")
        if candidate["id"] != candidate_id or not re.fullmatch(r"[a-z0-9-]{2,64}", candidate_id):
            raise ValueError("Invalid catalog candidate")
        if candidate.get("min_engine_build", 0) and (engine_build or 0) < candidate["min_engine_build"]:
            raise ValueError(f"This model requires llama.cpp build {candidate['min_engine_build']} or newer; update the engine first")
        with self.lock:
            if self.busy:
                raise RuntimeError("A model download is already running")
            if any(item["candidate_id"] == candidate_id for item in self.installed()):
                raise ValueError("Model already installed")
            metadata = self._metadata(candidate)
            hardware = self.hardware()
            if not hardware["vram_mib"] or metadata["size"] + 4 * 2**30 > hardware["vram_mib"] * 2**20:
                raise ValueError("Model does not fit GPU with the required 4 GiB reserve")
            if metadata["size"] * 1.2 > hardware["disk_free_gib"] * 2**30:
                raise ValueError("Insufficient model storage")
            context_tokens = context_for_hardware(candidate, metadata["size"], hardware["vram_mib"])
            job_id = str(uuid.uuid4())
            with self.connect() as db:
                db.execute("INSERT INTO model_jobs(id,candidate_id,started_at,status,stage) VALUES(?,?,?,?,?)",
                           (job_id, candidate_id, now(), "queued", "queued"))
            self.busy = True
            threading.Thread(target=self._install, args=(job_id, candidate, metadata, context_tokens), daemon=True).start()
            return job_id

    def _install(self, job_id, candidate, metadata, context_tokens):
        downloader = None
        serving = None
        try:
            self._job(job_id, status="running", stage="downloading")
            name = "local-ai-hf-download-" + job_id[:12]
            stage_dir = ".catalog_downloads/" + candidate["id"]
            config = {"Image": DOWNLOAD_IMAGE, "Entrypoint": ["hf"],
                      "Cmd": ["download", candidate["repo"], candidate["file"], "--revision", metadata["revision"],
                              "--local-dir", "/models/" + stage_dir],
                      "Env": ["HF_HOME=/tmp/hf"],
                      "HostConfig": {"Binds": [self.host_dir + ":/models"], "NetworkMode": "bridge", "Memory": 8 * 2**30}}
            downloader = docker_api("POST", "/containers/create?name=" + name, config)["Id"]
            docker_api("POST", f"/containers/{downloader}/start")
            for _ in range(720):
                state = docker_api("GET", f"/containers/{downloader}/json")["State"]
                if not state.get("Running"):
                    if state.get("ExitCode") != 0:
                        raise RuntimeError("Hugging Face download failed; inspect the download container logs")
                    break
                time.sleep(5)
            else:
                raise TimeoutError("Download exceeded one hour")
            self._job(job_id, stage="verifying SHA256")
            filename = candidate["file"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", filename):
                raise ValueError("Unsafe model filename")
            path = os.path.join(self.model_dir, stage_dir, filename)
            if os.path.getsize(path) != metadata["size"]:
                raise ValueError("Downloaded file size does not match Hugging Face")
            digest = hashlib.sha256()
            with open(path, "rb") as file:
                while chunk := file.read(4 * 2**20):
                    digest.update(chunk)
            if digest.hexdigest() != metadata["sha256"]:
                raise ValueError("Downloaded file SHA256 does not match Hugging Face")
            if candidate["id"].startswith("discovered-"):
                filename = candidate["id"] + "--" + filename
            destination = os.path.join(self.model_dir, filename)
            if os.path.lexists(destination):
                if os.path.islink(destination) or os.path.getsize(destination) != metadata["size"]:
                    raise ValueError("A different GGUF already has this destination name")
                existing = hashlib.sha256()
                with open(destination, "rb") as stream:
                    while chunk := stream.read(4 * 2**20):
                        existing.update(chunk)
                if existing.hexdigest() != metadata["sha256"]:
                    raise ValueError("A different GGUF already has this destination name")
            else:
                os.replace(path, destination)
            self._job(job_id, stage="creating serving container")
            container = "local-ai-hf-" + candidate["id"]
            alias = "local-hf-" + candidate["id"]
            serving_config = self.serving_config(filename, alias, context_tokens)
            serving = docker_api("POST", "/containers/create?name=" + container, serving_config)["Id"]
            with self.connect() as db:
                db.execute("INSERT INTO installed_models(candidate_id,repo,filename,revision,sha256,size,installed_at,alias,container,context_tokens) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (candidate["id"], candidate["repo"], filename, metadata["revision"], metadata["sha256"], metadata["size"], now(), alias, container, context_tokens))
            MODELS["hf-" + candidate["id"]] = (alias, container)
            serving = None
            self._job(job_id, status="completed", stage="installed; ready to select", ended_at=now())
        except Exception as exc:
            self._job(job_id, status="failed", stage="failed", ended_at=now(), error=f"{type(exc).__name__}: {str(exc)[:250]}")
        finally:
            for container_id in (downloader, serving):
                if container_id:
                    try:
                        docker_api("DELETE", f"/containers/{container_id}?force=1")
                    except RuntimeError:
                        pass
            with self.lock:
                self.busy = False

    def serving_config(self, filename, alias, context_tokens, image=GGUF_IMAGE, parallel_slots=1):
        return {"Image": image, "Env": ["LLAMA_API_KEY=" + self.api_key],
                "Cmd": ["--model", "/models/" + filename, "--alias", alias, "--host", "0.0.0.0", "--port", "8080",
                        "--ctx-size", str(context_tokens or 16384), "--n-gpu-layers", "auto", "--fit-target", "2048",
                        "--flash-attn", "on", "--parallel", str(parallel_slots)],
                "Labels": {"local-ai.model-manager": "1"},
                "HostConfig": {"Binds": [self.host_dir + ":/models:ro"], "NetworkMode": self.network,
                               "DeviceRequests": [{"Driver": "nvidia", "Count": 1, "Capabilities": [["gpu"]]}],
                               "RestartPolicy": {"Name": "unless-stopped"}}}

    def ensure_current_engine(self, mode):
        """Recreate a stopped managed container on the latest pulled image.

        Return the renamed previous container for rollback after start.
        """
        if not mode.startswith("hf-"):
            return None
        row = next((item for item in self.installed() if "hf-" + item["candidate_id"] == mode), None)
        if not row:
            raise ValueError("Managed model is not installed")
        name = row["container"]
        inspect = docker_api("GET", "/containers/" + name + "/json")
        if inspect["State"].get("Running"):
            return None
        image = docker_api("GET", "/images/" + urllib.parse.quote(GGUF_IMAGE, safe="") + "/json")["Id"]
        if inspect["Image"] == image:
            return None
        backup = name + "-rollback-" + uuid.uuid4().hex[:8]
        docker_api("POST", "/containers/" + name + "/rename?name=" + backup)
        try:
            docker_api("POST", "/containers/create?name=" + name,
                       self.serving_config(row["filename"], row["alias"], row["context_tokens"],
                                           parallel_slots=row["parallel_slots"]))
        except Exception:
            docker_api("POST", "/containers/" + backup + "/rename?name=" + name)
            raise
        return backup

    def finish_engine_recreation(self, mode, backup, success):
        if not backup:
            return
        name = MODELS[mode][1]
        if success:
            docker_api("DELETE", "/containers/" + backup + "?force=1")
        else:
            docker_api("DELETE", "/containers/" + name + "?force=1")
            docker_api("POST", "/containers/" + backup + "/rename?name=" + name)
