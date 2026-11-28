#!/usr/bin/env python3
"""Authenticated-internal HTTP front end and persistent task workers."""
import base64
import binascii
import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from multimodal_jobs import JobStore, CPU_KINDS, GPU_KINDS
import multimodal_workers as workers


ROOT = os.environ.get("MULTIMODAL_DATA", "/app/data/multimodal")
STORE = JobStore(ROOT + "/jobs.sqlite3", ROOT + "/artifacts")
TOKEN = os.environ["MULTIMODAL_INTERNAL_KEY"]
CONTROLLER = os.environ.get("MULTIMODAL_CONTROLLER", "http://gpu-control:8001")
ROUTES = {"/v1/documents/parse": "pdf", "/v1/ocr": "ocr",
          "/v1/audio/transcriptions": "transcription", "/v1/audio/speech": "speech",
          "/v1/images/generations": "image"}


def controller(action, ident):
    body = json.dumps({"job_id": ident}).encode()
    request = urllib.request.Request(CONTROLLER + "/multimodal/" + action, data=body,
                                     headers={"Authorization": "Bearer " + TOKEN,
                                              "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return json.load(response)


def worker(kinds):
    while True:
        job = STORE.claim(kinds)
        if job is None:
            time.sleep(1)
            continue
        leased = False
        try:
            payload = STORE.input(job)
            kind = job["kind"]
            if kind == "pdf":
                STORE.complete(job["id"], workers.pdf(job, payload))
            elif kind == "ocr":
                STORE.complete(job["id"], workers.ocr(job, payload))
            elif kind == "transcription":
                STORE.complete(job["id"], workers.transcription(job, payload))
            elif kind == "speech":
                result, artifact, mime = workers.speech(job)
                STORE.complete(job["id"], result, artifact, mime)
            elif kind == "image":
                STORE.update(job["id"], stage="waiting for GPU")
                controller("acquire", job["id"])
                leased = True
                STORE.update(job["id"], stage="loading ComfyUI")
                ready_by = time.monotonic() + 300
                while True:
                    try:
                        workers.comfy_request("/system_stats", timeout=5)
                        break
                    except Exception:
                        if time.monotonic() >= ready_by:
                            raise TimeoutError("ComfyUI did not become ready within five minutes")
                        time.sleep(5)
                result, artifact, mime = workers.image(job, STORE.update)
                STORE.update(job["id"], stage="restoring language model")
                release = controller("release", job["id"])
                leased = False
                restored = release.get("restored")
                if restored and restored != "media":
                    ready_by = time.monotonic() + 300
                    while True:
                        with urllib.request.urlopen(CONTROLLER + "/workload/status", timeout=10) as response:
                            state = json.load(response)
                        if state.get("mode") == restored and state.get("state") == "ready":
                            break
                        if time.monotonic() >= ready_by:
                            raise TimeoutError("Previous language model did not become ready within five minutes")
                        time.sleep(5)
                STORE.complete(job["id"], result, artifact, mime)
        except Exception as exc:
            STORE.fail(job["id"], str(exc))
        finally:
            if leased:
                try:
                    controller("release", job["id"])
                except Exception as exc:
                    STORE.update(job["id"], stage="completed; previous model restore failed: " + str(exc)[:160])


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def identity(self):
        if self.headers.get("X-Internal-Token") != TOKEN:
            self.reply(403, {"error": {"message": "Internal access only"}})
            return None
        owner = self.headers.get("X-AI-Client-ID", "")
        if not owner or len(owner) > 120:
            self.reply(400, {"error": {"message": "Missing client identity"}})
            return None
        return owner

    def do_POST(self):
        owner = self.identity()
        if owner is None:
            return
        kind = ROUTES.get(self.path)
        if not kind:
            self.reply(404, {"error": {"message": "Unknown task route"}})
            return
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or not 1 <= int(length) <= 24 * 1024 * 1024:
            self.reply(413, {"error": {"message": "Request body must be 1–24 MiB"}})
            return
        try:
            request = json.loads(self.rfile.read(int(length)))
            if not isinstance(request, dict):
                raise ValueError("JSON object required")
            profile_path = Path(ROOT) / "active_profile.json"
            profile = json.loads(profile_path.read_text()) if profile_path.is_file() else {}
            if kind == "transcription":
                request.setdefault("model", profile.get("asr_model", "tiny"))
            if kind == "image":
                if not profile.get("image"):
                    raise ValueError("Load a Creative studio profile before requesting an image")
                request.setdefault("checkpoint", profile.get("image_checkpoint"))
                request.setdefault("width", 1024)
                request.setdefault("height", 1024)
                request.setdefault("steps", 20)
            data = request.pop("data", None)
            if kind in {"pdf", "ocr", "transcription"}:
                if not isinstance(data, str):
                    raise ValueError("Base64 data field is required")
                payload = base64.b64decode(data, validate=True)
                if not 1 <= len(payload) <= 16 * 1024 * 1024:
                    raise ValueError("Input must be 1–16 MiB")
                if kind == "pdf" and not payload.startswith(b"%PDF-"):
                    raise ValueError("Input is not a PDF")
            else:
                payload = b""
                if data is not None:
                    raise ValueError("This task does not accept uploaded data")
            job = STORE.submit(owner, kind, request, payload)
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            self.reply(400, {"error": {"message": str(exc)}})
            return
        self.reply(202, {"id": job["id"], "status": "queued", "kind": kind,
                         "status_url": "/v1/jobs/" + job["id"]})

    def do_GET(self):
        owner = self.identity()
        if owner is None:
            return
        path = self.path.split("?", 1)[0]
        if path == "/v1/jobs":
            self.reply(200, {"data": [self.public(job) for job in STORE.list(owner)]})
            return
        parts = path.strip("/").split("/")
        if len(parts) not in (3, 4) or parts[:2] != ["v1", "jobs"]:
            self.reply(404, {"error": {"message": "Unknown task route"}})
            return
        try:
            job = STORE.get(parts[2], owner)
        except ValueError:
            job = None
        if job is None:
            self.reply(404, {"error": {"message": "Job not found"}})
            return
        if len(parts) == 3:
            self.reply(200, self.public(job))
            return
        if parts[3] != "result" or job["status"] != "completed" or not job["result_name"]:
            self.reply(404, {"error": {"message": "Result not available"}})
            return
        data = STORE.artifact(job)
        self.send_response(200)
        self.send_header("Content-Type", job["result_type"] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def public(job):
        return {key: job[key] for key in ("id", "kind", "status", "created_at", "updated_at",
                                        "started_at", "completed_at", "attempts", "stage", "result", "error")}


if __name__ == "__main__":
    for kinds in (CPU_KINDS, GPU_KINDS):
        threading.Thread(target=worker, args=(kinds,), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8003), Handler).serve_forever()
