#!/usr/bin/env python3
"""Small, fixed-action model switch. Only nginx should expose this service."""
import html
import http.client
import datetime as dt
from email.parser import BytesParser
from email.policy import default as email_policy
import base64
import json
import io
import zipfile
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import urllib.parse
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from benchmarks import BenchmarkManager, MODELS
import benchmark_views
from model_catalog import BY_ID, ModelManager, model_display_name
from media_catalog import MediaManager, MEDIA_BY_ID
from catalog_discovery import CatalogDiscovery
from engine_updates import EngineUpdates
from context_tuning import ContextTuner, context_presets
from webui_updates import WebUIUpdates
from sharing import SharingManager, FIELDS
from gpu_metrics import GPUMonitor
from parallel_tuning import ParallelTuner
import sharing_views
import model_views
import harness_views
import multimodal_profiles
import image_edit_views

PROFILE_PATH = "/app/data/multimodal/active_profile.json"
PROFILE_LIBRARY_PATH = "/app/data/multimodal/profile_library.json"


def profile_model_info(modes=()):
    installed = {"hf-" + item["candidate_id"]: item for item in MODEL_MANAGER.installed()}
    text = {}
    for mode in modes:
        item = installed.get(mode)
        catalog = BY_ID.get(item["candidate_id"], {}) if item else {}
        name = model_display_name(item) if item else MODELS[mode][0]
        ablit = bool(catalog.get("ablit") or any(word in name.lower()
                     for word in ("abliterat", "ablit", "uncensored", "nsfw")))
        features = catalog.get("features") or "Installed language model; specialization unverified"
        coder = "coder" in name.lower() or "coding" in features.lower()
        text[mode] = {"name": name, "features": features,
                      "description": features + ("; altered alignment" if ablit else ""),
                      "pros": ("Code-focused option" if coder else "Available for local text tasks") +
                              ("; fewer prompt refusals may be possible" if ablit else ""),
                      "cons": ("Alignment changes may affect accuracy and tool use" if ablit else
                               "Task quality should be checked with local benchmarks"),
                      "context_tokens": item.get("context_tokens") if item else None,
                      "ablit": ablit}
    media = Path("/app/media_models") / "checkpoints"
    image = {}
    edit = {}
    for path in media.glob("*.safetensors"):
        match = next((bundle for bundle in MEDIA_BY_ID.values() if any(
            destination == "checkpoints/" + path.name for _, _, destination in bundle["files"])), None)
        name = match["name"] if match else path.stem
        if "inpaint" in path.name.lower():
            edit[path.name] = {"name": name, "description": "Installed mask-based inpainting checkpoint",
                               "pros": "Can fill a selected object mask",
                               "cons": "Compatibility and result quality require a test edit",
                               "rank": 1}
        else:
            image[path.name] = {"name": name,
                                "description": match["use"] if match else "Installed checkpoint; architecture unverified",
                                "pros": "Installed and selectable; no download required",
                                "cons": "Built-in generation currently expects SDXL-style checkpoints",
                                "rank": match.get("priority", 0) if match else 0}
    video = {}
    for path in (Path("/app/media_models") / "diffusion_models").glob("wan2.1_t2v_*.safetensors"):
        known = "1.3b" in path.name.lower()
        video[path.name] = {"name": path.stem.replace("_", " "),
                            "description": "Wan 2.1 text-to-video" + ("; custom variant" if not known else ""),
                            "pros": "Installed and selectable; alternate prompt alignment if provided by its publisher",
                            "cons": "Short silent clips; custom variants require a trial render",
                            "rank": 2 if known else 1}
    asr = {"tiny": {"name": "Whisper tiny", "description": "Speech transcription",
                     "pros": "Fastest and smallest", "cons": "Lowest expected accuracy of the three", "rank": 1},
           "base": {"name": "Whisper base", "description": "Speech transcription",
                     "pros": "Balanced size and accuracy", "cons": "Slower than tiny", "rank": 3},
           "small": {"name": "Whisper small", "description": "Speech transcription",
                      "pros": "More capacity for difficult speech", "cons": "Slowest and largest of the three", "rank": 2}}
    return {"text": text, "image": image, "image_edit": edit, "video": video, "asr": asr}


def profile_recommendations():
    hardware = MODEL_MANAGER.hardware()
    available = {mode for mode, (_, container) in MODELS.items()
                 if mode != "media" and workload_state(container) != "not created"}
    checkpoints = sorted(path.name for path in (Path("/app/media_models") / "checkpoints").glob("*.safetensors"))
    image_checkpoints = [name for name in checkpoints if "inpaint" not in name.lower() and
                         ("xl" in name.lower() or "sdxl" in name.lower())]
    media_ready = (hardware.get("gpu_provider") == "nvidia" and
                   workload_state(MEDIA) != "not created")
    media_models = Path("/app/media_models")
    video_companions = ("text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
                        "vae/wan_2.1_vae.safetensors")
    video_bundles = (sorted(path.name for path in (media_models / "diffusion_models").glob(
        "wan2.1_t2v_*.safetensors")) if media_ready and all(
            (media_models / name).is_file() for name in video_companions) else [])
    rows = multimodal_profiles.recommend(hardware, available, media_ready,
                                         os.environ.get("AI_MULTIMODAL_ENABLED") == "1", checkpoints,
                                         active_mode(), video_bundles, image_checkpoints)
    info = profile_model_info(available)
    for row in rows:
        if row["text_modes"]:
            row["text_mode"] = max(row["text_modes"], key=lambda mode:
                multimodal_profiles.text_fit(info["text"].get(mode, {}), row["id"]))
    rows = multimodal_profiles.profile_rows(rows, multimodal_profiles.load_library(PROFILE_LIBRARY_PATH))
    return hardware, rows


def image_edit_job(ident, kind="image_edit"):
    try:
        if uuid.UUID(ident).version != 4:
            return None
    except (ValueError, TypeError):
        return None
    path = Path("/app/data/multimodal/jobs.sqlite3")
    if not path.is_file():
        return None
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM jobs WHERE id=? AND owner='switcher-admin' AND kind=?",
                         (ident, kind)).fetchone()
    finally:
        db.close()
    return dict(row) if row else None


def image_preview(data, content_type, query):
    values = urllib.parse.parse_qs(query).get("index", ["1"])
    if len(values) != 1 or values[0] not in ("1", "2", "3"):
        raise ValueError("Invalid preview index")
    index = int(values[0])
    if content_type == "image/png":
        if index != 1:
            raise ValueError("No image at that index")
        return data
    if content_type != "application/zip":
        raise ValueError("Unsupported result type")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = f"generated-{index:02}.png"
        if archive.getinfo(name).file_size > 32 * 1024 * 1024:
            raise ValueError("Image preview is too large")
        return archive.read(name)


def usage_window(query, prefix=""):
    """Read a bounded window for the shared and per-key usage views."""
    amount = query.get(prefix + "amount", ["24"])
    unit = query.get(prefix + "unit", ["hours"])
    if len(amount) != 1 or not amount[0].isdecimal() or len(unit) != 1:
        raise ValueError("Choose a valid usage window")
    return int(amount[0]), unit[0]

SOCKET = "/var/run/docker.sock"
LLM = "local-ai-llm"
CPU_TINY = "local-ai-cpu-tiny"
CPU_4B = "local-ai-cpu-4b"
QWEN4B = "local-ai-qwen4b"
HUIHUI = "local-ai-huihui"
FLINT_IQ4 = "local-ai-flint-iq4"
FLINT_Q3 = "local-ai-flint-q3"
DEEPSEEK = "local-ai-deepseek-v4-distill"
QWEN38 = "local-ai-qwen38-ablit"
GEMMA4 = "local-ai-gemma4-ablit"
VISION = "local-ai-vision"
MEDIA = "local-ai-comfyui"
ASSET_PATH = "/app/assets/gpu-workstation.webp"
ORIGIN = os.environ["CONTROL_ORIGIN"]
WEB_URL = os.environ["WEB_URL"]
COMFY_URL = os.environ["COMFY_URL"]
PUBLIC_API_URL = os.environ.get("PUBLIC_API_URL", ORIGIN.rsplit(":", 1)[0] + ":8080/v1")
CSRF = os.environ["CONTROL_CSRF_TOKEN"]
SELECTED_MODES = {value for value in os.environ.get("AI_SELECTED_MODELS", "").split(",") if value}
LOCK = threading.RLock()
LAST_SWITCH_MODE = None
MEDIA_LEASE_PATH = Path("/app/data/multimodal/gpu_lease.json")
try:
    MEDIA_LEASE = json.loads(MEDIA_LEASE_PATH.read_text())
except (OSError, ValueError):
    MEDIA_LEASE = None
BUILTIN_LABELS = {
    "cpu-tiny": "Qwen3 0.6B Q8 CPU (local-cpu-tiny)",
    "cpu-4b": "Qwen3 4B Q4 CPU (local-cpu-4b)",
    "llm": "Qwen3 Coder 30B-A3B (local-coder)",
    "qwen4b": "Qwen3 4B Instruct 2507 (local-qwen4b)",
    "huihui": "Huihui Qwen3.6 35B-A3B Q3_K (local-huihui)",
    "flint-iq4": "Flint 40B-A2.5B IQ4_XS (local-flint-iq4)",
    "flint-q3": "Flint 40B-A2.5B Q3_K_M (local-flint-q3)",
    "deepseek": "DeepSeek V4 Pro distill / Qwen3.5 9B abliterated Q5_K_M (local-deepseek-v4-distill)",
    "qwen38": "Qwen3.8 35B-A3B MoE distill abliterated (local-qwen38-ablit)",
    "gemma4": "Huihui Gemma 4 26B-A4B abliterated Q4_K_M (local-gemma4-ablit)",
    "vision": "Qwen3-VL 30B-A3B (local-vision)",
}


class UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=45)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET)


def docker(method, path):
    conn = UnixConnection()
    try:
        conn.request(method, path, headers={"Host": "localhost", "Content-Length": "0"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Docker API {resp.status}: {body[:240].decode('utf-8', 'replace')}")
        return json.loads(body) if body else None
    finally:
        conn.close()


def running(name):
    try:
        return bool(docker("GET", f"/containers/{name}/json")["State"]["Running"])
    except RuntimeError as exc:
        if "Docker API 404" in str(exc):
            return False
        raise


def workload_state(name):
    try:
        state = docker("GET", f"/containers/{name}/json")["State"]
    except RuntimeError as exc:
        if "Docker API 404" in str(exc):
            return "not created"
        raise
    status = state.get("Status", "unknown")
    if status == "running":
        return state.get("Health", {}).get("Status", "running")
    if status in ("exited", "dead"):
        return f"{status} (exit {state.get('ExitCode', '?')})"
    return status


def switch(target, internal=False):
    global LAST_SWITCH_MODE
    workloads = {mode: container for mode, (_, container) in MODELS.items()}
    if target not in workloads:
        raise ValueError("Unknown mode")
    if SELECTED_MODES and target not in SELECTED_MODES and not target.startswith("hf-"):
        raise ValueError("This workload was not selected at installation")
    if not SELECTED_MODES and workload_state(workloads[target]) == "not created":
        raise ValueError("This workload is not installed")
    if ENGINE_UPDATES.busy or CONTEXT_TUNER.busy or PARALLEL_TUNER.busy or MODEL_MANAGER.busy:
        raise RuntimeError("Wait for the model operation or context change to finish before switching models")
    with LOCK:
        if MEDIA_LEASE and not internal:
            raise RuntimeError("An image job is using the GPU; wait for it to finish")
        LAST_SWITCH_MODE = target
        start_name = workloads[target]
        backup = MODEL_MANAGER.ensure_current_engine(target)
        previous = []
        try:
            previous = [name for name in workloads.values() if name != start_name and running(name)]
            for name in previous:
                docker("POST", f"/containers/{name}/stop?t=30")
            if not running(start_name):
                docker("POST", f"/containers/{start_name}/start")
            MODEL_MANAGER.finish_engine_recreation(target, backup, True)
            try:
                SHARING.settings(MODEL_MANAGER.hardware(), active_server_slots())
            except Exception:
                pass
        except Exception:
            MODEL_MANAGER.finish_engine_recreation(target, backup, False)
            for name in previous:
                try:
                    docker("POST", f"/containers/{name}/start")
                except Exception:
                    pass
            raise


BENCHMARKS = BenchmarkManager(switch)
MODEL_MANAGER = ModelManager(BENCHMARKS.path, os.environ["BENCHMARK_API_KEY"])
MEDIA_MANAGER = MediaManager(BENCHMARKS.path)
DISCOVERY = CatalogDiscovery(BENCHMARKS.path, MODEL_MANAGER)
ENGINE_UPDATES = EngineUpdates(BENCHMARKS.path, os.environ["BENCHMARK_API_KEY"])
CONTEXT_TUNER = ContextTuner(BENCHMARKS.path, MODEL_MANAGER, os.environ["BENCHMARK_API_KEY"])
WEBUI_UPDATES = WebUIUpdates(BENCHMARKS.path)
SHARING = SharingManager(BENCHMARKS.path)
GPU_MONITOR = GPUMonitor()
PARALLEL_TUNER = ParallelTuner(BENCHMARKS.path, MODEL_MANAGER, os.environ["BENCHMARK_API_KEY"])


def active_mode():
    active = [mode for mode, (_, container) in MODELS.items() if running(container)]
    return active[0] if len(active) == 1 else None


def media_lease(action, job_id):
    """Reserve the sole GPU for one ComfyUI job and restore its prior model."""
    global MEDIA_LEASE
    with LOCK:
        if action == "acquire":
            if MEDIA_LEASE:
                if MEDIA_LEASE["job_id"] == job_id:
                    if active_mode() != "media":
                        switch("media", internal=True)
                    return {"state": "leased", "previous": MEDIA_LEASE["previous"]}
                raise RuntimeError("The GPU is reserved for another image job")
            if workload_state(MEDIA) == "not created":
                raise ValueError("ComfyUI is not installed; select it in the installer")
            previous = active_mode()
            MEDIA_LEASE = {"job_id": job_id, "previous": previous}
            MEDIA_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
            MEDIA_LEASE_PATH.write_text(json.dumps(MEDIA_LEASE))
            try:
                if previous != "media":
                    switch("media", internal=True)
            except Exception:
                MEDIA_LEASE = None
                MEDIA_LEASE_PATH.unlink(missing_ok=True)
                raise
            return {"state": "leased", "previous": previous}
        if action == "release":
            if not MEDIA_LEASE or MEDIA_LEASE["job_id"] != job_id:
                raise ValueError("Image job does not own the GPU lease")
            previous = MEDIA_LEASE["previous"]
            if previous and previous != "media":
                switch(previous, internal=True)
            elif previous is None and running(MEDIA):
                docker("POST", f"/containers/{MEDIA}/stop?t=30")
            MEDIA_LEASE = None
            MEDIA_LEASE_PATH.unlink(missing_ok=True)
            return {"state": "released", "restored": previous}
        raise ValueError("Unknown GPU lease action")


def workload_snapshot():
    """Report readiness from the public model API, not just Docker's running flag."""
    mode = active_mode() or LAST_SWITCH_MODE
    if not mode or mode not in MODELS:
        return {"mode": None, "state": "idle", "detail": "No model is running", "api_id": None}
    api_id, container = MODELS[mode]
    state = workload_state(container)
    if state.startswith(("exited", "dead")) or state in ("not created", "created", "unhealthy"):
        return {"mode": mode, "state": "failed", "detail": "Container " + state, "api_id": api_id}
    if state not in ("running", "healthy", "starting"):
        return {"mode": mode, "state": "loading", "detail": "Container " + state, "api_id": api_id}
    if mode == "media":
        ready = state == "healthy"
        native_context = None
    else:
        ready = False
        native_context = None
        try:
            request = urllib.request.Request("http://nginx:8080/v1/models",
                                             headers={"Authorization": "Bearer " + os.environ["BENCHMARK_API_KEY"]})
            with urllib.request.urlopen(request, timeout=3) as response:
                item = next((item for item in json.load(response).get("data", [])
                             if isinstance(item, dict) and item.get("id") == api_id), None)
                ready = bool(item)
                if item:
                    candidate = (item.get("meta") or {}).get("n_ctx_train")
                    if isinstance(candidate, int) and candidate > 0:
                        native_context = candidate
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return {"mode": mode, "state": "ready" if ready else "loading",
            "detail": ("ComfyUI is ready; the language model API is unavailable until you select a language model"
                       if mode == "media" and ready else "Ready for requests" if ready else
                       "Model is loading; API requests may fail until ready"),
            "api_id": api_id, "native_context_tokens": native_context}


def active_server_slots():
    mode = active_mode()
    if not mode or mode == "media":
        return 1
    try:
        command = docker("GET", "/containers/" + MODELS[mode][1] + "/json")["Config"]["Cmd"]
        for flag in ("--parallel", "--max-num-seqs"):
            if flag in command:
                return max(1, int(command[command.index(flag) + 1]))
    except (RuntimeError, KeyError, ValueError, IndexError):
        pass
    return 1


def verify_sharing_settings(expected):
    request = urllib.request.Request("http://fairness-gateway:8002/gateway/health",
                                     headers={"Authorization": "Bearer " + os.environ["BENCHMARK_API_KEY"]})
    with urllib.request.urlopen(request, timeout=5) as response:
        reported = json.load(response)["policy"]
    if any(reported.get(key) != expected[key] for key in FIELDS):
        raise RuntimeError("Gateway did not load the requested sharing limits")


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, page, status=200, embeddable=False):
        data = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'" if embeddable else "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        """Keep controller errors in the same visual system as the switcher."""
        reason = self.responses.get(code, ("Request failed", ""))[0]
        detail = message or reason
        body = (f'<section class="card"><div class="eyebrow">HTTP {code}</div>'
                f'<h2>{html.escape(reason)}</h2><p>{html.escape(str(detail))}</p>'
                '<p><a href="/">Return to AI Model Switcher</a></p></section>')
        self._send_html(benchmark_views.shell("Request failed", body), code)

    def _page(self, message="", status=200):
        try:
            cpu_tiny_on, cpu_4b_on, llm_on, qwen4b_on, huihui_on, flint_iq4_on, flint_q3_on, deepseek_on, qwen38_on, gemma4_on, vision_on, media_on = (
                running(CPU_TINY), running(CPU_4B), running(LLM), running(QWEN4B), running(HUIHUI),
                running(FLINT_IQ4), running(FLINT_Q3), running(DEEPSEEK), running(QWEN38), running(GEMMA4), running(VISION), running(MEDIA)
            )
            huihui_state = workload_state(HUIHUI)
            cpu_tiny_state = workload_state(CPU_TINY)
            cpu_4b_state = workload_state(CPU_4B)
            llm_state = workload_state(LLM)
            vision_state = workload_state(VISION)
            qwen4b_state = workload_state(QWEN4B)
            flint_iq4_state = workload_state(FLINT_IQ4)
            flint_q3_state = workload_state(FLINT_Q3)
            deepseek_state = workload_state(DEEPSEEK)
            qwen38_state = workload_state(QWEN38)
            gemma4_state = workload_state(GEMMA4)
            media_state = workload_state(MEDIA)
            managed = MODEL_MANAGER.installed()
        except Exception as exc:
            cpu_tiny_on = cpu_4b_on = llm_on = qwen4b_on = huihui_on = flint_iq4_on = flint_q3_on = deepseek_on = qwen38_on = gemma4_on = vision_on = media_on = False
            cpu_tiny_state = cpu_4b_state = llm_state = vision_state = qwen4b_state = huihui_state = flint_iq4_state = flint_q3_state = deepseek_state = qwen38_state = gemma4_state = media_state = "unknown"
            message = f"Docker status error: {exc}"
            status = 503
            managed = []
        active = [label for label, on in (("Qwen3 0.6B CPU", cpu_tiny_on), ("Qwen3 4B CPU", cpu_4b_on),
                  ("Coder", llm_on), ("Qwen3 4B", qwen4b_on),
                  ("Huihui Qwen3.6", huihui_on), ("Flint IQ4_XS", flint_iq4_on),
                  ("Flint Q3_K_M", flint_q3_on), ("Vision", vision_on),
                  ("DeepSeek V4 distill Q5", deepseek_on),
                  ("Qwen3.8 MoE distill abliterated", qwen38_on),
                  ("Gemma 4 26B-A4B abliterated", gemma4_on),
                  ("ComfyUI", media_on)) if on]
        active.extend(model_display_name(item) for item in managed if running(item["container"]))
        mode = active[0] if len(active) == 1 else "No model running" if not active else "Conflict: " + ", ".join(active)
        h = lambda s: html.escape(s, quote=True)
        try:
            api_port = urllib.parse.urlsplit(PUBLIC_API_URL).port
        except ValueError:
            api_port = None
        api_port_note = f'<span class="api-port">Port: {api_port}</span>' if api_port else ''
        try:
            snapshot = workload_snapshot()
        except (OSError, RuntimeError, ValueError, KeyError):
            snapshot = {"state": "unavailable", "detail": "Could not check model status"}
        badge_label = {"ready": "Ready to use", "loading": "Loading model", "failed": "Model failed",
                       "idle": "No active workload", "unavailable": "Status unavailable"}.get(snapshot["state"], "Status unavailable")
        try:
            fingerprint = MODEL_MANAGER.hardware().get("fingerprint")
            contexts = CONTEXT_TUNER.contexts(fingerprint)
        except (RuntimeError, ValueError, KeyError):
            contexts = {}
        def context_text(which):
            item = contexts.get(which) or {}
            tokens = item.get("tokens")
            return f'{tokens:,} tokens ({item["state"]})' if tokens else item.get("state", "unavailable")
        builtin_states = {"cpu-tiny": cpu_tiny_state, "cpu-4b": cpu_4b_state, "llm": llm_state,
                          "qwen4b": qwen4b_state, "huihui": huihui_state,
                          "flint-iq4": flint_iq4_state, "flint-q3": flint_q3_state,
                          "deepseek": deepseek_state, "qwen38": qwen38_state,
                          "gemma4": gemma4_state, "vision": vision_state, "media": media_state}
        def available_builtin(which):
            return which in SELECTED_MODES if SELECTED_MODES else builtin_states.get(which) != "not created"
        model_labels = {**{key: value for key, value in BUILTIN_LABELS.items()
                           if available_builtin(key)},
                        **{"hf-" + item["candidate_id"]: model_display_name(item) + " (Hugging Face; API " + item["alias"] + ")" for item in managed}}
        model_options = "".join(
            f'<option value="{h(which)}" {"selected" if contexts.get(which, {}).get("running") else ""}>'
            f'{h(label)} · {h(context_text(which))}</option>'
            for which, label in model_labels.items())
        context_rows = "".join(
            f'<tr><td>{h(label)}</td><td>{h(context_text(which))}</td></tr>'
            for which, label in model_labels.items())
        context_status = CONTEXT_TUNER.status()
        context_active = None
        if not (CONTEXT_TUNER.busy or ENGINE_UPDATES.busy or PARALLEL_TUNER.busy):
            try:
                context_active = CONTEXT_TUNER.active(active_mode())
            except (RuntimeError, ValueError, KeyError):
                pass
        context_job = context_status.get("job") or {}
        context_note = (f'<p>Latest context test: <strong>{h(context_job.get("status", ""))}</strong> · '
                        f'{h(context_job.get("mode", ""))}: {h(str(context_job.get("previous_tokens", "")))} → '
                        f'{h(str(context_job.get("requested_tokens", "")))} tokens · '
                        f'{h(context_job.get("stage", ""))} · {h(context_job.get("error") or "")}</p>') if context_job else ""
        if context_active:
            native_limit = (snapshot.get("native_context_tokens")
                            if snapshot.get("mode") == context_active["mode"] else None)
            choices = context_presets(context_active["tokens"], native_limit)
            options = "".join(f'<option value="{tokens}">{tokens:,} tokens</option>' for tokens in choices)
            limit_note = (f'Reported native limit: {native_limit:,} tokens.' if native_limit else
                          'The model did not report a native context limit.')
            context_form = (f'<p>Loaded model: <strong>{h(context_active["alias"])}</strong> · '
                            f'Context: <strong>{h(context_text(context_active["mode"]))}</strong></p>'
                            f'<p class="note">{h(limit_note)} Choose a preset to test; only a successful test is saved.</p>'
                            f'<form method="post" action="/context/tune"><input type="hidden" name="token" value="{h(CSRF)}">'
                            f'<input type="hidden" name="mode" value="{h(context_active["mode"])}">'
                            '<label for="context-tokens">New context size</label><br>'
                            '<select id="context-tokens" name="context_tokens" required>'
                            f'<option value="" selected disabled>Select a preset to test</option>{options}</select> '
                            '<button type="submit">Test and save context</button></form>' if choices else
                            '<p>No different supported context preset is available for this model.</p>')
        else:
            context_form = '<p>Start one model to tune its context.</p>'
        if context_status["busy"]:
            context_form = '<p>Context test running. Progress updates above without reloading this page.</p>'
        states = (("cpu-tiny", "Qwen3 0.6B CPU", cpu_tiny_state),
                  ("cpu-4b", "Qwen3 4B CPU", cpu_4b_state),
                  ("llm", "Qwen3 Coder", llm_state),
                  ("vision", "Qwen3 VL", vision_state),
                  ("qwen4b", "Qwen 4B", qwen4b_state), ("huihui", "Huihui", huihui_state),
                  ("flint-iq4", "Flint IQ4", flint_iq4_state), ("flint-q3", "Flint Q3", flint_q3_state),
                  ("deepseek", "DeepSeek distill", deepseek_state), ("qwen38", "Qwen3.8 distill", qwen38_state),
                  ("gemma4", "Gemma 4", gemma4_state), ("media", "ComfyUI", media_state))
        status_cards = "".join(
            f'<div class="service"><span>{h(label)}</span><strong class="{"ready" if state in ("healthy", "running") else "idle"}">{h(state)}</strong></div>'
            for key, label, state in states if available_builtin(key))
        webui_status = WEBUI_UPDATES.status()
        webui_job = webui_status.get("job") or {}
        webui_card = ""
        webui_result = ""
        if webui_job.get("status") in ("completed", "failed", "interrupted") and not webui_job.get("dismissed_at"):
            try:
                recent = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(webui_job["started_at"]) < dt.timedelta(hours=24)
            except (ValueError, KeyError):
                recent = False
            if recent:
                text = ("Open WebUI update completed. Data backup: " + str(webui_job.get("backup_path", ""))
                        if webui_job["status"] == "completed" else
                        "Open WebUI update " + webui_job["status"] + ": " + str(webui_job.get("error") or webui_job.get("stage")))
                webui_result = (f'<div class="message message-banner" role="status"><p>{h(text)}</p>'
                                f'<form method="post" action="/webui/update/dismiss">'
                                f'<input type="hidden" name="token" value="{h(CSRF)}">'
                                f'<input type="hidden" name="job_id" value="{h(webui_job["id"])}">'
                                '<button type="submit" aria-label="Dismiss Open WebUI update message">Dismiss</button>'
                                '</form></div>')
        if webui_status["update_available"] or webui_status["busy"]:
            webui_action = (f'<form method="post" action="/webui/update"><input type="hidden" name="token" value="{h(CSRF)}">'
                            '<button type="submit">Back up and update Open WebUI</button></form>') if webui_status["update_available"] and not (webui_status["busy"] or webui_status["checking"]) else ''
            progress = (f'<p role="status">Update {h(webui_job.get("status", "running"))}: '
                        f'{h(webui_job.get("stage", "starting"))}</p>') if webui_status["busy"] else ''
            webui_card = ('<section class="card update-card"><div><div class="eyebrow">Software update available</div>'
                          '<h2>Open WebUI</h2><p>A newer container image is available. Updating briefly takes Open WebUI offline. '
                          'The current data volume is backed up first and restored if the new version fails its health check.</p>'
                          '</div><div>' + webui_action + progress + '</div></section>')
        try:
            all_usage = SHARING.usage_summary("all", *usage_window(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query), "all_"))
        except ValueError as exc:
            self.send_error(400, str(exc))
            return
        except Exception as exc:
            all_usage_panel = f'<p class="note" role="status">API usage is temporarily unavailable: {h(type(exc).__name__)}</p>'
        else:
            all_usage_panel = sharing_views.usage_panel(all_usage, "all API keys", embedded=True, action="/")
        gpu_card = ('' if not any(available_builtin(item) for item in ('llm','qwen4b','huihui','flint-iq4','flint-q3','deepseek','qwen38','gemma4','vision','media')) else
                    '<section class="card"><h2>Live GPU status</h2><p class="gpu-note">GPU use and VRAM use over recent samples. Temperature and power are current readings. This panel refreshes every five seconds without reloading the switcher controls.</p><iframe class="gpu-frame" src="/gpu/graph" title="Live GPU utilization and memory graph" loading="lazy"></iframe>' + all_usage_panel + '</section>')
        home_style = """.home-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(19rem,1fr);gap:1rem;align-items:start}.home-grid .card{margin:0}.home-grid select{width:100%;margin:.45rem 0}.home-grid button{margin:.55rem .5rem .2rem 0}.home-actions{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem 1rem}.home-actions form{margin:0}.active-line{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}.active-badge{display:inline-flex;align-items:center;gap:.5rem;background:#1b3a34;border:1px solid #437864;color:#c8efd9;border-radius:99px;padding:.28rem .75rem;font-size:.82rem;font-weight:800}.active-badge:before{content:"";height:.48rem;width:.48rem;border-radius:50%;background:#82d6a2;box-shadow:0 0 0 3px #82d6a226}.services{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.6rem}.service{display:flex;flex-direction:column;gap:.3rem;background:#122126;border:1px solid #30464b;border-radius:.55rem;padding:.75rem}.service span{font-size:.8rem;color:#b5c8c7}.service strong{font-size:.82rem;overflow-wrap:anywhere}.service .ready{color:#a8e0bc}.service .idle{color:#ced8d3}details{margin:1rem 0}summary{cursor:pointer;color:#f3bf80;font-weight:700}details table{font-size:.88rem}.home-footer{display:flex;gap:.6rem;flex-wrap:wrap;margin:1.5rem 0}.home-footer a{padding:.65rem .9rem;border:1px solid #49605e;border-radius:.6rem;text-decoration:none;background:#17282b}.home-footer a:hover{background:#24393a}.message{padding:.75rem 1rem;border-left:3px solid #e9b977;background:#24302d;border-radius:.3rem}@media(max-width:900px){.home-grid{grid-template-columns:1fr}.services{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:480px){.services{grid-template-columns:1fr 1fr}.home-actions button{width:100%}}"""
        home_style += ".update-card{display:grid;grid-template-columns:1fr auto;gap:1rem;align-items:center;border-color:#8a6843;background:linear-gradient(110deg,#27332d,#1a292c)}.update-card h2{margin:.35rem 0}.update-card p{max-width:65rem}.update-card form{margin:0}@media(max-width:740px){.update-card{grid-template-columns:1fr}}"
        home_style += ".message-banner{display:flex;align-items:center;justify-content:space-between;gap:1rem}.message-banner p,.message-banner form{margin:0}.message-banner button{min-height:2.2rem;padding:.3rem .7rem;background:transparent;border-color:#c5a976;color:#f8d9a5;white-space:nowrap}.message-banner button:hover{background:#3a4437}@media(max-width:740px){.message-banner{align-items:flex-start;flex-direction:column}}"
        home_style += ".gpu-frame{width:100%;height:345px;border:0;background:#17262a}.gpu-note{font-size:.9rem;color:#b6c6c4}.all-key-usage{margin-top:1.5rem;padding-top:1.2rem;border-top:1px solid #365056}@media(max-width:740px){.gpu-frame{height:440px}}"
        home_style += ".active-caption{font-size:.83rem;font-weight:750;color:#d4e3db}.active-badge:not(.is-ready){background:#443926;border-color:#987445;color:#ffe1ad}.active-badge:not(.is-ready):before{background:#f0b967;box-shadow:0 0 0 3px #f0b96726}.active-badge.is-failed{background:#492a2a;border-color:#a85b55;color:#ffcbc5}.active-badge.is-failed:before{background:#f28b83}.active-detail{margin:.45rem 0;color:#d5e0db}.api-connection{display:flex;gap:.75rem 1.2rem;flex-wrap:wrap;align-items:end;margin-top:.7rem}.api-connection label{display:grid;gap:.2rem;color:#cad9d5;font-size:.82rem}.api-connection input{width:min(100%,22rem);font-family:ui-monospace,SFMono-Regular,monospace;font-size:.88rem}.api-connection .api-base{width:min(100%,28rem)}.api-note{font-size:.84rem;color:#c4d5d0;margin:.45rem 0 0}"
        home_style += ".api-connection{display:grid;grid-template-columns:minmax(0,12rem) minmax(0,1fr);gap:1rem;align-items:start}.api-connection label{min-width:0;margin:0}.api-connection input[type=text]{width:100%;max-width:none}.api-port{display:block;color:#e8ba78;font-size:.78rem;font-weight:700}@media(max-width:680px){.api-connection{grid-template-columns:1fr}}"
        page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Model Switcher</title><style>{benchmark_views.STYLE}{home_style}{sharing_views.USAGE_STYLE}</style>
<main><nav aria-label="Main navigation"><a href="/">AI Model Switcher</a><a href="/models">Find models</a><a href="/benchmarks">Benchmarks</a><a href="/profiles">Task profiles</a><a href="/image-edit">Image editor</a><a href="/sharing">Client sharing</a><a href="/harnesses">Coding harnesses</a></nav>
<header class="hero"><div class="hero-copy"><div class="eyebrow">Local AI / Control desk</div><h1>AI Model Switcher</h1><p>Manage local workloads and model context from one place.</p><div class="active-line"><span class="active-caption">Active workload</span><span class="active-badge {'is-ready' if snapshot['state'] == 'ready' else 'is-failed' if snapshot['state'] == 'failed' else ''}" data-workload-badge>{h(badge_label)}</span><strong data-workload-label>{h(mode)}</strong></div><p class="active-detail" role="status" data-workload-detail>{h(snapshot['detail'])}</p><div class="api-connection"><label>Stable API model ID<input type="text" readonly value="local-llm-api" data-copy-field aria-label="Copyable stable API model ID"></label><label>OpenAI API base URL<input type="text" class="api-base" readonly value="{h(PUBLIC_API_URL)}" data-copy-field aria-label="Copyable OpenAI API base URL">{api_port_note}</label></div><p class="api-note">Use <code>local-llm-api</code> as the model in your harness to reach the currently loaded language model. Wait for “Ready to use” before sending requests.</p></div></header>
<script src="/assets/operation_poll.js?v=workload-status-1" defer></script>
{'<p class="message" role="status" data-operation-monitor="/operations/status?scope=home">Checking operation status…</p>' if context_status['busy'] or webui_status['checking'] or webui_status['busy'] else ''}
{webui_card}
{webui_result}
{f'<p class="message" role="status">{h(message)}</p>' if message else ''}
<h2>Choose a workload</h2><div class="home-grid"><section class="card"><h3>Language model</h3>
<form method="post" action="/switch"><input type="hidden" name="token" value="{h(CSRF)}"><label for="model">LLM model and saved context</label><br><select id="model" name="mode">{model_options}</select><button type="submit">Use selected model</button></form>
<details><summary>Context for every model before loading</summary><table><tr><th>Model</th><th>Context</th></tr>{context_rows}</table></details>
{'' if not available_builtin('media') else '<div class="home-actions"><span class="note">Image and video workflow</span>'}
{'' if not available_builtin('media') else f'''<form method="post" action="/switch"><input type="hidden" name="token" value="{h(CSRF)}"><button name="mode" value="media">Use ComfyUI</button></form></div>'''}
<p class="note">Switching stops the other model workloads and interrupts active requests. In Open WebUI, choose the matching model after it starts.</p></section>
<section class="card"><h3>Context tuning</h3>{context_form}{context_note}<p class="note">“Tuned” means the value passed a restart and readiness test on this hardware. Testing briefly interrupts requests; a failed test restores the previous setting. Larger context uses more memory and may reduce speed.</p></section></div>
<h2>Service status</h2><div class="services">{status_cards}</div><p><a href="/">Refresh status</a></p>
{gpu_card}
<p class="note">If a service fails, check ai-control.sh huihui-logs or media-logs. If Open WebUI still shows local-coder after switching, set its connection URL to http://nginx:8080/v1 in Admin → Settings → Connections.</p>
<div class="home-footer"><a href="/models">Find models</a><a href="/benchmarks">Benchmarks and results</a><a href="/profiles">Task profiles</a><a href="/image-edit">Image editor</a><a href="/sharing">Client sharing and rate limits</a><a href="/harnesses">Coding harnesses</a><a href="{h(WEB_URL)}">Open WebUI</a>{f'<a href="{h(COMFY_URL)}">ComfyUI</a>' if available_builtin('media') else ''}</div></main></html>"""
        data = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; frame-src 'self'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path)
        if path.path == "/image-edit":
            query = urllib.parse.parse_qs(path.query)
            job_id = query.get("job", [None])[0]
            task = query.get("task", ["edit"])[0]
            if task not in ("edit", "generate") or job_id and not image_edit_job(
                    job_id, "image" if task == "generate" else "image_edit"):
                self.send_error(404, "Image edit job not found")
                return
            self._send_html(image_edit_views.page(CSRF, job_id, task))
            return
        if path.path.startswith("/image-edit/private/preview/"):
            self._private_image_preview(path.path.rsplit("/", 1)[1], path.query)
            return
        for prefix, suffix in (("/image-edit/private/status/", ""),
                               ("/image-edit/private/result/", "/result")):
            if path.path.startswith(prefix):
                self._private_edit_proxy(path.path[len(prefix):], suffix)
                return
        for prefix, kind, binary, preview in (
                ("/image-edit/status/", "image_edit", False, False),
                ("/image-edit/result/", "image_edit", True, False),
                ("/image-edit/generation/status/", "image", False, False),
                ("/image-edit/generation/result/", "image", True, False),
                ("/image-edit/generation/preview/", "image", True, True)):
            if path.path.startswith(prefix):
                job = image_edit_job(path.path[len(prefix):], kind)
                if not job:
                    self.send_error(404, "Image edit job not found")
                    return
                if binary:
                    if job["status"] != "completed" or job["result_name"] != job["id"] + ".result":
                        self.send_error(404, "Edited image is not ready")
                        return
                    data = (Path("/app/data/multimodal/artifacts") / job["result_name"]).read_bytes()
                    content_type = job["result_type"] or "image/png"
                    if preview:
                        try:
                            data = image_preview(data, content_type, path.query)
                        except (ValueError, KeyError, zipfile.BadZipFile):
                            self.send_error(400, "Invalid image preview")
                            return
                        content_type = "image/png"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    data = json.dumps({"id": job["id"], "kind": job["kind"], "status": job["status"],
                                       "stage": job["stage"], "error": job["error"],
                                       "result": json.loads(job["result_json"]) if job["result_json"] else None}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                return
        if path.path == "/profiles":
            try:
                hardware, rows = profile_recommendations()
                active = multimodal_profiles.load(PROFILE_PATH)
                db_path = Path("/app/data/multimodal/jobs.sqlite3")
                jobs = []
                if db_path.is_file():
                    db = sqlite3.connect(db_path)
                    try:
                        db.row_factory = sqlite3.Row
                        jobs = db.execute("SELECT kind,status,stage,created_at FROM jobs ORDER BY rowid DESC LIMIT 20").fetchall()
                    finally:
                        db.close()
            except Exception as exc:
                self.send_error(503, "Could not read profile recommendations: " + str(exc))
                return
            self._send_html(multimodal_profiles.page(rows, active, hardware, CSRF, jobs,
                                                     profile_model_info({mode for row in rows
                                                                         for mode in row["text_modes"]})))
            return
        if path.path == "/assets/operation_poll.js":
            try:
                with open("/app/operation_poll.js", "rb") as script:
                    data = script.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/assets/image_edit_poll.js":
            try:
                data = Path("/app/image_edit_poll.js").read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/workload/status":
            try:
                snapshot = workload_snapshot()
            except (OSError, RuntimeError, ValueError, KeyError):
                snapshot = {"mode": None, "state": "unavailable", "detail": "Could not check model status", "api_id": None}
            snapshot["label"] = (BUILTIN_LABELS.get(snapshot["mode"]) or
                                 next((model_display_name(item) for item in MODEL_MANAGER.installed()
                                       if "hf-" + item["candidate_id"] == snapshot["mode"]), "No model running"))
            data = json.dumps(snapshot).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/operations/status":
            query = urllib.parse.parse_qs(path.query)
            scope = query.get("scope", [""])[0]
            try:
                if scope == "models":
                    jobs = MODEL_MANAGER.jobs()
                    media = MEDIA_MANAGER.jobs()
                    catalog = DISCOVERY.latest()
                    search = DISCOVERY.latest_search()
                    engine = ENGINE_UPDATES.status()
                    job_names = {item["candidate_id"]: model_display_name(item) for item in MODEL_MANAGER.installed()}
                    job_names.update({item["id"]: item.get("name") or model_display_name(item)
                                      for item in catalog["items"] + search["items"]})
                    job_names.update({item["id"]: item["name"] for item in BY_ID.values()})
                    active = (any(job["status"] in ("queued", "running") for job in jobs + media) or
                              catalog["status"] == "running" or search["status"] == "running" or
                              engine["checking"] or engine["busy"])
                    lines = [f'Text {job_names.get(job["candidate_id"], "Hugging Face model")}: {job["status"]} · {job["stage"]}'
                             for job in jobs if job["status"] in ("queued", "running")][:5]
                    lines += [f'Media {job["candidate_id"]}: {job["status"]} · {job["stage"]}'
                              for job in media if job["status"] in ("queued", "running")][:5]
                    if catalog["status"] == "running":
                        lines.append("Catalog refresh running")
                    if search["status"] == "running":
                        lines.append("Hugging Face search running")
                    if engine["checking"] or engine["busy"]:
                        lines.append("Engine check or update running")
                    if not lines:
                        latest = jobs[0] if jobs else media[0] if media else None
                        lines = [f'{job_names.get(latest["candidate_id"], latest["candidate_id"])}: {latest["status"]} · {latest["stage"]}' if latest else "Operation finished"]
                elif scope == "home":
                    context = CONTEXT_TUNER.status()
                    webui = WEBUI_UPDATES.status()
                    active = context["busy"] or webui["checking"] or webui["busy"]
                    lines = []
                    if context["job"]:
                        lines.append(f'Context: {context["job"]["status"]} · {context["job"]["stage"]}')
                    if webui["job"]:
                        lines.append(f'Open WebUI: {webui["job"]["status"]} · {webui["job"]["stage"]}')
                    if webui["checking"]:
                        lines.append("Checking Open WebUI update")
                elif scope == "sharing":
                    slot = PARALLEL_TUNER.status()
                    active = slot["busy"]
                    job = slot["job"]
                    lines = [f'Slot test: {job["status"]} · {job["stage"]}'] if job else []
                elif scope == "benchmark":
                    run_id = query.get("run_id", [""])[0]
                    uuid.UUID(run_id)
                    run = BENCHMARKS.get_run(run_id)
                    if not run:
                        raise ValueError("Benchmark run missing")
                    active = run["status"] in ("queued", "running")
                    lines = [f'Benchmark: {run["status"]} · {run["stage"]}']
                else:
                    raise ValueError("Unknown operation scope")
            except (ValueError, KeyError):
                self.send_error(400, "Invalid operation status request")
                return
            data = json.dumps({"active": bool(active), "message": " | ".join(lines) or "Operation finished"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/harnesses":
            try:
                current = CONTEXT_TUNER.active(active_mode())
                context_tokens = current["tokens"] if current else 4096
            except (RuntimeError, ValueError, KeyError):
                context_tokens = 4096
            self._send_html(harness_views.index(PUBLIC_API_URL, context_tokens))
            return
        if path.path == "/gpu/graph":
            self._send_html(GPU_MONITOR.render(), embeddable=True)
            return
        if path.path == "/sharing":
            try:
                hardware = MODEL_MANAGER.hardware()
                settings = SHARING.settings(hardware, active_server_slots())
                clients = SHARING.clients()
                query = urllib.parse.parse_qs(path.query)
                amount, unit = usage_window(query, "all_")
                window_end = dt.datetime.now(dt.timezone.utc)
                all_usage = SHARING.usage_summary("all", amount, unit, end=window_end)
                client_usage = {client_id: SHARING.usage_summary(client_id, amount, unit, end=window_end)
                                for client_id in ["legacy", *(item["id"] for item in clients)]}
                notice = {"limits_saved": "Sharing limits saved and confirmed by the API gateway.",
                          "defaults_restored": "Detected hardware defaults restored and confirmed by the API gateway.",
                          "key_revoked": "Client key revoked. New requests using it are denied.",
                          "slot_test_started": "Model slot test started. This page will report readiness or rollback."}.get(
                              query.get("status", [""])[0], "")
                self._send_html(sharing_views.index(settings, clients, CSRF, hardware, active_server_slots(),
                                                    active_mode(), PARALLEL_TUNER.status(), message=notice,
                                                    all_usage=all_usage, client_usage=client_usage))
            except ValueError as exc:
                self.send_error(400, str(exc))
            except Exception as exc:
                self.send_error(503, "Sharing settings unavailable: " + type(exc).__name__)
            return
        if path.path == "/assets/gpu-workstation.webp":
            try:
                with open(ASSET_PATH, "rb") as asset:
                    data = asset.read()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        if path.path == "/models":
            query = urllib.parse.parse_qs(path.query)
            prefer_ablit = query.get("ablit", ["1"])[0] != "0"
            try:
                hardware = MODEL_MANAGER.hardware()
                if hardware.get("vram_mib"):
                    DISCOVERY.maybe_refresh(hardware)
                    ENGINE_UPDATES.check_async()
                hardware, suggestions = MODEL_MANAGER.recommendations(prefer_ablit, hardware)
                media_suggestions = MEDIA_MANAGER.recommendations(hardware, prefer_ablit)
                installed = MODEL_MANAGER.installed()
                active_models = {item["candidate_id"] for item in installed if running(item["container"])}
                self._send_html(model_views.index(hardware, suggestions, installed, MODEL_MANAGER.jobs(), CSRF,
                                                  prefer_ablit, media_suggestions, MEDIA_MANAGER.jobs(), COMFY_URL,
                                                  DISCOVERY.latest(hardware), ENGINE_UPDATES.status(), DISCOVERY.latest_search(hardware),
                                                  MODEL_MANAGER.hybrid_enabled(), active_models,
                                                  "Model uninstalled. Benchmarks were retained." if
                                                  urllib.parse.parse_qs(path.query).get("removed") == ["1"] else
                                                  "API ID changed. Update API clients and Open WebUI to use the new ID." if
                                                  urllib.parse.parse_qs(path.query).get("renamed") == ["1"] else ""))
            except Exception as exc:
                self.send_error(503, f"Recommendations unavailable: {type(exc).__name__}")
            return
        if path.path == "/benchmarks":
            query = urllib.parse.parse_qs(path.query)
            deleted = query.get("deleted", [""])[0]
            message = f"Deleted {deleted} benchmark run(s)." if deleted.isdecimal() and len(deleted) <= 6 else ""
            self._send_html(benchmark_views.index(
                BENCHMARKS.list_runs(), CSRF, message=message, active_mode=active_mode(),
                sort=query.get("sort", ["tokens_per_second"])[0],
                direction=query.get("dir", ["desc"])[0],
                group_context=query.get("group", [""])[0] == "context"))
            return
        if path.path == "/benchmarks/model":
            query = urllib.parse.parse_qs(path.query)
            model_id = query.get("model_id", [])
            if len(model_id) != 1 or not 1 <= len(model_id[0]) <= 256:
                self.send_error(400, "Invalid model")
                return
            runs = BENCHMARKS.model_runs(model_id[0])
            if not runs:
                self.send_error(404)
                return
            self._send_html(benchmark_views.model_detail(model_id[0], runs, CSRF))
            return
        if self.path.startswith("/benchmarks/") and self.path.endswith("/image"):
            run_id = self.path[len("/benchmarks/"):-len("/image")]
            try:
                uuid.UUID(run_id)
            except ValueError:
                self.send_error(404)
                return
            run = BENCHMARKS.get_run(run_id)
            image = (json.loads(run["results_json"] or "{}").get("image_generation") or {}) if run else {}
            filename = image.get("filename", "")
            if image.get("status") != "completed" or image.get("subfolder") or not re.fullmatch(r"benchmark_512_sdxl_\d+_\.png", filename):
                self.send_error(404)
                return
            try:
                fd = os.open(os.path.join("/app/images", filename), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                self.send_error(404)
                return
            with os.fdopen(fd, "rb") as file:
                size = os.fstat(file.fileno()).st_size
                if size < 1 or size > 10_000_000:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                while chunk := file.read(65536):
                    self.wfile.write(chunk)
            return
        if self.path.startswith("/benchmarks/"):
            slug = self.path.removeprefix("/benchmarks/")
            json_response = slug.endswith(".json")
            run_id = slug[:-5] if json_response else slug
            try:
                uuid.UUID(run_id)
            except ValueError:
                self.send_error(404)
                return
            run = BENCHMARKS.get_run(run_id)
            if not run:
                self.send_error(404)
                return
            if json_response:
                export = dict(run)
                export["system"] = json.loads(export.pop("system_json") or "{}")
                export["results"] = json.loads(export.pop("results_json") or "{}")
                data = json.dumps(export, indent=2).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="benchmark-{run_id}.json"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_html(benchmark_views.detail(run))
            return
        if self.path == "/route":
            try:
                mode = active_mode()
                if not mode or mode == "media":
                    self.send_error(503, "No active language model")
                    return
                backends = {"qwen4b": "qwen4b:8000", "flint-iq4": "flint-iq4:8080",
                            "flint-q3": "flint-q3:8080", "deepseek": "deepseek-v4-distill:8080",
                            "qwen38": "qwen38-ablit:8080", "gemma4": "gemma4-ablit:8080",
                            "huihui": "huihui:8080", "cpu-4b": "cpu-4b:8080",
                            "cpu-tiny": "cpu-tiny:8080", "vision": "vision:8000", "llm": "llm:8000"}
                if mode.startswith("hf-"):
                    item = next(item for item in MODEL_MANAGER.installed() if item["candidate_id"] == mode[3:])
                    backend = item["container"] + (":8000" if item.get("format") == "safetensors" else ":8080")
                else:
                    backend = backends[mode]
                model_id = MODELS[mode][0]
            except Exception:
                self.send_error(503, "Docker status unavailable")
                return
            self.send_response(200)
            self.send_header("X-LLM-Backend", backend)
            self.send_header("X-LLM-Model", model_id)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.path != "/":
            self.send_error(404)
            return
        WEBUI_UPDATES.check_async()
        self._page()

    def _submit_image_edit(self):
        if self.headers.get("Origin") != ORIGIN or self.headers.get("Host") != urllib.parse.urlsplit(ORIGIN).netloc:
            self.send_error(403, "Invalid origin")
            return
        content_type = self.headers.get("Content-Type", "")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= 24 * 1024 * 1024 or not content_type.startswith("multipart/form-data;"):
            self.send_error(413, "Upload a JPEG, PNG, or WebP image up to 16 MiB")
            return
        raw = self.rfile.read(length)
        document = BytesParser(policy=email_policy).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii", "ignore") + b"\r\n\r\n" + raw)
        if not document.is_multipart():
            self.send_error(400, "Invalid upload form")
            return
        fields = {}
        for part in document.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name not in ("token", "prompt", "image", "private") or name in fields:
                self.send_error(400, "Invalid upload form fields")
                return
            fields[name] = part.get_payload(decode=True)
        if fields.get("token") != CSRF.encode():
            self.send_error(403, "Invalid request")
            return
        image_data = fields.get("image", b"")
        try:
            prompt = fields.get("prompt", b"").decode("utf-8")
        except UnicodeDecodeError:
            self.send_error(400, "Instruction must be UTF-8 text")
            return
        if not 1 <= len(image_data) <= 16 * 1024 * 1024 or not 1 <= len(prompt) <= 1000:
            self.send_error(400, "Choose an image up to 16 MiB and a short instruction")
            return
        if fields.get("private") not in (None, b"on"):
            self.send_error(400, "Invalid privacy setting")
            return
        private = fields.get("private") == b"on"
        payload = json.dumps({"data": base64.b64encode(image_data).decode(), "prompt": prompt,
                              "private": private}).encode()
        request = urllib.request.Request("http://multimodal:8003/v1/images/edits", data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "X-Internal-Token": os.environ["BENCHMARK_API_KEY"],
                                                  "X-AI-Client-ID": "switcher-admin"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                ident = json.load(response)["id"]
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", {}).get("message", "Image edit was rejected")
            except (ValueError, TypeError):
                detail = "Image edit was rejected"
            self.send_error(exc.code, detail)
            return
        except (OSError, KeyError, ValueError):
            self.send_error(502, "Image-edit worker is unavailable")
            return
        if private:
            data = json.dumps({"id": ident, "status": "queued", "private": True}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(303)
        self.send_header("Location", "/image-edit?job=" + ident)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _submit_image_generation(self):
        if self.headers.get("Origin") != ORIGIN or self.headers.get("Host") != urllib.parse.urlsplit(ORIGIN).netloc:
            self.send_error(403, "Invalid origin")
            return
        content_type = self.headers.get("Content-Type", "")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= 24 * 1024 * 1024 or not content_type.startswith("multipart/form-data;"):
            self.send_error(413, "Upload up to three reference images, 16 MiB total")
            return
        document = BytesParser(policy=email_policy).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("ascii", "ignore") +
            b"\r\n\r\n" + self.rfile.read(length))
        if not document.is_multipart():
            self.send_error(400, "Invalid upload form")
            return
        fields, references = {}, []
        for part in document.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if name == "reference":
                data = part.get_payload(decode=True)
                if data:
                    references.append(data)
            elif name in ("token", "prompt", "private", "reference_strength") and name not in fields:
                fields[name] = part.get_payload(decode=True)
            else:
                self.send_error(400, "Invalid upload form fields")
                return
        if fields.get("token") != CSRF.encode():
            self.send_error(403, "Invalid request")
            return
        try:
            prompt = fields.get("prompt", b"").decode("utf-8")
            strength = fields.get("reference_strength", b"0.55").decode("ascii")
        except UnicodeDecodeError:
            self.send_error(400, "Invalid prompt or reference strength")
            return
        if not 1 <= len(prompt) <= 1000 or strength not in ("0.35", "0.55", "0.75") or \
                not 0 <= len(references) <= 3 or any(not 1 <= len(item) <= 8 * 1024 * 1024 for item in references) or \
                sum(map(len, references)) > 16 * 1024 * 1024 or fields.get("private") not in (None, b"on"):
            self.send_error(400, "Use a short prompt and up to three reference images, 16 MiB total")
            return
        private = fields.get("private") == b"on"
        payload = json.dumps({"prompt": prompt, "references": [base64.b64encode(item).decode() for item in references],
                              "reference_strength": float(strength), "private": private}).encode()
        request = urllib.request.Request("http://multimodal:8003/v1/images/generations", data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "X-Internal-Token": os.environ["BENCHMARK_API_KEY"],
                                                  "X-AI-Client-ID": "switcher-admin"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                ident = json.load(response)["id"]
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", {}).get("message", "Image generation was rejected")
            except (ValueError, TypeError):
                detail = "Image generation was rejected"
            self.send_error(exc.code, detail)
            return
        except (OSError, KeyError, ValueError):
            self.send_error(502, "Image worker is unavailable")
            return
        if private:
            data = json.dumps({"id": ident, "status": "queued", "private": True}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(303)
        self.send_header("Location", "/image-edit?task=generate&job=" + ident)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _private_image_preview(self, ident, query):
        try:
            if uuid.UUID(ident).version != 4:
                raise ValueError("Invalid job ID")
            request = urllib.request.Request("http://multimodal:8003/v1/jobs/" + ident + "/result",
                                             headers={"X-Internal-Token": os.environ["BENCHMARK_API_KEY"],
                                                      "X-AI-Client-ID": "switcher-admin"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(128 * 1024 * 1024 + 1)
                if len(data) > 128 * 1024 * 1024:
                    raise ValueError("Result too large")
                image = image_preview(data, response.headers.get_content_type(), query)
        except (ValueError, KeyError, zipfile.BadZipFile, OSError):
            self.send_error(404, "Private image preview unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(image)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(image)

    def _private_edit_proxy(self, ident, suffix="", method="GET"):
        try:
            if uuid.UUID(ident).version != 4:
                raise ValueError("Invalid job ID")
        except (TypeError, ValueError):
            self.send_error(404, "Private job not found")
            return
        request = urllib.request.Request("http://multimodal:8003/v1/jobs/" + ident + suffix,
                                         method=method, headers={
                                             "X-Internal-Token": os.environ["BENCHMARK_API_KEY"],
                                             "X-AI-Client-ID": "switcher-admin"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(128 * 1024 * 1024 + 1)
                if len(data) > 128 * 1024 * 1024:
                    raise ValueError("Private result exceeds 128 MiB")
                status = response.status
                content_type = response.headers.get_content_type()
        except (urllib.error.HTTPError, OSError, ValueError):
            self.send_error(404, "Private job is unavailable or expired")
            return
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        prefix = "/image-edit/private/"
        if not self.path.startswith(prefix) or self.headers.get("Origin") != ORIGIN or \
                self.headers.get("Host") != urllib.parse.urlsplit(ORIGIN).netloc or \
                self.headers.get("X-CSRF-Token") != CSRF:
            self.send_error(403, "Invalid request")
            return
        self._private_edit_proxy(self.path[len(prefix):], method="DELETE")

    def do_POST(self):
        if self.path == "/image-edit/generate":
            self._submit_image_generation()
            return
        if self.path == "/image-edit/submit":
            self._submit_image_edit()
            return
        if self.path in ("/multimodal/acquire", "/multimodal/release"):
            if self.headers.get("Authorization") != "Bearer " + os.environ["BENCHMARK_API_KEY"]:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 128 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("Invalid lease request")
                job_id = json.loads(self.rfile.read(length))["job_id"]
                uuid.UUID(job_id)
                result = media_lease(self.path.rsplit("/", 1)[1], job_id)
            except (ValueError, KeyError, TypeError) as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path not in ("/switch", "/benchmark", "/context/tune", "/benchmarks/delete", "/profiles/apply", "/models/install", "/models/uninstall", "/models/rename",
                             "/models/media/install", "/models/refresh", "/models/search", "/models/hardware/rescan", "/models/discovered/install", "/models/hybrid", "/models/hybrid/apply",
                             "/models/engine/check", "/models/engine/update", "/webui/update", "/webui/update/dismiss",
                             "/sharing/save", "/sharing/reset", "/sharing/client", "/sharing/revoke",
                             "/sharing/parallel"):
            self.send_error(404)
            return
        if self.headers.get("Origin") != ORIGIN or self.headers.get("Host") != urllib.parse.urlsplit(ORIGIN).netloc:
            self.send_error(403, "Invalid origin")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 1 <= length <= 1024 or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            self.send_error(400)
            return
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
        if fields.get("token") != [CSRF]:
            self.send_error(403, "Invalid request")
            return
        if self.path == "/profiles/apply":
            ident = fields.get("profile", [])
            if len(ident) != 1:
                self.send_error(400, "Select one profile")
                return
            try:
                _, rows = profile_recommendations()
                selected = next((row for row in rows if row["id"] == ident[0]), None)
                if selected is None:
                    raise ValueError("Profile not found")
                selected = dict(selected)
                action = fields.get("action", ["load"])
                if len(action) != 1 or action[0] not in ("save", "load", "copy", "delete"):
                    raise ValueError("Unknown profile action")
                action = action[0]
                library = multimodal_profiles.load_library(PROFILE_LIBRARY_PATH)
                if action == "delete":
                    if not selected["custom"] or (multimodal_profiles.load(PROFILE_PATH) or {}).get("id") == selected["id"]:
                        raise ValueError("Switch to another profile before deleting this copy")
                    library["custom"].pop(selected["id"], None)
                    multimodal_profiles.save_library(PROFILE_LIBRARY_PATH, library)
                    self.send_response(303)
                    self.send_header("Location", "/profiles")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                text_modes = fields.get("text_mode", [])
                if len(text_modes) != 1 or text_modes[0] not in selected["text_modes"]:
                    raise ValueError("Select an installed language model")
                selected["text_mode"] = text_modes[0]
                selected["prefer_ablit"] = fields.get("prefer_ablit") == ["on"]
                if selected["asr_models"]:
                    asr = fields.get("asr_model", [])
                    if len(asr) != 1 or asr[0] not in selected["asr_models"]:
                        raise ValueError("Select a supported transcription model")
                    selected["asr_model"] = asr[0]
                if selected["image"]:
                    checkpoint = fields.get("checkpoint", [])
                    if selected["checkpoints"]:
                        if len(checkpoint) != 1 or checkpoint[0] not in selected["checkpoints"]:
                            raise ValueError("Select an installed image checkpoint")
                        selected["image_checkpoint"] = checkpoint[0]
                    if selected["image_edit_checkpoints"]:
                        edit_checkpoint = fields.get("edit_checkpoint", [])
                        if len(edit_checkpoint) != 1 or edit_checkpoint[0] not in selected["image_edit_checkpoints"]:
                            raise ValueError("Select an installed inpainting checkpoint")
                        selected["image_edit_checkpoint"] = edit_checkpoint[0]
                if selected["video"]:
                    video_model = fields.get("video_model", [])
                    if len(video_model) != 1 or video_model[0] not in selected["video_models"]:
                        raise ValueError("Select an installed video model")
                    selected["video_model"] = video_model[0]
                if action == "copy":
                    name = fields.get("copy_name", [""])
                    if len(name) != 1 or not 2 <= len(name[0].strip()) <= 60:
                        raise ValueError("Enter a copy name of 2–60 characters")
                    copy_id = multimodal_profiles.new_custom_id()
                    library["custom"][copy_id] = {"base": selected["base"], "name": name[0].strip(),
                                                  "selection": multimodal_profiles.selected_choices(selected)}
                    multimodal_profiles.save_library(PROFILE_LIBRARY_PATH, library)
                elif action == "save":
                    if selected["custom"]:
                        library["custom"][selected["id"]]["selection"] = multimodal_profiles.selected_choices(selected)
                    else:
                        library["saved"][selected["id"]] = multimodal_profiles.selected_choices(selected)
                    multimodal_profiles.save_library(PROFILE_LIBRARY_PATH, library)
                else:
                    if not selected["available"]:
                        raise ValueError("Install the required model files before loading this profile")
                    previous = active_mode()
                    old_profile = multimodal_profiles.load(PROFILE_PATH)
                    if previous != selected["text_mode"]:
                        switch(selected["text_mode"])
                    try:
                        multimodal_profiles.save(PROFILE_PATH, selected)
                        if selected["custom"]:
                            library["custom"][selected["id"]]["selection"] = multimodal_profiles.selected_choices(selected)
                        else:
                            library["saved"][selected["id"]] = multimodal_profiles.selected_choices(selected)
                        multimodal_profiles.save_library(PROFILE_LIBRARY_PATH, library)
                    except Exception:
                        if previous and previous != selected["text_mode"]:
                            switch(previous)
                        if old_profile:
                            multimodal_profiles.save(PROFILE_PATH, old_profile)
                        raise
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except Exception as exc:
                self.send_error(409, "Profile activation failed; previous configuration restored: " + str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/profiles")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/models/uninstall":
            candidate = fields.get("candidate", [])
            confirmation = fields.get("confirm", [])
            if len(candidate) != 1 or len(confirmation) != 1 or fields.get("delete_ack") != ["1"]:
                self.send_error(400, "Select one model and confirm deletion")
                return
            try:
                if any((BENCHMARKS.busy, MEDIA_MANAGER.busy, CONTEXT_TUNER.busy,
                        ENGINE_UPDATES.busy, PARALLEL_TUNER.busy)):
                    raise RuntimeError("Finish the active benchmark, download, or tuning operation first")
                with LOCK:
                    MODEL_MANAGER.uninstall(candidate[0], confirmation[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/models?removed=1#specific-search")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/models/rename":
            candidate = fields.get("candidate", [])
            alias = fields.get("alias", [])
            if len(candidate) != 1 or len(alias) != 1:
                self.send_error(400, "Select one model and API ID")
                return
            try:
                if any((BENCHMARKS.busy, MEDIA_MANAGER.busy, CONTEXT_TUNER.busy,
                        ENGINE_UPDATES.busy, PARALLEL_TUNER.busy)):
                    raise RuntimeError("Finish the active benchmark, download, or tuning operation first")
                with LOCK:
                    MODEL_MANAGER.rename_alias(candidate[0], alias[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/models?renamed=1#specific-search")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path.startswith("/sharing/"):
            try:
                hardware = MODEL_MANAGER.hardware()
                slots = active_server_slots()
                settings = SHARING.settings(hardware, slots)
                created = None
                status_message = ""
                if self.path == "/sharing/parallel":
                    modes = fields.get("mode", [])
                    wanted = fields.get("slots", [])
                    if (len(modes) != 1 or len(wanted) != 1 or not wanted[0].isdecimal() or
                            modes[0] != active_mode()):
                        raise ValueError("Select the active model and a whole-number slot count")
                    if BENCHMARKS.busy or MODEL_MANAGER.busy or MEDIA_MANAGER.busy or CONTEXT_TUNER.busy or ENGINE_UPDATES.busy:
                        raise RuntimeError("Finish the active benchmark, download, context test or engine update first")
                    PARALLEL_TUNER.start(modes[0], int(wanted[0]))
                    status_message = "slot_test_started"
                elif self.path == "/sharing/save":
                    saved = SHARING.save_settings({key: fields.get(key, [""])[0] for key in FIELDS})
                    try:
                        verify_sharing_settings(saved)
                    except Exception:
                        SHARING.restore_settings(settings)
                        raise RuntimeError("Gateway did not accept sharing limits; previous settings restored")
                    status_message = "limits_saved"
                elif self.path == "/sharing/reset":
                    saved = SHARING.reset(hardware, slots)
                    try:
                        verify_sharing_settings(saved)
                    except Exception:
                        SHARING.restore_settings(settings)
                        raise RuntimeError("Gateway did not accept hardware defaults; previous settings restored")
                    status_message = "defaults_restored"
                elif self.path == "/sharing/client":
                    created = SHARING.create_client(fields.get("label", [""])[0])
                else:
                    ids = fields.get("client_id", [])
                    if len(ids) != 1 or not SHARING.revoke(ids[0]):
                        raise ValueError("Client key is missing or already revoked")
                    status_message = "key_revoked"
            except ValueError as exc:
                self._send_html(sharing_views.index(SHARING.settings(), SHARING.clients(), CSRF,
                                                    hardware, slots, active_mode(), PARALLEL_TUNER.status(),
                                                    message="Change not applied: " + str(exc)), status=400)
                return
            except RuntimeError as exc:
                self._send_html(sharing_views.index(SHARING.settings(), SHARING.clients(), CSRF,
                                                    hardware, slots, active_mode(), PARALLEL_TUNER.status(),
                                                    message="Change failed; current settings retained: " + str(exc)), status=409)
                return
            if created:
                self._send_html(sharing_views.index(SHARING.settings(), SHARING.clients(), CSRF,
                                                    hardware, slots, active_mode(), PARALLEL_TUNER.status(),
                                                    created=created))
            else:
                self.send_response(303)
                self.send_header("Location", "/sharing?status=" + status_message)
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            return
        if self.path == "/webui/update/dismiss":
            job_ids = fields.get("job_id", [])
            if len(job_ids) != 1 or not WEBUI_UPDATES.dismiss_result(job_ids[0]):
                self.send_error(409, "No current Open WebUI update result to dismiss")
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/webui/update":
            try:
                WEBUI_UPDATES.start_update()
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/context/tune":
            try:
                mode_values = fields.get("mode", [])
                token_values = fields.get("context_tokens", [])
                if len(mode_values) != 1 or len(token_values) != 1 or not token_values[0].isdecimal():
                    raise ValueError("Select a model and whole-number context")
                if active_mode() != mode_values[0]:
                    raise ValueError("Tune only the currently active model")
                if (BENCHMARKS.busy or MODEL_MANAGER.busy or MEDIA_MANAGER.busy or
                        ENGINE_UPDATES.busy or PARALLEL_TUNER.busy):
                    raise RuntimeError("Finish the active benchmark, download or engine update first")
                CONTEXT_TUNER.start(mode_values[0], int(token_values[0]))
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/models/hybrid":
            values = fields.get("enabled", [])
            if values not in ([], ["1"]):
                self.send_error(400, "Invalid GPU + RAM preference")
                return
            if MODEL_MANAGER.busy:
                self.send_error(409, "Finish the current model operation first")
                return
            try:
                MODEL_MANAGER.set_hybrid_enabled(values == ["1"])
            except sqlite3.Error:
                self.send_error(503, "Memory preference was not saved")
                return
            self.send_response(303)
            self.send_header("Location", "/models#hybrid-memory")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/models/hybrid/apply":
            candidates = fields.get("candidate", [])
            override = fields.get("override_fit", [])
            if len(candidates) != 1 or override not in ([], ["1"]):
                self.send_error(400, "Select one installed model and a valid memory override")
                return
            try:
                if BENCHMARKS.busy or CONTEXT_TUNER.busy or PARALLEL_TUNER.busy or ENGINE_UPDATES.busy:
                    raise RuntimeError("Finish the current benchmark or engine change first")
                MODEL_MANAGER.start_reconfigure_hybrid(candidates[0], allow_unfit=override == ["1"])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/models#hybrid-memory")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path in ("/models/refresh", "/models/search", "/models/hardware/rescan", "/models/engine/check", "/models/engine/update", "/models/discovered/install"):
            try:
                if self.path == "/models/refresh":
                    DISCOVERY.start_refresh(fields.get("search", [""])[0])
                elif self.path == "/models/search":
                    values = fields.get("query", [])
                    if len(values) != 1:
                        raise ValueError("Enter one model name or Hugging Face repository")
                    DISCOVERY.start_search(values[0])
                elif self.path == "/models/hardware/rescan":
                    hardware = MODEL_MANAGER.hardware(force=True)
                    SHARING.settings(hardware, active_server_slots())
                    DISCOVERY.maybe_refresh(hardware)
                elif self.path == "/models/engine/check":
                    ENGINE_UPDATES.check_async(force=True)
                elif self.path == "/models/engine/update":
                    if BENCHMARKS.busy or MODEL_MANAGER.busy or MEDIA_MANAGER.busy or CONTEXT_TUNER.busy or PARALLEL_TUNER.busy:
                        raise RuntimeError("Finish the active benchmark, download or context test first")
                    ENGINE_UPDATES.start_update()
                else:
                    selected = fields.get("candidate", [])
                    if len(selected) != 1:
                        raise ValueError("Select one discovered model")
                    candidate = DISCOVERY.candidate(selected[0])
                    if not candidate:
                        raise ValueError("Candidate is no longer in the latest search or catalog refresh")
                    override = fields.get("override_fit", [])
                    if override not in ([], ["1"]):
                        raise ValueError("Invalid memory override")
                    MODEL_MANAGER.start_install(selected[0], candidate, ENGINE_UPDATES.status().get("local_build"),
                                                allow_unfit=override == ["1"])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/models")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/benchmarks/delete":
            run_id = fields.get("run_id", [])
            before = fields.get("before", [])
            model_id = fields.get("model_id", [])
            if sum(bool(item) for item in (run_id, before, model_id)) != 1:
                self.send_error(400, "Choose one deletion action")
                return
            try:
                if run_id:
                    uuid.UUID(run_id[0])
                    deleted = BENCHMARKS.delete_run(run_id[0])
                elif model_id:
                    if len(model_id) != 1 or not 1 <= len(model_id[0]) <= 256:
                        raise ValueError("Invalid model")
                    deleted = BENCHMARKS.delete_model(model_id[0])
                else:
                    date = dt.date.fromisoformat(before[0])
                    if date.isoformat() != before[0]:
                        raise ValueError("Invalid date")
                    deleted = BENCHMARKS.delete_before(date.isoformat() + "T00:00:00+00:00")
            except (ValueError, IndexError):
                self.send_error(400, "Invalid run or date")
                return
            if deleted is None:
                self.send_error(409, "Finish the active benchmark before deleting this model")
                return
            self.send_response(303)
            self.send_header("Location", f"/benchmarks?deleted={deleted}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/models/install":
            candidate = fields.get("candidate", [])
            if len(candidate) != 1:
                self.send_error(400, "Select one catalog model")
                return
            try:
                job_id = MODEL_MANAGER.start_install(candidate[0])
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", f"/models?job={job_id}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if self.path == "/models/media/install":
            candidate = fields.get("candidate", [])
            if len(candidate) != 1:
                self.send_error(400, "Select one media model")
                return
            try:
                job_id = MEDIA_MANAGER.start_install(candidate[0], MODEL_MANAGER.hardware())
            except ValueError as exc:
                self.send_error(400, str(exc))
                return
            except RuntimeError as exc:
                self.send_error(409, str(exc))
                return
            self.send_response(303)
            self.send_header("Location", f"/models?media_job={job_id}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if fields.get("mode") not in ([mode] for mode in MODELS):
            self.send_error(403, "Invalid request")
            return
        mode = fields["mode"][0]
        if SELECTED_MODES and mode not in SELECTED_MODES and not mode.startswith("hf-"):
            self.send_error(403, "This workload was not selected at installation")
            return
        if self.path == "/benchmark":
            if CONTEXT_TUNER.busy or ENGINE_UPDATES.busy or PARALLEL_TUNER.busy:
                self.send_error(409, "Finish the context test or engine update before benchmarking")
                return
            run_id = BENCHMARKS.start(mode)
            if not run_id:
                self._send_html(benchmark_views.index(BENCHMARKS.list_runs(), CSRF, "A benchmark is already running.", active_mode()), 409)
                return
            self.send_response(303)
            self.send_header("Location", f"/benchmarks/{run_id}")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if BENCHMARKS.busy:
            self.send_error(409, "Benchmark in progress")
            return
        try:
            switch(mode)
            self._page(f"Start requested for {mode}. Refresh status before opening the service.")
        except Exception as exc:
            self._page(f"Switch failed: {exc}", 500)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()
