from __future__ import annotations
import io
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock
from urllib import request, error
from benchmarks import api_broker
from benchmarks.api_broker import APIBroker, CampaignBudget, BudgetStopped


class Response(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()


class SlowResponse:
    """A deterministic slow upstream: never touches the network."""
    def __init__(self, head, tail):
        self.head, self.tail = head, tail
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.phase = 0

    def read1(self, length):
        if self.phase == 0:
            self.phase = 1
            return self.head[:length]
        if self.phase == 1:
            self.waiting.set()
            if not self.release.wait(5):
                raise TimeoutError("offline slow fixture")
            self.phase = 2
            return b"" if self.closed.is_set() else self.tail[:length]
        return b""

    read = read1

    def close(self):
        self.closed.set()
        self.release.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.budget = CampaignBudget(self.root / "control")

    def relay(self, arm="codex_bridge"):
        broker = APIBroker({"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash",
                            "api_key": "offline-private-key"}, self.budget).start()
        self.addCleanup(broker.close)
        profile = broker.route(arm, self.root / "run")
        self.budget.start_run()
        return broker, profile

    def call(self, profile, payload, path="/chat/completions"):
        req = request.Request(profile["base_url"] + path, data=json.dumps(payload).encode(),
                              headers={"Authorization": "Bearer " + profile["api_key"],
                                       "Content-Type": "application/json"})
        with request.urlopen(req, timeout=3) as response:
            return response.read()

    def disconnected_handler(self, broker, profile, upstream, *, native=False):
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = upstream
        body = json.dumps({"messages": [{"role": "user", "content": "offline"}], "stream": True}).encode()
        handler = object.__new__(broker.server.RequestHandlerClass)
        handler.headers = {"Authorization": "Bearer " + profile["api_key"], "Content-Length": str(len(body))}
        handler.path = "/v1/messages" if native else "/v1/chat/completions"
        handler.connection = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler.send_response = handler.send_header = handler.end_headers = mock.Mock()
        errors = []

        def invoke():
            try:
                handler.do_POST()
            except BaseException as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=invoke, daemon=True)
        thread.start()
        self.assertTrue(upstream.waiting.wait(1))
        self.addCleanup(upstream.close)
        self.addCleanup(lambda: thread.join(timeout=1))
        return thread, errors

    def close_slow_stream(self, *, native, allow_tail):
        broker, profile = self.relay("deepseek_harness" if native else "codex_bridge")
        if native:
            head = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":7,"output_tokens":0,"cache_read_input_tokens":3}}}\n\n'
            tail = b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'
        else:
            head = b'data: {"choices":[{"delta":{"content":"offline"}}]}\n\n'
            tail = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\ndata: [DONE]\n\n'
        upstream = SlowResponse(head, tail)
        thread, errors = self.disconnected_handler(broker, profile, upstream, native=native)
        self.assertEqual(1, broker.pending_requests)
        self.assertEqual(1, self.budget.ledger.summary()["unfinished_requests"])
        if allow_tail:
            timer = threading.Timer(.05, upstream.release.set)
            timer.start()
            self.addCleanup(timer.cancel)
        result = broker.close(timeout_seconds=1 if allow_tail else 0)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(result["settled"])
        self.assertEqual(0, result["pending_requests"])
        self.assertEqual(0, broker.pending_requests)
        global_summary = self.budget.ledger.summary()
        local_summary = broker.routes[profile["api_key"]]["ledger"].summary()
        for summary in (global_summary, local_summary):
            self.assertEqual((1, 1, 0), tuple(summary[key] for key in ("requests_started", "requests_finished", "unfinished_requests")))
            self.assertEqual(1, summary["requests_failed"])
            self.assertEqual(allow_tail, summary["usage_complete"])
            self.assertEqual((12 if native else 9) if allow_tail else None, summary["total_tokens"])
        self.assertEqual(1, broker.upstream_opener.open.call_count)
        self.assertTrue(broker.close(timeout_seconds=0)["settled"])

    def test_close_drains_slow_native_usage_after_client_disconnect(self):
        self.close_slow_stream(native=True, allow_tail=True)

    def test_close_drains_slow_cc_usage_after_client_disconnect(self):
        self.close_slow_stream(native=False, allow_tail=True)

    def test_close_native_deadline_finishes_unknown_instead_of_leaving_pending(self):
        self.close_slow_stream(native=True, allow_tail=False)

    def test_close_cc_deadline_finishes_unknown_instead_of_leaving_pending(self):
        self.close_slow_stream(native=False, allow_tail=False)

    def test_settle_waits_without_permanently_stopping_budget_or_resending(self):
        broker, profile = self.relay()
        upstream = SlowResponse(b'data: {"choices":[]}\n\n',
                                b'data: {"usage":{"prompt_tokens":7,"completion_tokens":2},"choices":[]}\n\ndata: [DONE]\n\n')
        thread, errors = self.disconnected_handler(broker, profile, upstream)
        self.assertFalse(broker.settle(.01)["settled"])
        self.assertIsNone(self.budget.state["stop_reason"])
        upstream.release.set()
        result = broker.settle(1)
        thread.join(timeout=1)
        self.assertEqual([], errors)
        self.assertTrue(result["settled"])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(1, broker.upstream_opener.open.call_count)
        self.assertTrue(self.budget.check()["allowed"])

    def close_real_slow_http(self, *, native):
        broker, profile = self.relay("deepseek_harness" if native else "codex_bridge")
        release = threading.Event()
        calls = []
        if native:
            head = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
        else:
            head = b'data: {"choices":[{"delta":{"content":"offline"}}]}\n\n'

        class SlowUpstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                calls.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(head)
                self.wfile.flush()
                release.wait(3)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), SlowUpstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        endpoint = "http://127.0.0.1:" + str(upstream.server_port)
        broker.client.base_url = endpoint
        try:
            with mock.patch.object(api_broker, "NATIVE_MESSAGES_URL", endpoint + "/messages"):
                body = json.dumps({"messages": [{"role": "user", "content": "offline"}], "stream": True}).encode()
                req = request.Request(profile["base_url"] + ("/messages" if native else "/chat/completions"), data=body,
                                      headers={"Authorization": "Bearer " + profile["api_key"], "Content-Type": "application/json"})
                with request.urlopen(req, timeout=3) as response:
                    self.assertTrue(response.readline())
                self.assertEqual(1, broker.pending_requests)
                settled = broker.close(timeout_seconds=0)
                self.assertTrue(settled["settled"])
                self.assertEqual(0, settled["pending_requests"])
                self.assertFalse(settled["usage_complete"])
                self.assertEqual((1, 1, 0), tuple(self.budget.ledger.summary()[key] for key in
                                               ("requests_started", "requests_finished", "unfinished_requests")))
                self.assertEqual(1, len(calls))
        finally:
            release.set()
            upstream.shutdown(); upstream.server_close(); thread.join(timeout=1)

    def test_real_http_cc_disconnect_and_close_finishes_unknown_pair(self):
        self.close_real_slow_http(native=False)

    def test_real_http_native_disconnect_and_close_finishes_unknown_pair(self):
        self.close_real_slow_http(native=True)

    def test_budget_counts_runs_and_starts_time_only_at_first_run(self):
        self.assertIsNone(self.budget.state["started_epoch"])
        for number in range(18):
            self.assertEqual(number+1, self.budget.start_run()["runs_started"])
        with self.assertRaises(BudgetStopped):
            self.budget.start_run()

    def test_inflight_unknown_stops_new_requests(self):
        self.budget.ledger.start("deepseek", "deepseek-flash", "codex_bridge")
        self.assertIsNone(self.budget.estimate())
        with self.assertRaises(BudgetStopped):
            self.budget.check()

    def test_budget_conservative_cache_and_subset_math(self):
        rid = self.budget.ledger.start("deepseek", "deepseek-flash", "codex_bridge")
        self.budget.ledger.finish(rid, "ok", {"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
                                             "prompt_cache_hit_tokens": 200_000,
                                             "completion_tokens_details": {"reasoning_tokens": 200}})
        self.assertAlmostEqual((.8*.3+.2*.006+.1*1.2)*8, self.budget.estimate())
        with self.assertRaises(BudgetStopped):
            self.budget.check(reserve_cny=10)

    def test_sse_usage_and_request_settings_durable_without_content(self):
        broker, profile = self.relay()
        chunks = b'data: {"choices":[{"delta":{"content":"offline answer"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\ndata: [DONE]\n\n'
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(chunks)
        result = self.call(profile, {"messages": [{"role": "user", "content": "private prompt"}],
                                     "stream": True, "model": "wrong", "max_tokens": 40000})
        self.assertEqual(chunks, result)
        upstream = broker.upstream_opener.open.call_args.args[0]
        payload = json.loads(upstream.data)
        self.assertEqual("deepseek-flash", payload["model"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertEqual(8192, payload["max_tokens"])
        self.assertTrue(payload["stream_options"]["include_usage"])
        self.assertEqual(10, self.budget.ledger.summary()["total_tokens"])
        persisted = "".join(p.read_text() for p in self.root.rglob("*.json*"))
        for secret in ["offline-private-key", "private prompt", "offline answer", profile["api_key"]]:
            self.assertNotIn(secret, persisted)

    def test_unapproved_routes_and_server_tools_never_call_upstream(self):
        broker, profile = self.relay()
        broker.upstream_opener = mock.Mock()
        for path, payload in [("/search", {"messages": []}),
                              ("/chat/completions", {"messages": [], "tools": [{"type": "web_search"}]})]:
            with self.subTest(path=path), self.assertRaises(error.HTTPError):
                self.call(profile, payload, path)
        broker.upstream_opener.open.assert_not_called()
        self.assertEqual(0, self.budget.ledger.summary()["requests_started"])

    def test_http_failure_is_unknown_and_not_retried(self):
        broker, profile = self.relay()
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.side_effect = OSError("untrusted error")
        with self.assertRaises(error.HTTPError):
            self.call(profile, {"messages": [{"role": "user", "content": "offline input"}]})
        self.assertEqual(1, broker.upstream_opener.open.call_count)
        self.assertIsNone(self.budget.estimate())
        self.assertEqual(1, self.budget.ledger.summary()["requests_failed"])

    def test_private_options_and_multiple_completions_never_reach_upstream(self):
        broker, profile = self.relay()
        broker.upstream_opener = mock.Mock()
        variants = [{"n": 2}, {"best_of": 100}, {"enable_search": True}, {"web_search_options": {}},
                    {"max_tokens": True}, {"stream": "true"},
                    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://other.invalid"}}]}]}]
        for options in variants:
            with self.subTest(options=options), self.assertRaises(error.HTTPError):
                self.call(profile, {"messages": [{"role": "user", "content": "offline"}], **options})
        broker.upstream_opener.open.assert_not_called()
        self.assertEqual(0, self.budget.ledger.summary()["requests_started"])

    def test_completion_alias_is_clamped_and_cannot_override_reservation(self):
        broker, profile = self.relay()
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(b'{"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}')
        self.call(profile, {"messages": [{"role": "user", "content": "offline"}], "max_completion_tokens": 100000, "n": 1})
        body = json.loads(broker.upstream_opener.open.call_args.args[0].data)
        self.assertEqual(8192, body["max_tokens"])
        self.assertNotIn("max_completion_tokens", body)
        self.assertEqual(1, body["n"])

    def test_error_response_with_usage_is_charged_and_body_not_echoed(self):
        broker, profile = self.relay()
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(b'{"error":{"message":"PRIVATE_UPSTREAM"},"usage":{"prompt_tokens":7,"completion_tokens":3}}')
        with self.assertRaises(error.HTTPError) as failure:
            self.call(profile, {"messages": [{"role": "user", "content": "offline"}]})
        self.assertNotIn(b"PRIVATE_UPSTREAM", failure.exception.read())
        self.assertEqual(10, self.budget.ledger.summary()["total_tokens"])
        self.assertEqual(1, self.budget.ledger.summary()["requests_failed"])

    def test_response_byte_limit_stops_future_submissions_without_retry(self):
        broker, profile = self.relay()
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(b"x" * 81)
        with mock.patch.object(api_broker, "MAX_RESPONSE_BYTES", 80), self.assertRaises(error.HTTPError):
            self.call(profile, {"messages": [{"role": "user", "content": "offline"}]})
        with self.assertRaises(error.HTTPError):
            self.call(profile, {"messages": [{"role": "user", "content": "offline"}]})
        self.assertEqual(1, broker.upstream_opener.open.call_count)
        self.assertIsNone(self.budget.estimate())

    def test_client_disconnect_still_drains_final_usage_tail(self):
        broker, profile = self.relay()
        chunks = b'data: {"choices":[{"delta":{"content":"offline"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(chunks)
        body = json.dumps({"messages": [{"role": "user", "content": "offline"}], "stream": True}).encode()
        handler = object.__new__(broker.server.RequestHandlerClass)
        handler.headers = {"Authorization": "Bearer " + profile["api_key"], "Content-Length": str(len(body))}
        handler.path = "/v1/chat/completions"
        handler.connection = mock.Mock()
        handler.rfile = io.BytesIO(body)
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = BrokenPipeError()
        handler.send_response = handler.send_header = handler.end_headers = mock.Mock()
        handler.do_POST()
        self.assertEqual(10, self.budget.ledger.summary()["total_tokens"])
        self.assertEqual(1, self.budget.ledger.summary()["requests_failed"])
        self.assertEqual(1, broker.upstream_opener.open.call_count)

    def test_budget_restart_cannot_relax_existing_limits(self):
        restored = CampaignBudget(self.budget.root, max_runs=100, max_seconds=999999, max_cny=100)
        self.assertEqual((18, 7200, 10), (restored.max_runs, restored.max_seconds, restored.max_cny))

    def test_stream_read_has_total_wall_limit_not_only_idle_timeout(self):
        with mock.patch.object(api_broker.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaises(TimeoutError):
                list(api_broker._bounded_chunks(Response(b"x"), 1))

    def test_native_messages_relay_preserves_protocol_and_normalises_cache_once(self):
        broker, profile = self.relay("deepseek_harness")
        stream = b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":0,"cache_read_input_tokens":40,"cache_creation_input_tokens":2}}}\n\nevent: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":10}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(stream)
        payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "offline"}]}],
                   "system": [{"type": "text", "text": "fixture system"}], "stream": True,
                   "tools": [{"name": "read_file", "description": "fixture", "parameters": {"type": "object"}}],
                   "dsh_session_log": [], "dsh_plugin_packages": [], "max_tokens": 30000}
        self.assertEqual(stream, self.call(profile, payload, "/messages"))
        upstream = broker.upstream_opener.open.call_args.args[0]
        self.assertEqual("https://api.deepseek.com/anthropic/v1/messages", upstream.full_url)
        self.assertEqual("offline-private-key", upstream.get_header("X-api-key"))
        self.assertIsNone(upstream.get_header("Authorization"))
        self.assertNotIn(profile["api_key"], str(upstream.headers))
        self.assertEqual("2023-06-01", upstream.get_header("Anthropic-version"))
        body = json.loads(upstream.data)
        self.assertEqual(8192, body["max_tokens"])
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertEqual(payload["tools"], body["tools"])
        totals = self.budget.ledger.summary()
        self.assertEqual((142, 40, 10, 152), tuple(totals[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")))

    def test_native_start_only_is_unknown_not_zero_output_bill(self):
        broker, profile = self.relay("deepseek_harness")
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100,"output_tokens":0}}}\n\n')
        self.call(profile, {"messages": [{"role": "user", "content": "offline"}], "stream": True}, "/messages")
        self.assertIsNone(self.budget.ledger.summary()["total_tokens"])
        self.assertEqual(1, self.budget.ledger.summary()["requests_failed"])

    def test_native_server_tools_and_remote_content_are_denied(self):
        broker, profile = self.relay("deepseek_harness")
        broker.upstream_opener = mock.Mock()
        for options in [{"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
                        {"messages": [{"role": "user", "content": [{"type": "document", "source": {"url": "https://remote.invalid"}}]}]}]:
            with self.subTest(options=options), self.assertRaises(error.HTTPError):
                self.call(profile, {"messages": [{"role": "user", "content": "offline"}], **options}, "/messages")
        broker.upstream_opener.open.assert_not_called()

    def test_native_x_api_key_uses_random_route_key_and_cannot_switch_protocol(self):
        broker, profile = self.relay("deepseek_harness")
        broker.upstream_opener = mock.Mock()
        broker.upstream_opener.open.return_value = Response(b'{"type":"message","stop_reason":"end_turn","usage":{"input_tokens":5,"output_tokens":2,"cache_read_input_tokens":3}}')
        body = json.dumps({"messages": [{"role": "user", "content": "offline"}]}).encode()
        req = request.Request(profile["base_url"] + "/messages", data=body,
                              headers={"x-api-key": profile["api_key"], "Content-Type": "application/json"})
        with request.urlopen(req, timeout=3) as response:
            response.read()
        self.assertEqual(10, self.budget.ledger.summary()["total_tokens"])
        with self.assertRaises(error.HTTPError):
            self.call(profile, {"messages": [{"role": "user", "content": "offline"}]})
        self.assertEqual(1, broker.upstream_opener.open.call_count)


if __name__ == "__main__":
    unittest.main()
