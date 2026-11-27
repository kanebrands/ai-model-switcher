#!/usr/bin/env python3
"""Small, fixed-action model switch. Only nginx should expose this service."""
import html
import http.client
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from benchmarks import BenchmarkManager, MODELS
import benchmark_views
from model_catalog import ModelManager
from media_catalog import MediaManager
from catalog_discovery import CatalogDiscovery
from engine_updates import EngineUpdates
from context_tuning import ContextTuner
from webui_updates import WebUIUpdates
from sharing import SharingManager, FIELDS
from gpu_metrics import GPUMonitor
from parallel_tuning import ParallelTuner
import sharing_views
import model_views

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
CSRF = os.environ["CONTROL_CSRF_TOKEN"]
SELECTED_MODES = {value for value in os.environ.get("AI_SELECTED_MODELS", "").split(",") if value}
LOCK = threading.Lock()
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


def switch(target):
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
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'self'" if embeddable else "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

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
        active.extend(item["alias"] for item in managed if running(item["container"]))
        mode = active[0] if len(active) == 1 else "Neither" if not active else "Conflict: " + ", ".join(active)
        h = lambda s: html.escape(s, quote=True)
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
                        **{"hf-" + item["candidate_id"]: item["alias"] + " (Hugging Face)" for item in managed}}
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
        context_form = (f'<p>Loaded model: <strong>{h(context_active["alias"])}</strong> · '
                        f'Context: <strong>{h(context_text(context_active["mode"]))}</strong></p>'
                        f'<form method="post" action="/context/tune"><input type="hidden" name="token" value="{h(CSRF)}">'
                        f'<input type="hidden" name="mode" value="{h(context_active["mode"])}">'
                        f'<label for="context-tokens">New context (tokens)</label><br><input id="context-tokens" '
                        f'name="context_tokens" type="number" min="4096" max="262144" step="1024" '
                        f'value="{context_active["tokens"]}" required> '
                        '<button type="submit">Test and save context</button></form>') if context_active else '<p>Start one model to tune its context.</p>'
        if context_status["busy"]:
            context_form = '<p>Context test running. This page refreshes every five seconds.</p>'
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
        home_style = """.home-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(19rem,1fr);gap:1rem;align-items:start}.home-grid .card{margin:0}.home-grid select{width:100%;margin:.45rem 0}.home-grid button{margin:.55rem .5rem .2rem 0}.home-actions{display:flex;flex-wrap:wrap;align-items:center;gap:.3rem 1rem}.home-actions form{margin:0}.active-line{display:flex;align-items:center;gap:.8rem;flex-wrap:wrap}.active-badge{display:inline-flex;align-items:center;gap:.5rem;background:#1b3a34;border:1px solid #437864;color:#c8efd9;border-radius:99px;padding:.28rem .75rem;font-size:.82rem;font-weight:800}.active-badge:before{content:"";height:.48rem;width:.48rem;border-radius:50%;background:#82d6a2;box-shadow:0 0 0 3px #82d6a226}.services{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.6rem}.service{display:flex;flex-direction:column;gap:.3rem;background:#122126;border:1px solid #30464b;border-radius:.55rem;padding:.75rem}.service span{font-size:.8rem;color:#b5c8c7}.service strong{font-size:.82rem;overflow-wrap:anywhere}.service .ready{color:#a8e0bc}.service .idle{color:#ced8d3}details{margin:1rem 0}summary{cursor:pointer;color:#f3bf80;font-weight:700}details table{font-size:.88rem}.home-footer{display:flex;gap:.6rem;flex-wrap:wrap;margin:1.5rem 0}.home-footer a{padding:.65rem .9rem;border:1px solid #49605e;border-radius:.6rem;text-decoration:none;background:#17282b}.home-footer a:hover{background:#24393a}.message{padding:.75rem 1rem;border-left:3px solid #e9b977;background:#24302d;border-radius:.3rem}@media(max-width:900px){.home-grid{grid-template-columns:1fr}.services{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:480px){.services{grid-template-columns:1fr 1fr}.home-actions button{width:100%}}"""
        home_style += ".update-card{display:grid;grid-template-columns:1fr auto;gap:1rem;align-items:center;border-color:#8a6843;background:linear-gradient(110deg,#27332d,#1a292c)}.update-card h2{margin:.35rem 0}.update-card p{max-width:65rem}.update-card form{margin:0}@media(max-width:740px){.update-card{grid-template-columns:1fr}}"
        home_style += ".message-banner{display:flex;align-items:center;justify-content:space-between;gap:1rem}.message-banner p,.message-banner form{margin:0}.message-banner button{min-height:2.2rem;padding:.3rem .7rem;background:transparent;border-color:#c5a976;color:#f8d9a5;white-space:nowrap}.message-banner button:hover{background:#3a4437}@media(max-width:740px){.message-banner{align-items:flex-start;flex-direction:column}}"
        home_style += ".gpu-frame{width:100%;height:345px;border:0;background:#17262a}.gpu-note{font-size:.9rem;color:#b6c6c4}@media(max-width:740px){.gpu-frame{height:440px}}"
        page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{'<meta http-equiv="refresh" content="5">' if context_status['busy'] or webui_status['checking'] or webui_status['busy'] else ''}
<title>AI Model Switcher</title><style>{benchmark_views.STYLE}{home_style}</style>
<main><nav aria-label="Main navigation"><a href="/">AI Model Switcher</a><a href="/models">Find models</a><a href="/benchmarks">Benchmarks</a><a href="/sharing">Client sharing</a></nav>
<header class="hero"><div class="hero-copy"><div class="eyebrow">Local AI / Control desk</div><h1>AI Model Switcher</h1><p>Manage local workloads and model context from one place.</p><div class="active-line"><span class="active-badge">Active workload</span><strong>{h(mode)}</strong></div></div></header>
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
{'' if not any(available_builtin(item) for item in ('llm','qwen4b','huihui','flint-iq4','flint-q3','deepseek','qwen38','gemma4','vision','media')) else '<section class="card"><h2>Live GPU status</h2><p class="gpu-note">GPU use and VRAM use over recent samples. Temperature and power are current readings. This panel refreshes every five seconds without reloading the switcher controls.</p><iframe class="gpu-frame" src="/gpu/graph" title="Live GPU utilization and memory graph" loading="lazy"></iframe></section>'}
<p class="note">If a service fails, check ai-control.sh huihui-logs or media-logs. If Open WebUI still shows local-coder after switching, set its connection URL to http://nginx:8080/v1 in Admin → Settings → Connections.</p>
<div class="home-footer"><a href="/models">Find models</a><a href="/benchmarks">Benchmarks and results</a><a href="/sharing">Client sharing and rate limits</a><a href="{h(WEB_URL)}">Open WebUI</a>{f'<a href="{h(COMFY_URL)}">ComfyUI</a>' if available_builtin('media') else ''}</div></main></html>"""
        data = page.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; frame-src 'self'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path)
        if path.path == "/gpu/graph":
            self._send_html(GPU_MONITOR.render(), embeddable=True)
            return
        if path.path == "/sharing":
            try:
                hardware = MODEL_MANAGER.hardware()
                settings = SHARING.settings(hardware, active_server_slots())
                notice = {"limits_saved": "Sharing limits saved and confirmed by the API gateway.",
                          "defaults_restored": "Detected hardware defaults restored and confirmed by the API gateway.",
                          "key_revoked": "Client key revoked. New requests using it are denied.",
                          "slot_test_started": "Model slot test started. This page will report readiness or rollback."}.get(
                              urllib.parse.parse_qs(path.query).get("status", [""])[0], "")
                self._send_html(sharing_views.index(settings, SHARING.clients(), CSRF, hardware, active_server_slots(),
                                                    active_mode(), PARALLEL_TUNER.status(), message=notice))
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
                self._send_html(model_views.index(hardware, suggestions, MODEL_MANAGER.installed(), MODEL_MANAGER.jobs(), CSRF,
                                                  prefer_ablit, media_suggestions, MEDIA_MANAGER.jobs(), COMFY_URL,
                                                  DISCOVERY.latest(hardware), ENGINE_UPDATES.status(), DISCOVERY.latest_search(hardware),
                                                  MODEL_MANAGER.hybrid_enabled()))
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
                if running(QWEN4B):
                    backend = "qwen4b:8000"
                elif running(FLINT_IQ4):
                    backend = "flint-iq4:8080"
                elif running(FLINT_Q3):
                    backend = "flint-q3:8080"
                elif running(DEEPSEEK):
                    backend = "deepseek-v4-distill:8080"
                elif running(QWEN38):
                    backend = "qwen38-ablit:8080"
                elif running(GEMMA4):
                    backend = "gemma4-ablit:8080"
                elif running(HUIHUI):
                    backend = "huihui:8080"
                elif running(CPU_4B):
                    backend = "cpu-4b:8080"
                elif running(CPU_TINY):
                    backend = "cpu-tiny:8080"
                elif running(VISION):
                    backend = "vision:8000"
                else:
                    backend = next((item["container"] + (":8000" if item.get("format") == "safetensors" else ":8080") for item in MODEL_MANAGER.installed()
                                    if running(item["container"])), "llm:8000")
            except Exception:
                self.send_error(503, "Docker status unavailable")
                return
            self.send_response(200)
            self.send_header("X-LLM-Backend", backend)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path != "/":
            self.send_error(404)
            return
        WEBUI_UPDATES.check_async()
        self._page()

    def do_POST(self):
        if self.path not in ("/switch", "/benchmark", "/context/tune", "/benchmarks/delete", "/models/install",
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
