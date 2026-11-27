"""Curated image/video model bundles for the installed ComfyUI service."""
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid

from benchmarks import docker_api, docker_stream_text
from model_catalog import DOWNLOAD_IMAGE


def file(repo, source, destination):
    return (repo, source, destination)


MEDIA_CATALOG = (
    {"id": "sdxl-base", "name": "SDXL 1.0 Base", "kind": "Image", "use": "General images and image-to-image editing", "vram": 8, "ram": 16, "priority": 81,
     "workflow": "https://comfyanonymous.github.io/ComfyUI_examples/sdxl/", "files": (file("stabilityai/stable-diffusion-xl-base-1.0", "sd_xl_base_1.0.safetensors", "checkpoints/sd_xl_base_1.0.safetensors"),)},
    {"id": "sdxl-turbo", "name": "SDXL Turbo", "kind": "Image", "use": "Fast previews and rough concepts", "vram": 8, "ram": 16, "priority": 77,
     "workflow": "https://comfyanonymous.github.io/ComfyUI_examples/sdturbo/", "files": (file("stabilityai/sdxl-turbo", "sd_xl_turbo_1.0_fp16.safetensors", "checkpoints/sd_xl_turbo_1.0_fp16.safetensors"),)},
    {"id": "animagine-xl-4", "name": "Animagine XL 4.0", "kind": "Image", "use": "Illustration and anime art", "vram": 8, "ram": 16, "priority": 74,
     "workflow": "https://huggingface.co/cagliostrolab/animagine-xl-4.0", "files": (file("cagliostrolab/animagine-xl-4.0", "animagine-xl-4.0.safetensors", "checkpoints/animagine-xl-4.0.safetensors"),)},
    {"id": "juggernaut-xl-v9", "name": "Juggernaut XL v9", "kind": "Image", "use": "Photorealistic SDXL images", "vram": 8, "ram": 16, "priority": 82,
     "workflow": "https://comfyanonymous.github.io/ComfyUI_examples/sdxl/", "files": (file("RunDiffusion/Juggernaut-XL-v9", "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors", "checkpoints/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"),)},
    {"id": "flux1-schnell-fp8", "name": "FLUX.1 Schnell FP8", "kind": "Image", "use": "Fast prompt-based images; single checkpoint", "vram": 20, "ram": 32, "priority": 88,
     "workflow": "https://huggingface.co/Comfy-Org/Flux-1-Schnell", "files": (file("Comfy-Org/Flux-1-Schnell", "flux1-schnell-fp8.safetensors", "checkpoints/flux1-schnell-fp8.safetensors"),)},
    {"id": "flux2-klein-4b", "name": "FLUX.2 Klein 4B", "kind": "Image", "use": "Image generation and reference-guided editing", "vram": 18, "ram": 32, "priority": 93,
     "workflow": "https://docs.comfy.org/tutorials/flux/flux-2-klein", "files": (
         file("Comfy-Org/Flux-2-Klein-4B", "split_files/diffusion_models/flux-2-klein-4b.safetensors", "diffusion_models/flux-2-klein-4b.safetensors"),
         file("Comfy-Org/Flux-2-Klein-4B", "split_files/text_encoders/qwen_3_4b.safetensors", "text_encoders/qwen_3_4b.safetensors"),
         file("Comfy-Org/Flux-2-Klein-4B", "split_files/vae/flux2-vae.safetensors", "vae/flux2-vae.safetensors"))},
    {"id": "z-image-turbo", "name": "Z-Image Turbo", "kind": "Image", "use": "Fast photorealism and English/Chinese text", "vram": 16, "ram": 32, "priority": 91,
     "workflow": "https://docs.comfy.org/tutorials/image/z-image/z-image-turbo", "files": (
         file("Comfy-Org/Z-Image-Turbo", "split_files/diffusion_models/z_image_turbo_bf16.safetensors", "diffusion_models/z_image_turbo_bf16.safetensors"),
         file("Comfy-Org/Z-Image-Turbo", "split_files/text_encoders/qwen_3_4b.safetensors", "text_encoders/qwen_3_4b.safetensors"),
         file("Comfy-Org/Z-Image-Turbo", "split_files/vae/ae.safetensors", "vae/ae.safetensors"))},
    {"id": "z-image-turbo-ablit", "name": "Z-Image Turbo + abliterated Qwen encoder", "kind": "Image", "use": "Fast photorealism with an abliterated prompt encoder", "vram": 16, "ram": 32, "priority": 90, "ablit": True,
     "workflow": "https://docs.comfy.org/tutorials/image/z-image/z-image-turbo", "files": (
         file("Comfy-Org/Z-Image-Turbo", "split_files/diffusion_models/z_image_turbo_bf16.safetensors", "diffusion_models/z_image_turbo_bf16.safetensors"),
         file("BoyoDiffusion/qwen_abliterated_fp8_e4m3fn.safetensors", "qwen_abliterated_fp8_e4m3fn.safetensors", "text_encoders/qwen_abliterated_fp8_e4m3fn.safetensors"),
         file("Comfy-Org/Z-Image-Turbo", "split_files/vae/ae.safetensors", "vae/ae.safetensors"))},
    {"id": "wan22-5b", "name": "Wan 2.2 TI2V 5B", "kind": "Video", "use": "Text-to-video and image-to-video clips", "vram": 20, "ram": 32, "priority": 96,
     "workflow": "https://docs.comfy.org/tutorials/video/wan/wan2_2", "files": (
         file("Comfy-Org/Wan-2.2", "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors", "diffusion_models/wan2.2_ti2v_5B_fp16.safetensors"),
         file("Comfy-Org/Wan-2.2", "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
         file("Comfy-Org/Wan-2.2", "split_files/vae/wan2.2_vae.safetensors", "vae/wan2.2_vae.safetensors"))},
    {"id": "wan21-1b", "name": "Wan 2.1 T2V 1.3B", "kind": "Video", "use": "Lower-memory text-to-video experiments", "vram": 12, "ram": 24, "priority": 72,
     "workflow": "https://comfyanonymous.github.io/ComfyUI_examples/wan/", "files": (
         file("Comfy-Org/Wan-2.1", "split_files/diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors", "diffusion_models/wan2.1_t2v_1.3B_fp16.safetensors"),
         file("Comfy-Org/Wan-2.1", "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", "text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors"),
         file("Comfy-Org/Wan-2.1", "split_files/vae/wan_2.1_vae.safetensors", "vae/wan_2.1_vae.safetensors"))},
    {"id": "ltx-video-2b", "name": "LTX-Video 2B v0.9.5", "kind": "Video", "use": "Lightweight text-to-video and image-to-video", "vram": 16, "ram": 24, "priority": 80,
     "workflow": "https://comfyanonymous.github.io/ComfyUI_examples/ltxv/", "files": (
         file("Lightricks/LTX-Video", "ltx-video-2b-v0.9.5.safetensors", "checkpoints/ltx-video-2b-v0.9.5.safetensors"),
         file("comfyanonymous/flux_text_encoders", "t5xxl_fp16.safetensors", "text_encoders/t5xxl_fp16.safetensors"))},
)
MEDIA_BY_ID = {item["id"]: item for item in MEDIA_CATALOG}
ALLOWED_FOLDERS = {"checkpoints", "diffusion_models", "text_encoders", "vae"}

# Rough RTX 3090 planning ranges, not measured on this host. Image: one 1024x1024
# output; video: 30 seconds at 24 fps assembled from short model-native clips.
# Actual workflows, step counts, offloading, and upscaling can change runtime greatly.
ESTIMATES_3090 = {
    "sdxl-base": ("~6–20 s", "~5–18 s", "NA", "NA"),
    "sdxl-turbo": ("~1–5 s", "~1–5 s", "NA", "NA"),
    "animagine-xl-4": ("~6–20 s", "NA", "NA", "NA"),
    "juggernaut-xl-v9": ("~6–20 s", "~5–18 s", "NA", "NA"),
    "flux1-schnell-fp8": ("~5–20 s", "NA", "NA", "NA"),
    "flux2-klein-4b": ("~5–20 s", "~5–25 s", "NA", "NA"),
    "z-image-turbo": ("~6–25 s", "NA", "NA", "NA"),
    "z-image-turbo-ablit": ("~6–25 s", "NA", "NA", "NA"),
    "wan22-5b": ("NA", "NA", "~30–90 min; 6 clips", "Upscale required; unmeasured"),
    "wan21-1b": ("NA", "NA", "720p not recommended; unmeasured", "Upscale required; unmeasured"),
    "ltx-video-2b": ("NA", "NA", "~15–60 min; 4+ clips", "Upscale required; unmeasured"),
}


def estimated_benchmarks(candidate, hardware):
    gpu = str(hardware.get("gpu") or "").lower()
    if "rtx 3090" not in gpu or hardware.get("vram_mib", 0) < 24 * 1024:
        return ("Uncalibrated",) * 4
    return ESTIMATES_3090[candidate["id"]]


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_destination(destination):
    parts = destination.split("/")
    return len(parts) == 2 and parts[0] in ALLOWED_FOLDERS and bool(re.fullmatch(r"[A-Za-z0-9_.-]+\.safetensors", parts[1]))


class MediaManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.path = os.environ["MEDIA_MODEL_DIR"]
        self.host_path = os.environ["MEDIA_HOST_DIR"]
        self.lock = threading.Lock()
        self.busy = False
        self.pending_installs = []
        self.reserved_bytes = 0
        self.cache = {}
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS media_jobs (
                id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, status TEXT NOT NULL, stage TEXT NOT NULL, error TEXT)""")
            db.execute("UPDATE media_jobs SET status='interrupted', stage='controller restarted', ended_at=? WHERE status IN ('queued','running')", (utc_now(),))

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=20)
        db.row_factory = sqlite3.Row
        return db

    def jobs(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM media_jobs ORDER BY started_at DESC, rowid DESC LIMIT 10")]

    def _job(self, job_id, **values):
        with self.connect() as db:
            columns = ", ".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE media_jobs SET {columns} WHERE id=?", (*values.values(), job_id))

    def _repo(self, repo):
        cached = self.cache.get(repo)
        if cached and time.monotonic() - cached[0] < 1800:
            return cached[1]
        url = "https://huggingface.co/api/models/" + urllib.parse.quote(repo, safe="/") + "/revision/main?blobs=true"
        request = urllib.request.Request(url, headers={"User-Agent": "local-ai-model-switcher/1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            info = json.load(response)
        if info.get("gated") or not re.fullmatch(r"[0-9a-f]{40}", info.get("sha", "")):
            raise ValueError(f"Repository unavailable or gated: {repo}")
        self.cache[repo] = (time.monotonic(), info)
        return info

    def metadata(self, candidate):
        files = []
        for repo, source, destination in candidate["files"]:
            if not safe_destination(destination) or not re.fullmatch(r"[A-Za-z0-9_.\-/]+\.safetensors", source) or ".." in source.split("/"):
                raise ValueError("Unsafe catalog path")
            info = self._repo(repo)
            blob = next((row for row in info.get("siblings", []) if row.get("rfilename") == source), None)
            if not blob:
                raise ValueError(f"Missing Hugging Face file: {repo}/{source}")
            size = blob.get("size") or (blob.get("lfs") or {}).get("size")
            digest = (blob.get("lfs") or {}).get("sha256")
            if not isinstance(size, int) or not 100_000_000 < size < 35_000_000_000 or not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
                raise ValueError(f"Missing verified metadata: {repo}/{source}")
            files.append({"repo": repo, "source": source, "destination": destination,
                          "revision": info["sha"], "size": size, "sha256": digest})
        return files

    def _destination(self, item):
        path = os.path.join(self.path, item["destination"])
        base = os.path.realpath(self.path)
        parent = os.path.realpath(os.path.dirname(path))
        if os.path.islink(path) or not parent.startswith(base + os.sep):
            raise ValueError("Unsafe media model destination")
        return path

    def recommendations(self, hardware, prefer_ablit=True):
        def get(candidate):
            try:
                return candidate, self.metadata(candidate)
            except (OSError, ValueError, KeyError):
                return candidate, None
        chosen = tuple(candidate for candidate in MEDIA_CATALOG
                       if candidate["id"] != ("z-image-turbo" if prefer_ablit else "z-image-turbo-ablit"))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            fetched = list(pool.map(get, chosen))
        disk_free = shutil.disk_usage(self.path).free
        suggestions = []
        for candidate, files in fetched:
            if not files:
                continue
            if not hardware["vram_mib"] or hardware["vram_mib"] < candidate["vram"] * 1024 or hardware["ram_gib"] < candidate["ram"]:
                continue
            conflicts = [item for item in files if os.path.lexists(os.path.join(self.path, item["destination"]))
                         and (not os.path.isfile(self._destination(item)) or os.path.getsize(self._destination(item)) != item["size"])]
            missing = [item for item in files if not os.path.isfile(self._destination(item)) or os.path.getsize(self._destination(item)) != item["size"]]
            needed = sum(item["size"] for item in missing)
            if needed * 1.15 > disk_free:
                continue
            suggestions.append({**candidate, "installed": not missing, "conflict": bool(conflicts),
                                "ablit": candidate.get("ablit", False),
                                "estimates": estimated_benchmarks(candidate, hardware),
                                "total_gb": round(sum(item["size"] for item in files) / 1e9, 1),
                                "download_gb": round(needed / 1e9, 1), "files_count": len(files)})
        suggestions.sort(key=lambda item: (-item["priority"], item["name"]))
        return suggestions[:10]

    def start_install(self, candidate_id, hardware):
        candidate = MEDIA_BY_ID.get(candidate_id)
        if not candidate:
            raise ValueError("Unknown media model")
        with self.lock:
            with self.connect() as db:
                if db.execute("SELECT 1 FROM media_jobs WHERE candidate_id=? AND status IN ('queued','running')",
                              (candidate_id,)).fetchone():
                    raise ValueError("This media model is already queued or downloading")
            if not hardware["vram_mib"] or hardware["vram_mib"] < candidate["vram"] * 1024 or hardware["ram_gib"] < candidate["ram"]:
                raise ValueError("This model is not recommended for the detected hardware")
            files = self.metadata(candidate)
            conflicts = [item for item in files if os.path.lexists(os.path.join(self.path, item["destination"]))
                         and (not os.path.isfile(self._destination(item)) or os.path.getsize(self._destination(item)) != item["size"])]
            if conflicts:
                raise ValueError("A ComfyUI file has the same name but a different size; move it before installing")
            missing = [item for item in files if not os.path.isfile(self._destination(item)) or os.path.getsize(self._destination(item)) != item["size"]]
            if not missing:
                raise ValueError("All files are already installed")
            reserve = int(sum(item["size"] for item in missing) * 1.15)
            if reserve + self.reserved_bytes > shutil.disk_usage(self.path).free:
                raise ValueError("Insufficient media model storage")
            job_id = str(uuid.uuid4())
            with self.connect() as db:
                db.execute("INSERT INTO media_jobs(id,candidate_id,started_at,status,stage) VALUES(?,?,?,?,?)",
                           (job_id, candidate_id, utc_now(), "queued", "queued behind current download" if self.busy else "queued"))
            self.reserved_bytes += reserve
            if self.busy:
                self.pending_installs.append((job_id, missing, reserve))
            else:
                self.busy = True
                threading.Thread(target=self._install, args=(job_id, missing, reserve), daemon=True).start()
            return job_id

    def _verify(self, path, item):
        if os.path.getsize(path) != item["size"]:
            raise ValueError("File size does not match Hugging Face: " + item["destination"])
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while chunk := stream.read(4 * 2**20):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise ValueError("SHA256 does not match Hugging Face: " + item["destination"])

    def _install(self, job_id, files, reserve=0):
        try:
            self._job(job_id, status="running", stage="checking existing files")
            for number, item in enumerate(files, 1):
                destination = self._destination(item)
                if os.path.exists(destination):
                    self._verify(destination, item)
                    continue
                repo_folder = item["repo"].replace("/", "--")
                staged_root = ".media_downloads/" + repo_folder
                stage = os.path.join(self.path, staged_root, item["source"])
                self._job(job_id, stage=f"downloading {number}/{len(files)}: {os.path.basename(destination)}")
                name = "local-ai-media-download-" + job_id[:12] + "-" + str(number)
                config = {"Image": DOWNLOAD_IMAGE, "Entrypoint": ["hf"],
                          "Cmd": ["download", item["repo"], item["source"], "--revision", item["revision"],
                                  "--local-dir", "/models/" + staged_root],
                          "Env": ["HF_HOME=/tmp/hf"],
                          "HostConfig": {"Binds": [self.host_path + ":/models"], "NetworkMode": "bridge", "Memory": 8 * 2**30}}
                container = None
                try:
                    container = docker_api("POST", "/containers/create?name=" + name, config)["Id"]
                    docker_api("POST", f"/containers/{container}/start")
                    for _ in range(1440):
                        state = docker_api("GET", f"/containers/{container}/json")["State"]
                        if not state.get("Running"):
                            if state.get("ExitCode") != 0:
                                raw = docker_api("GET", f"/containers/{container}/logs?stdout=1&stderr=1&tail=15")
                                raise RuntimeError("Download failed: " + docker_stream_text(raw)[-400:])
                            break
                        time.sleep(5)
                    else:
                        raise TimeoutError("Download exceeded two hours")
                finally:
                    if container:
                        docker_api("DELETE", f"/containers/{container}?force=1")
                self._job(job_id, stage=f"verifying {number}/{len(files)}: {os.path.basename(destination)}")
                self._verify(stage, item)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                destination = self._destination(item)
                if os.path.exists(destination):
                    self._verify(destination, item)
                else:
                    os.replace(stage, destination)
            self._job(job_id, status="completed", stage="installed; select its workflow in ComfyUI", ended_at=utc_now())
        except Exception as exc:
            self._job(job_id, status="failed", stage="failed", ended_at=utc_now(), error=f"{type(exc).__name__}: {str(exc)[:300]}")
        finally:
            with self.lock:
                self.reserved_bytes = max(0, self.reserved_bytes - reserve)
                if self.pending_installs:
                    args = self.pending_installs.pop(0)
                    threading.Thread(target=self._install, args=args, daemon=True).start()
                else:
                    self.busy = False
