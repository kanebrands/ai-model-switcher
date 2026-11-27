"""Exercise per-client limits, streaming proxying, and portable GPU reporting."""
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from gpu_metrics import GPUMonitor, parse_samples
from parallel_tuning import ParallelTuner
from sharing import SharingManager, defaults, FIELDS
from sharing_views import index as sharing_page
from harness_views import index as harness_page

with patch.dict(os.environ, {"BENCHMARK_API_KEY": "legacy-test-key"}):
    import fairness_gateway as gateway


class SharingTests(unittest.TestCase):
    def test_opencode_config_uses_current_api_base_and_stable_model_id(self):
        page = harness_page("http://switcher.test:9123/v1", 32768)
        self.assertIn("~/.config/opencode/opencode.json", page)
        self.assertIn("http://switcher.test:9123/v1", page)
        self.assertIn("@opencode/ai/providers/openai-compatible", page)
        self.assertIn("local-llm-api", page)
        self.assertNotIn("YOUR_ACTUAL_KEY", page)

    def test_hardware_defaults_follow_capacity_and_active_server(self):
        small = {"fingerprint": "small", "vram_mib": 8192, "cpu_threads": 8}
        large = {"fingerprint": "large", "vram_mib": 49152, "cpu_threads": 32}
        self.assertEqual(defaults(small, 4)["global_active"], 1)
        self.assertEqual(defaults(large, 4)["global_active"], 4)
        with tempfile.TemporaryDirectory() as directory:
            manager = SharingManager(directory + "/sharing.db")
            self.assertEqual(manager.settings(large, 1)["global_active"], 1)
            self.assertEqual(manager.settings(large, 4)["global_active"], 4)
            values = {key: manager.settings()[key] for key in FIELDS}
            values["requests_per_minute"] = 15
            manager.save_settings(values)
            self.assertEqual(manager.settings(small, 1)["requests_per_minute"], 15)

    def test_client_keys_are_distinct_revocable_and_not_stored_in_plaintext(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/sharing.db"
            manager = SharingManager(path)
            created = manager.create_client("Agent one")
            self.assertEqual(manager.identify(created["token"], "legacy"), created["id"])
            with open(path, "rb") as data:
                self.assertNotIn(created["token"], data.read().decode("latin1"))
            self.assertTrue(manager.revoke(created["id"]))
            self.assertIsNone(manager.identify(created["token"], "legacy"))

    def test_rate_and_active_limits_reject_excess(self):
        policy = defaults({"vram_mib": 49152, "cpu_threads": 32}, 2)
        policy["per_client_queued"] = 0
        limiter = gateway.Admission()
        self.assertTrue(limiter.acquire("one", policy)[0])
        self.assertFalse(limiter.acquire("one", policy)[0])
        self.assertTrue(limiter.acquire("two", policy)[0])
        limiter.release("one")
        limiter.release("two")

    def test_generation_output_is_capped(self):
        raw = b'{"model":"test","messages":[],"max_tokens":99999}'
        capped = gateway.cap_output("/v1/chat/completions", raw, "application/json", 2048)
        self.assertEqual(json.loads(capped)["max_tokens"], 2048)
        self.assertEqual(json.loads(gateway.cap_output("/v1/responses", b'{}', "application/json", 512))["max_output_tokens"], 512)
        streaming = gateway.cap_output("/v1/chat/completions", b'{"stream":true}', "application/json", 512)
        self.assertTrue(json.loads(streaming)["stream_options"]["include_usage"])

    def test_usage_collector_reads_json_and_split_sse_without_storing_text(self):
        collector = gateway.UsageCollector("text/event-stream")
        collector.feed(b'data: {"choices":[{"delta":{"content":"secret"}}]}\n\ndata: {"usage":{"prompt_tokens":12,')
        collector.feed(b'"completion_tokens":5}}\n\ndata: [DONE]\n\n')
        self.assertEqual(collector.result(), (12, 5))
        responses = gateway.UsageCollector("application/json")
        responses.feed(b'{"usage":{"input_tokens":20,"output_tokens":7}}')
        self.assertEqual(responses.result(), (20, 7))

    def test_per_key_window_counts_and_inline_stats(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SharingManager(directory + "/sharing.db")
            client = manager.create_client("Deck Builder")
            from sharing import now
            manager.record_usage(client["id"], now(), 200, 2.0, 100, 50)
            manager.record_usage(client["id"], now(), 200, 1.0, None, None)
            manager.record_usage("legacy", now(), 200, 1.0, 8, 4)
            report = manager.usage_summary(client["id"], 1, "hours")
            self.assertEqual(report["totals"]["requests"], 2)
            self.assertEqual(report["totals"]["input_tokens"], 100)
            self.assertEqual(report["totals"]["output_tokens"], 50)
            self.assertEqual(report["totals"]["tokens_per_second"], 25.0)
            self.assertEqual(report["totals"]["input_measured"], 1)
            self.assertEqual(len(report["points"]), 30)
            hardware = {"vram_mib": 0, "ram_gib": 16, "cpu_threads": 8}
            settings = manager.settings(hardware, 1)
            clients = manager.clients()
            page = sharing_page(settings, clients, "csrf", hardware, 1,
                                all_usage=manager.usage_summary("all", 1, "hours"),
                                client_usage={client["id"]: report,
                                              "legacy": manager.usage_summary("legacy", 1, "hours")})
            self.assertRegex(page, r'Deck Builder</td>.*?</tr><tr class="client-usage-row">')
            self.assertIn('<details class="client-usage-details"><summary>Usage for Deck Builder</summary>', page)
            self.assertIn("25.00", page)
            self.assertIn("Input tokens", page)
            self.assertNotIn("View usage", page)
            self.assertEqual(page.count('name="all_amount"'), 1)
            self.assertEqual(page.count('name="all_unit"'), 1)
            self.assertIn('class="plot-grid"', page)

    def test_all_key_usage_includes_legacy_and_revoked_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SharingManager(directory + "/sharing.db")
            first = manager.create_client("Agent one")
            second = manager.create_client("Agent two")
            from sharing import now
            manager.record_usage(first["id"], now(), 200, 2.0, 10, 4)
            manager.record_usage(second["id"], now(), 200, 1.0, 20, 6)
            manager.record_usage("legacy", now(), 200, 2.0, 30, 10)
            manager.record_usage(second["id"], now(), 200, 1.0, None, None)
            manager.revoke(first["id"])
            aggregate = manager.usage_summary("all", 1, "hours")
            self.assertEqual(aggregate["totals"]["requests"], 4)
            self.assertEqual(aggregate["totals"]["input_tokens"], 60)
            self.assertEqual(aggregate["totals"]["output_tokens"], 20)
            self.assertEqual(aggregate["totals"]["tokens_per_second"], 4.0)
            self.assertEqual(aggregate["totals"]["input_measured"], 3)
            self.assertEqual(manager.usage_summary(second["id"], 1, "hours")["totals"]["input_tokens"], 20)
            hardware = {"vram_mib": 0, "ram_gib": 16, "cpu_threads": 8}
            page = sharing_page(manager.settings(hardware, 1), manager.clients(), "csrf", hardware, 1,
                                all_usage=aggregate,
                                client_usage={"legacy": manager.usage_summary("legacy", 1, "hours"),
                                              first["id"]: manager.usage_summary(first["id"], 1, "hours"),
                                              second["id"]: manager.usage_summary(second["id"], 1, "hours")})
            self.assertIn('id="all-key-usage"', page)
            self.assertIn('name="all_amount"', page)
            self.assertIn('name="all_unit"', page)
            self.assertIn("Usage for all API keys", page)
            self.assertIn("Effective output tokens/s", page)
            self.assertEqual(page.count('class="client-usage-row"'), 3)
            self.assertEqual(page.count('<details class="client-usage-details">'), 3)
            self.assertNotIn('<details class="client-usage-details" open', page)
            self.assertIn('width:9rem;min-width:9rem;max-width:9rem', page)

    def test_gpu_parser_accepts_optional_power_and_multiple_devices(self):
        samples = parse_samples("0, GPU A, 52, 8000, 24000, 61, 210.5\n1, GPU B, 0, 10, 16000, N/A, N/A",
                                "2026-11-26T15:00:00+00:00")
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0]["util"], 52)
        self.assertIsNone(samples[1]["power"])

    def test_gpu_graph_reports_live_utilization_and_memory(self):
        monitor = GPUMonitor()
        with patch("gpu_metrics.docker_api", return_value=[{"Names": ["/local-ai-gemma4-ablit"]}]), \
             patch("gpu_metrics.docker_exec", return_value="0, Test GPU, 54, 12000, 24000, 65, 220"):
            page = monitor.render()
        self.assertIn("GPU use <strong>54%</strong>", page)
        self.assertIn("VRAM <strong>12000 / 24000 MiB</strong>", page)
        self.assertIn('>25%</text>', page)
        self.assertIn('>75%</text>', page)
        self.assertIn('class="plot-grid"', page)

    def test_failed_parallel_restart_attempts_rollback_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SharingManager(directory + "/settings.db")
            tuner = ParallelTuner(directory + "/settings.db", manager, "test")
            current = {"mode": "gemma4", "container": "local-ai-gemma4-ablit", "slots": 1, "alias": "local-gemma4-ablit"}
            with tuner.connect() as db:
                db.execute("INSERT INTO parallel_jobs(id,mode,previous_slots,requested_slots,started_at,status,stage) VALUES(?,?,?,?,?,?,?)",
                           ("job", "gemma4", 1, 2, "2026-11-26T15:00:00+00:00", "running", "queued"))
            calls = []
            def helper(action, *_):
                calls.append(action)
                if action == "apply":
                    raise RuntimeError("model could not load")
            with patch("parallel_tuning.docker_api", return_value={"Id": "old-id"}), \
                 patch.object(tuner, "_helper", side_effect=helper), \
                 patch.object(tuner, "_wait_ready", return_value=True):
                tuner._run("job", current, 2)
            self.assertEqual(calls, ["apply", "rollback"])
            self.assertEqual(tuner.status()["job"]["status"], "failed")


class GatewayIntegrationTests(unittest.TestCase):
    def test_dedicated_key_streams_and_rewrites_backend_auth(self):
        seen = {}
        class Backend(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps({"object": "list", "data": [{"id": "backend-model", "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                seen["auth"] = self.headers["Authorization"]
                seen["body"] = json.loads(self.rfile.read(length))
                data = b'data: {"ok":true}\n\ndata: {"usage":{"prompt_tokens":9,"completion_tokens":4}}\n\ndata: [DONE]\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:18]); self.wfile.flush()
                self.wfile.write(data[18:]); self.wfile.flush()
            def log_message(self, *_):
                pass
        class Route:
            def __init__(self, target):
                self.headers = {"X-LLM-Backend": target, "X-LLM-Model": "backend-model"}
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        try:
            backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
            gateway_server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        except PermissionError:
            self.skipTest("Loopback socket binding is unavailable in this sandbox")
        with tempfile.TemporaryDirectory() as directory:
            manager = SharingManager(directory + "/sharing.db")
            manager.settings({"fingerprint": "test", "vram_mib": 24576, "cpu_threads": 8}, 1)
            client = manager.create_client("code agent")
            threads = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (backend, gateway_server)]
            for thread in threads:
                thread.start()
            try:
                usage_recorded = threading.Event()
                original_record_usage = manager.record_usage
                def record_usage(*args, **kwargs):
                    original_record_usage(*args, **kwargs)
                    usage_recorded.set()
                with patch.object(gateway, "SHARING", manager), \
                     patch.object(gateway, "USAGE", manager), \
                     patch.object(manager, "record_usage", side_effect=record_usage), \
                     patch.object(gateway, "BACKEND", re.compile(r"127\.0\.0\.1:\d+\Z")), \
                     patch.object(gateway.urllib.request, "urlopen", return_value=Route(f"127.0.0.1:{backend.server_port}")):
                    conn = http.client.HTTPConnection("127.0.0.1", gateway_server.server_port, timeout=5)
                    body = json.dumps({"model": "local-llm-api", "messages": [], "stream": True, "max_tokens": 90000})
                    conn.request("POST", "/v1/chat/completions", body=body,
                                 headers={"Authorization": "Bearer " + client["token"], "Content-Type": "application/json"})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"[DONE]", response.read())
                    conn.close()
                    conn = http.client.HTTPConnection("127.0.0.1", gateway_server.server_port, timeout=5)
                    conn.request("GET", "/v1/models", headers={"Authorization": "Bearer " + client["token"]})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual({item["id"] for item in json.loads(response.read())["data"]},
                                     {"backend-model", "local-llm-api"})
                    conn.close()
                    self.assertTrue(usage_recorded.wait(5), "Gateway did not finish recording usage")
                self.assertEqual(seen["auth"], "Bearer legacy-test-key")
                self.assertEqual(seen["body"]["model"], "backend-model")
                self.assertEqual(seen["body"]["max_tokens"], 8192)
                self.assertTrue(seen["body"]["stream_options"]["include_usage"])
                totals = manager.usage_summary(client["id"], 1, "hours")["totals"]
                self.assertEqual((totals["input_tokens"], totals["output_tokens"]), (9, 4))
            finally:
                gateway_server.shutdown(); backend.shutdown()
                gateway_server.server_close(); backend.server_close()
