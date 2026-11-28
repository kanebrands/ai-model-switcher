#!/usr/bin/env python3
"""Streaming OpenAI-compatible proxy with per-client admission control."""
import http.client
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sharing import SharingManager, FIELDS


DB_PATH = os.environ.get("BENCHMARK_DB", "/app/data/benchmarks.sqlite3")
LEGACY_KEY = os.environ["BENCHMARK_API_KEY"]
SHARING = SharingManager(DB_PATH, read_only=True)
USAGE = None  # Initialized only in the server process, after the database mount is ready.
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade"}
BACKEND = re.compile(r"[a-z0-9-]+:(?:8000|8080)\Z")
STABLE_MODEL_ID = "local-llm-api"
MEDIA_POST_ROUTES = {"/v1/documents/parse", "/v1/ocr", "/v1/audio/transcriptions",
                     "/v1/audio/speech", "/v1/images/generations", "/v1/images/edits",
                     "/v1/videos/generations"}


def is_media_route(path, method):
    route = path.split("?", 1)[0]
    return (method == "POST" and route in MEDIA_POST_ROUTES or
            method == "GET" and (route == "/v1/jobs" or route.startswith("/v1/jobs/")) or
            method == "DELETE" and route.startswith("/v1/jobs/"))


def resolve_model_alias(raw, content_type, model_id):
    """Replace the stable client-facing ID only when a JSON request uses it."""
    if not raw or "application/json" not in content_type.lower():
        return raw
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(payload, dict) or payload.get("model") != STABLE_MODEL_ID:
        return raw
    if not model_id:
        raise RuntimeError("No active model ID")
    payload["model"] = model_id
    return json.dumps(payload, separators=(",", ":")).encode()


def list_model_alias(raw, model_id):
    """Advertise the stable ID beside the actual ID when that model is loaded."""
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return raw
    actual = next((item for item in payload["data"] if isinstance(item, dict) and item.get("id") == model_id), None)
    if not actual or any(isinstance(item, dict) and item.get("id") == STABLE_MODEL_ID for item in payload["data"]):
        return raw
    alias = {**actual, "id": STABLE_MODEL_ID}
    if isinstance(alias.get("aliases"), list):
        alias["aliases"] = [STABLE_MODEL_ID]
    payload["data"].append(alias)
    if isinstance(payload.get("models"), list):
        source = next((item for item in payload["models"] if isinstance(item, dict) and
                       (item.get("name") == model_id or item.get("model") == model_id)), None)
        if source:
            payload["models"].append({**source, "name": STABLE_MODEL_ID, "model": STABLE_MODEL_ID})
    return json.dumps(payload, separators=(",", ":")).encode()


def cap_output(path, raw, content_type, maximum):
    """Bound generation even when the client omits an output limit."""
    route = path.split("?", 1)[0]
    key = ("max_output_tokens" if route.endswith("/responses") else
           "max_tokens" if route.endswith(("/chat/completions", "/completions")) else
           "n_predict" if route == "/completion" else None)
    if not key:
        return raw
    if "application/json" not in content_type.lower():
        raise ValueError("Generation requests must use JSON")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid JSON request") from None
    if not isinstance(body, dict):
        raise ValueError("Generation request must be a JSON object")
    keys = ("max_tokens", "max_completion_tokens") if route.endswith("/chat/completions") else (key,)
    present = [name for name in keys if name in body]
    for name in present or [key]:
        value = body.get(name)
        if value is None or value == -1:
            body[name] = maximum
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        else:
            body[name] = min(value, maximum)
    if body.get("stream") is True and route.endswith(("/chat/completions", "/completions")):
        options = body.get("stream_options")
        if options is None:
            options = {}
        if isinstance(options, dict):
            options["include_usage"] = True
            body["stream_options"] = options
    return json.dumps(body, separators=(",", ":")).encode()


class UsageCollector:
    """Extract reported usage from JSON or SSE without retaining generated text."""
    def __init__(self, content_type):
        self.streaming = "text/event-stream" in (content_type or "").lower()
        self.buffer = b""
        self.input_tokens = None
        self.output_tokens = None

    def _accept(self, payload):
        if not isinstance(payload, dict):
            return
        usage = payload.get("usage") or (payload.get("response") or {}).get("usage")
        if not isinstance(usage, dict):
            return
        incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
        outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(incoming, int) and incoming >= 0:
            self.input_tokens = incoming
        if isinstance(outgoing, int) and outgoing >= 0:
            self.output_tokens = outgoing

    def feed(self, chunk):
        if not self.streaming:
            if len(self.buffer) + len(chunk) <= 2 * 1024 * 1024:
                self.buffer += chunk
            return
        self.buffer = (self.buffer + chunk).replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            event, self.buffer = self.buffer.split(b"\n\n", 1)
            data = b"\n".join(line[5:].strip() for line in event.splitlines() if line.startswith(b"data:"))
            if data and data != b"[DONE]" and len(data) <= 2 * 1024 * 1024:
                try:
                    self._accept(json.loads(data))
                except (ValueError, UnicodeDecodeError):
                    pass
        if len(self.buffer) > 2 * 1024 * 1024:
            self.buffer = self.buffer[-65536:]

    def result(self):
        if not self.streaming and self.buffer:
            try:
                self._accept(json.loads(self.buffer))
            except (ValueError, UnicodeDecodeError):
                pass
        return self.input_tokens, self.output_tokens


class Admission:
    def __init__(self):
        self.cv = threading.Condition()
        self.active = {}
        self.total_active = 0
        self.waiters = []
        self.tokens = {}

    def acquire(self, client_id, policy):
        with self.cv:
            now = time.monotonic()
            allowance, last = self.tokens.get(client_id, (float(policy["burst"]), now))
            allowance = min(float(policy["burst"]), allowance + (now - last) * policy["requests_per_minute"] / 60)
            if allowance < 1:
                self.tokens[client_id] = (allowance, now)
                return False, max(1, int((1 - allowance) * 60 / policy["requests_per_minute"] + 1))
            self.tokens[client_id] = (allowance - 1, now)
            if (self.total_active < policy["global_active"] and
                    self.active.get(client_id, 0) < policy["per_client_active"] and not self.waiters):
                self._grant(client_id)
                return True, 0
            if (sum(waiter[1] == client_id for waiter in self.waiters) >= policy["per_client_queued"] or
                    len(self.waiters) >= policy["global_queued"]):
                return False, 1
            waiter = (object(), client_id)
            self.waiters.append(waiter)
            deadline = now + policy["queue_wait_seconds"]
            while True:
                available = self.total_active < policy["global_active"] and self.active.get(client_id, 0) < policy["per_client_active"]
                first_eligible = next((item for item in self.waiters
                                       if self.active.get(item[1], 0) < policy["per_client_active"]), None)
                if available and first_eligible is waiter:
                    self.waiters.remove(waiter)
                    self._grant(client_id)
                    return True, 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.waiters.remove(waiter)
                    self.cv.notify_all()
                    return False, 1
                self.cv.wait(min(remaining, 1))

    def _grant(self, client_id):
        self.active[client_id] = self.active.get(client_id, 0) + 1
        self.total_active += 1

    def release(self, client_id):
        with self.cv:
            self.active[client_id] -= 1
            if not self.active[client_id]:
                del self.active[client_id]
            self.total_active -= 1
            self.cv.notify_all()


ADMISSION = Admission()


class GatewayHandler(BaseHTTPRequestHandler):
    def reply(self, status, message, retry=None):
        payload = json.dumps({"error": {"message": message, "type": "rate_limit_error" if status == 429 else "invalid_request_error"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if retry is not None:
            self.send_header("Retry-After", str(retry))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def do_DELETE(self):
        self.forward()

    def do_HEAD(self):
        self.forward()

    def forward(self):
        if self.path == "/gateway/health" and self.command == "GET":
            authorization = self.headers.get("Authorization", "")
            if not authorization.lower().startswith("bearer ") or authorization[7:].strip() != LEGACY_KEY:
                self.reply(401, "A controller API key is required")
                return
            policy = SHARING.settings()
            payload = json.dumps({"status": "ready", "policy": {key: policy[key] for key in FIELDS}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        health = self.path == "/health"
        client_id = "health" if health else None
        if not health:
            authorization = self.headers.get("Authorization", "")
            if not authorization.lower().startswith("bearer "):
                self.reply(401, "A client API key is required")
                return
            client_id = SHARING.identify(authorization[7:].strip(), LEGACY_KEY)
            if not client_id:
                self.reply(401, "Invalid or revoked client API key")
                return
        policy = SHARING.settings()
        length = self.headers.get("Content-Length", "0")
        if not length.isdecimal():
            self.reply(411, "Content-Length is required")
            return
        length = int(length)
        media = is_media_route(self.path, self.command)
        if length > (24 * 1024 * 1024 if media else policy["max_request_kib"] * 1024):
            self.reply(413, "Request exceeds the configured size limit")
            return
        raw = self.rfile.read(length) if length else b""
        if self.command == "POST" and not health:
            try:
                raw = cap_output(self.path, raw, self.headers.get("Content-Type", ""), policy["max_output_tokens"])
            except ValueError as exc:
                self.reply(400, str(exc))
                return
        admitted = False
        if self.command == "POST" and not health:
            admitted, retry = ADMISSION.acquire(client_id, policy)
            if not admitted:
                self.reply(429, "Client rate or concurrency limit reached", retry)
                return
        upstream = None
        collector = None
        response_status = 502
        started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        started_clock = time.monotonic()
        try:
            path = self.path
            if media:
                target = "multimodal:8003"
                model_id = None
            elif path.startswith("/vision/v1/"):
                target = "vision:8000"
                model_id = "local-vision"
                path = path[len("/vision"):]
            else:
                request = urllib.request.Request("http://gpu-control:8001/route")
                with urllib.request.urlopen(request, timeout=10) as routing:
                    target = routing.headers.get("X-LLM-Backend", "")
                    model_id = routing.headers.get("X-LLM-Model", "")
            if not media and not BACKEND.fullmatch(target):
                raise RuntimeError("No active model backend")
            if self.command == "POST" and not media:
                raw = resolve_model_alias(raw, self.headers.get("Content-Type", ""), model_id)
            host, port = target.rsplit(":", 1)
            upstream = http.client.HTTPConnection(host, int(port), timeout=600)
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in HOP_HEADERS | {"authorization", "host", "content-length"}}
            if media:
                headers["X-Internal-Token"] = LEGACY_KEY
                headers["X-AI-Client-ID"] = client_id
            else:
                headers["Authorization"] = "Bearer " + LEGACY_KEY
            headers["Host"] = target
            headers["Accept-Encoding"] = "identity"
            headers["Content-Length"] = str(len(raw))
            headers["Connection"] = "close"
            upstream.request(self.command, path, body=raw if self.command == "POST" else None, headers=headers)
            response = upstream.getresponse()
            response_status = response.status
            collector = UsageCollector(response.getheader("Content-Type")) if admitted else None
            model_list = (self.command == "GET" and path.split("?", 1)[0] == "/v1/models" and
                          response.status == 200 and response.getheader("Content-Encoding", "identity") == "identity")
            response_body = list_model_alias(response.read(), model_id) if model_list else None
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS and (response_body is None or key.lower() != "content-length"):
                    self.send_header(key, value)
            if response_body is not None:
                self.send_header("Content-Length", str(len(response_body)))
            self.send_header("X-AI-Client", "shared" if client_id == "legacy" else "dedicated" if client_id != "health" else "health")
            self.end_headers()
            if self.command != "HEAD":
                if response_body is not None:
                    self.wfile.write(response_body)
                else:
                    while chunk := response.read1(65536):
                        if collector:
                            collector.feed(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                if not self.wfile.closed and not getattr(self, "_response_started", False):
                    self.reply(502, "Model backend unavailable")
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            if upstream:
                upstream.close()
            if admitted:
                try:
                    if USAGE:
                        incoming, outgoing = collector.result() if collector else (None, None)
                        USAGE.record_usage(client_id, started_at, response_status,
                                           time.monotonic() - started_clock, incoming, outgoing)
                except (OSError, sqlite3.Error, ValueError, RuntimeError):
                    pass  # Telemetry must never break a client response or hold its slot.
                ADMISSION.release(client_id)

    def end_headers(self):
        super().end_headers()
        self._response_started = True


if __name__ == "__main__":
    USAGE = SharingManager(DB_PATH)
    ThreadingHTTPServer(("0.0.0.0", 8002), GatewayHandler).serve_forever()
