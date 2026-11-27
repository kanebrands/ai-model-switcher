"""Recent Hugging Face GGUF discoveries, separate from reviewed recommendations."""
import concurrent.futures
import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
import uuid
from model_artifacts import artifact_sets, SAFE_PATH


AUTHORS = ("bartowski", "unsloth", "mradermacher")
USER_AGENT = {"User-Agent": "local-ai-model-switcher/2"}
MAX_RESULTS = 10
SEARCH_RESULTS = 40
REPO_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}"
MIN_ENGINE_BUILD = {"inkling-2-mini": 12600}


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def fetch_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=USER_AGENT), timeout=20) as response:
        return json.load(response)


def candidate_id(repo, filename):
    return "discovered-" + hashlib.sha256((repo + "/" + filename).encode()).hexdigest()[:16]


def normalize_search(value):
    value = value.strip()
    if value.startswith("https://huggingface.co/"):
        value = value[len("https://huggingface.co/"):].rstrip("/")
    if not value or len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9_./ -]+", value):
        raise ValueError("Enter a model name or Hugging Face owner/repository URL")
    if "/" in value and not re.fullmatch(REPO_PATTERN, value):
        raise ValueError("Use an exact owner/repository ID or a model name")
    return value


def verified_ggufs(siblings):
    """Only standalone GGUF weights with a verifiable Hub LFS digest are installable."""
    files = []
    for row in siblings:
        name = row.get("rfilename", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", name, re.I):
            continue
        if "mmproj" in name.lower() or re.search(r"-\d{5}-of-\d{5}\.gguf$", name, re.I):
            continue
        size = row.get("size") or (row.get("lfs") or {}).get("size")
        digest = (row.get("lfs") or {}).get("sha256")
        if isinstance(size, int) and 100_000_000 < size < 100_000_000_000 and re.fullmatch(r"[0-9a-f]{64}", digest or ""):
            files.append((name, size, digest))
    return sorted(files, key=lambda item: (item[1], item[0].lower()))


def selected_gguf(siblings):
    files = []
    for row in siblings:
        name = row.get("rfilename", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", name):
            continue
        if ("mmproj" in name.lower() or re.search(r"-\d{5}-of-\d{5}\.gguf$", name, re.I)
                or not ("Q4_K_M" in name.upper() or "IQ4_XS" in name.upper())):
            continue
        size = row.get("size") or (row.get("lfs") or {}).get("size")
        digest = (row.get("lfs") or {}).get("sha256")
        if isinstance(size, int) and 100_000_000 < size < 100_000_000_000 and re.fullmatch(r"[0-9a-f]{64}", digest or ""):
            files.append((0 if "Q4_K_M" in name.upper() else 1, size, name, digest))
    return min(files, default=None)


class CatalogDiscovery:
    def __init__(self, db_path, model_manager):
        self.db_path = db_path
        self.model_manager = model_manager
        self.lock = threading.Lock()
        self.busy = False
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS catalog_refreshes (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                query TEXT NOT NULL, status TEXT NOT NULL, error TEXT, items_json TEXT,
                hardware_fingerprint TEXT)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(catalog_refreshes)")}
            if "hardware_fingerprint" not in columns:
                db.execute("ALTER TABLE catalog_refreshes ADD COLUMN hardware_fingerprint TEXT")
            db.execute("UPDATE catalog_refreshes SET status='interrupted', ended_at=? WHERE status='running'", (utc_now(),))
            db.execute("""CREATE TABLE IF NOT EXISTS model_searches (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                query TEXT NOT NULL, status TEXT NOT NULL, error TEXT, items_json TEXT)""")
            db.execute("UPDATE model_searches SET status='interrupted', ended_at=? WHERE status='running'", (utc_now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def latest(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM catalog_refreshes ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "not checked", "items": [], "query": "", "started_at": None, "error": None}
        result = dict(row)
        result["items"] = json.loads(result.pop("items_json") or "[]")
        installed_rows = self.model_manager.installed()
        installed_ids = {item["candidate_id"] for item in installed_rows}
        installed_files = {(item["repo"], item["sha256"]) for item in installed_rows}
        for item in result["items"]:
            item["installed"] = (item["id"] in installed_ids
                                 or (item["repo"], item["sha256"]) in installed_files)
        return result

    def latest_search(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM model_searches ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "not searched", "items": [], "query": "", "error": None}
        result = dict(row)
        result["items"] = json.loads(result.pop("items_json") or "[]")
        installed = {(item["repo"], item["sha256"]) for item in self.model_manager.installed()}
        for item in result["items"]:
            item["installed"] = (item["repo"], item["sha256"]) in installed
        return result

    def start_search(self, query):
        query = normalize_search(query)
        with self.lock:
            if self.busy:
                raise RuntimeError("Another Hugging Face search is running")
            run_id = str(uuid.uuid4())
            with self.connect() as db:
                db.execute("INSERT INTO model_searches(id,started_at,query,status) VALUES(?,?,?,?)",
                           (run_id, utc_now(), query, "running"))
            self.busy = True
        threading.Thread(target=self._search, args=(run_id, query), daemon=True).start()
        return run_id

    def _search(self, run_id, query):
        try:
            hardware = self.model_manager.hardware()
            if re.fullmatch(REPO_PATTERN, query):
                repos = [{"id": query}]
            else:
                url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
                    {"search": query, "sort": "downloads", "direction": "-1", "limit": 40})
                repos = fetch_json(url)
                if not isinstance(repos, list):
                    raise ValueError("Hugging Face search returned an unexpected response")
            def inspect(summary):
                repo = summary.get("id", "")
                if not isinstance(repo, str) or not re.fullmatch(REPO_PATTERN, repo):
                    return []
                url = "https://huggingface.co/api/models/" + urllib.parse.quote(repo, safe="/") + "/revision/main?blobs=true"
                try:
                    info = fetch_json(url)
                except (OSError, ValueError, KeyError):
                    return []
                if info.get("gated") or not re.fullmatch(r"[0-9a-f]{40}", info.get("sha", "")):
                    return []
                pipeline = info.get("pipeline_tag") or summary.get("pipeline_tag") or "Unknown"
                if pipeline not in ("text-generation", "conversational", "image-text-to-text", "Unknown"):
                    return []
                lower = repo.lower()
                minimum = max((build for family, build in MIN_ENGINE_BUILD.items() if family in lower), default=0)
                artifacts = artifact_sets(info.get("siblings", []), info.get("config"))
                return [{"id": candidate_id(repo, entry["file"]), "name": repo.split("/", 1)[1],
                         "repo": repo, "file": entry["file"], "files": entry["files"],
                         "format": entry["format"], "quantization": entry.get("quantization", ""),
                         "revision": info["sha"], "sha256": entry["sha256"],
                         "size": entry["size"], "size_gb": round(entry["size"] / 1e9, 1), "pipeline": pipeline,
                         "ablit": any(word in lower for word in ("abliterat", "uncensor", "unbound")),
                         "min_engine_build": minimum,
                         "runtime": ("gpu" if hardware.get("gpu_provider") in (None, "nvidia") and hardware.get("vram_mib") and entry["size"] + 4 * 2**30 <= hardware["vram_mib"] * 2**20 else
                                     "vulkan" if entry["format"].startswith("gguf") and hardware.get("gpu_provider") in ("amd", "intel") and hardware.get("render_devices") else "cpu"),
                         "disk_fits": entry["size"] * 1.2 <= hardware.get("disk_free_gib", 0) * 2**30,
                         "fits": (entry["size"] + 4 * 2**30 <= (hardware.get("vram_mib") or 0) * 2**20 or
                                  entry["size"] + 4 * 2**30 <= hardware.get("ram_gib", 0) * 2**30)}
                        for entry in artifacts]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                batches = list(pool.map(inspect, repos[:20]))
            items = [item for batch in batches for item in batch]
            # Exact repository searches show all verified variants; broad searches
            # keep a couple of small variants per repository for useful diversity.
            if not re.fullmatch(REPO_PATTERN, query):
                counts = {}
                limited = []
                for item in items:
                    if counts.get(item["repo"], 0) >= 2:
                        continue
                    counts[item["repo"]] = counts.get(item["repo"], 0) + 1
                    limited.append(item)
                items = limited
            with self.connect() as db:
                db.execute("UPDATE model_searches SET status='completed', ended_at=?, items_json=? WHERE id=?",
                           (utc_now(), json.dumps(items[:SEARCH_RESULTS]), run_id))
        except Exception as exc:
            with self.connect() as db:
                db.execute("UPDATE model_searches SET status='failed', ended_at=?, error=? WHERE id=?",
                           (utc_now(), f"{type(exc).__name__}: {str(exc)[:180]}", run_id))
        finally:
            with self.lock:
                self.busy = False

    def maybe_refresh(self, hardware=None):
        hardware = hardware or self.model_manager.hardware()
        current = self.latest()
        if current["status"] == "running":
            return
        checked = current.get("started_at")
        if checked:
            try:
                age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(checked)
                retry_after = dt.timedelta(minutes=15) if current["status"] == "failed" else dt.timedelta(hours=24)
                if current.get("hardware_fingerprint") == hardware.get("fingerprint") and age < retry_after:
                    return
            except ValueError:
                pass
        self.start_refresh("", hardware)

    def start_refresh(self, query="", hardware=None):
        query = query.strip()
        if len(query) > 64 or (query and not re.fullmatch(r"[A-Za-z0-9 _.-]+", query)):
            raise ValueError("Search must be at most 64 letters, digits, spaces, periods, dashes or underscores")
        with self.lock:
            if self.busy:
                raise RuntimeError("Catalog refresh already running")
            run_id = str(uuid.uuid4())
            hardware = hardware or self.model_manager.hardware()
            with self.connect() as db:
                db.execute("INSERT INTO catalog_refreshes(id,started_at,query,status,hardware_fingerprint) VALUES(?,?,?,?,?)",
                           (run_id, utc_now(), query, "running", hardware.get("fingerprint")))
            self.busy = True
            threading.Thread(target=self._refresh, args=(run_id, query, hardware), daemon=True).start()
            return run_id

    def _refresh(self, run_id, query, hardware=None):
        try:
            hardware = hardware or self.model_manager.hardware()
            vram_bytes = (hardware.get("vram_mib") or 0) * 2**20
            disk_bytes = hardware.get("disk_free_gib", 0) * 2**30
            summaries = []
            for author in AUTHORS:
                params = {"author": author, "search": query or "GGUF",
                          "sort": "createdAt", "direction": "-1", "limit": 50}
                url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(params)
                for row in fetch_json(url):
                    repo = row.get("id", "")
                    if (isinstance(repo, str) and repo.startswith(author + "/")
                            and repo.lower().endswith("-gguf") and not row.get("gated")):
                        summaries.append(row)
            summaries.sort(key=lambda row: row.get("createdAt") or "", reverse=True)
            if query:
                # A popular family may have dozens of new remixes. Keep its
                # shorter, direct GGUF names visible in the ten search results.
                summaries.sort(key=lambda row: (len(row["id"].split("/", 1)[1]),
                                                AUTHORS.index(row["id"].split("/", 1)[0])))

            def inspect(summary):
                repo = summary["id"]
                url = "https://huggingface.co/api/models/" + urllib.parse.quote(repo, safe="/") + "/revision/main?blobs=true"
                try:
                    info = fetch_json(url)
                except (OSError, ValueError, KeyError):
                    return None
                if info.get("gated") or not re.fullmatch(r"[0-9a-f]{40}", info.get("sha", "")):
                    return None
                selected = selected_gguf(info.get("siblings", []))
                if not selected:
                    return None
                _, size, filename, digest = selected
                if size + 4 * 2**30 > vram_bytes or size * 1.2 > disk_bytes:
                    return None
                lower = repo.lower()
                minimum = max((build for family, build in MIN_ENGINE_BUILD.items() if family in lower), default=0)
                return {"id": candidate_id(repo, filename), "name": repo.split("/", 1)[1].removesuffix("-GGUF").replace("_", " "),
                        "repo": repo, "file": filename, "revision": info["sha"], "sha256": digest,
                        "size": size, "size_gb": round(size / 1e9, 1),
                        "created_at": summary.get("createdAt"), "pipeline": summary.get("pipeline_tag") or "Unknown",
                        "ablit": any(word in lower for word in ("abliterat", "uncensor", "unbound")),
                        "min_engine_build": minimum}

            # Inspect newest repos first. A search such as "Inkling" narrows this set.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                items = [item for item in pool.map(inspect, summaries[:80]) if item]
            items.sort(key=lambda item: item["created_at"] or "", reverse=True)
            if query:
                items.sort(key=lambda item: (len(item["repo"].split("/", 1)[1]),
                                             AUTHORS.index(item["repo"].split("/", 1)[0])))
            with self.connect() as db:
                db.execute("UPDATE catalog_refreshes SET status='completed', ended_at=?, items_json=? WHERE id=?",
                           (utc_now(), json.dumps(items[:MAX_RESULTS]), run_id))
        except Exception as exc:
            with self.connect() as db:
                db.execute("UPDATE catalog_refreshes SET status='failed', ended_at=?, error=? WHERE id=?",
                           (utc_now(), f"{type(exc).__name__}: {str(exc)[:180]}", run_id))
        finally:
            with self.lock:
                self.busy = False

    def candidate(self, candidate_id_value):
        if not re.fullmatch(r"discovered-[0-9a-f]{16}", candidate_id_value or ""):
            return None
        item = next((row for row in self.latest_search()["items"] + self.latest()["items"]
                     if row["id"] == candidate_id_value), None)
        if not item or item["installed"]:
            return None
        if (not re.fullmatch(REPO_PATTERN, item["repo"]) or
                not (SAFE_PATH.fullmatch(item["file"]) and item["file"].lower().endswith(".gguf") or
                     item.get("format") == "safetensors" and item["file"] == "config.json") or
                candidate_id(item["repo"], item["file"]) != item["id"]):
            return None
        return {**item, "active_b": 0, "moe": False, "features": "Model capabilities unverified", "priority": 0}
