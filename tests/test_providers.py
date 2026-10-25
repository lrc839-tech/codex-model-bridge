"""Offline provider contract tests. No real keys, vendor traffic, or paid calls."""

from __future__ import annotations

import io
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError

from runtime import providers
from runtime.providers import (ProviderClient, ProviderError, execution_parameters,
                               normalize_base_url, provider_name)


FAKE_KEY = "unit-test-key-not-a-real-secret"
PROFILE = {"base_url": "https://example.test/v1", "api_key": FAKE_KEY, "model": "manual-model"}


class FakeResponse:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()

    def read(self, length):
        return self.data[:length]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeOpener:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def open(self, req, timeout):
        self.calls.append((req, timeout))
        if self.exc:
            raise self.exc
        return FakeResponse(self.result)


def client_with(result=None, exc=None, profile=None):
    client = ProviderClient(profile or PROFILE)
    client._opener = FakeOpener(result, exc)
    return client


def completion(message=None):
    return {"choices": [{"message": message or {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}


class BaseURLTests(unittest.TestCase):
    def test_base_full_endpoint_and_root(self):
        cases = {
            "https://api.deepseek.com": "https://api.deepseek.com/v1",
            "api.deepseek.com/v1/": "https://api.deepseek.com/v1",
            "https://example.test/v1/chat/completions/": "https://example.test/v1",
            "https://example.test/v1/models": "https://example.test/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/":
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions":
                "https://ark.cn-beijing.volces.com/api/v3",
            "https://open.bigmodel.cn/api/paas/v4/": "https://open.bigmodel.cn/api/paas/v4",
            "http://[::1]:9000/v1": "http://[::1]:9000/v1",
            "http://localhost:9000/v1": "http://localhost:9000/v1",
            "http://127.0.0.1:9000/v1": "http://127.0.0.1:9000/v1",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_base_url(value), expected)

    def test_unsafe_and_wrong_protocol_urls_rejected(self):
        for value in ["", None, "https://", "ftp://example.test/v1", "http://example.test/v1",
                      "http://192.168.1.1/v1", "https://user:secret@example.test/v1",
                      "https://example.test/v1?api_key=secret", "https://example.test/#secret",
                      "https://example.test:invalid/v1", "https://example.test:0/v1",
                      "https://example.test/with\ncontrol", "https://example.test/with space",
                      "https://example.test\\@evil.test/v1", "https://example.test/v1/responses",
                      "https://example.test/v1/messages"]:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                normalize_base_url(value)

    def test_http_similar_hostname_is_not_loopback(self):
        for host in ["localhost.evil.test", "127.0.0.1.evil.test", "local.test", "[::2]"]:
            with self.subTest(host=host), self.assertRaises(ProviderError):
                normalize_base_url("http://" + host + "/v1")


class VendorParameterTests(unittest.TestCase):
    def test_recognizes_only_official_hosts(self):
        cases = {
            "https://api.deepseek.com/v1": "deepseek",
            "https://dashscope.aliyuncs.com/compatible-mode/v1": "qwen",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1": "qwen",
            "https://ark.cn-beijing.volces.com/api/v3": "doubao",
            "https://api.moonshot.cn/v1": "kimi",
            "https://api.moonshot.ai/v1": "kimi",
            "https://open.bigmodel.cn/api/paas/v4": "zhipu",
            "https://api.z.ai/api/paas/v4": "zhipu",
            "https://api.deepseek.com.evil.test/v1": "custom",
            "https://example.test/deepseek": "custom",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(provider_name(url), expected)

    def test_known_execution_models_get_documented_body_fields(self):
        cases = [
            ("https://dashscope.aliyuncs.com/v1", "qwen3.5-plus", {"enable_thinking": False}),
            ("https://api.deepseek.com/v1", "deepseek-chat", {"thinking": {"type": "disabled"}}),
            ("https://ark.cn-beijing.volces.com/api/v3", "ep-user-endpoint", {"thinking": {"type": "disabled"}}),
            ("https://api.moonshot.cn/v1", "kimi-k2.6", {"thinking": {"type": "disabled"}}),
            ("https://api.moonshot.cn/v1", "kimi-k3.1-pro", {"reasoning_effort": "low"}),
            ("https://open.bigmodel.cn/api/paas/v4", "glm-4.7", {"thinking": {"type": "disabled"}}),
            ("https://open.bigmodel.cn/api/paas/v4", "glm-5.3", {"reasoning_effort": "low"}),
        ]
        for url, model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(execution_parameters(url, model), expected)

    def test_unknown_custom_and_thinking_only_models_do_not_get_invalid_switches(self):
        cases = [
            ("https://example.test/v1", "qwen3-new"),
            ("https://api.moonshot.cn/v1", "kimi-k2.7-code"),
            ("https://dashscope.aliyuncs.com/v1", "qwen3-235b-a22b-thinking-2507"),
            ("https://dashscope.aliyuncs.com/v1", "qwen3-coder-plus"),
            ("https://open.bigmodel.cn/v1", "glm-future-model"),
        ]
        for url, model in cases:
            with self.subTest(model=model):
                self.assertEqual(execution_parameters(url, model), {})


class ProfileTests(unittest.TestCase):
    def test_profile_and_fields_validated_without_key_leakage(self):
        profiles = [None, {}, {**PROFILE, "api_key": ""}, {**PROFILE, "model": ""},
                    {**PROFILE, "api_key": "bad\r\nkey"}, {**PROFILE, "model": "bad\nmodel"}]
        for profile in profiles:
            with self.subTest(profile_type=type(profile)), self.assertRaises(ProviderError) as cm:
                ProviderClient(profile)
            self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_client_repr_contains_no_key(self):
        self.assertNotIn(FAKE_KEY, repr(ProviderClient(PROFILE)))

    def test_manual_model_has_no_hardcoded_allowlist(self):
        client = ProviderClient({**PROFILE, "model": "my-model-or-deployment-2030"})
        self.assertEqual(client.model, "my-model-or-deployment-2030")


class RequestTests(unittest.TestCase):
    def test_chat_uses_correct_route_headers_and_json_without_extra_body_wrapper(self):
        client = client_with(completion(), profile={
            **PROFILE, "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "qwen3.5-plus"})
        client.chat([{"role": "user", "content": "返回 JSON"}], json_output=True, timeout=33)
        req, timeout = client._opener.calls[0]
        body = json.loads(req.data)
        self.assertEqual(req.full_url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + FAKE_KEY)
        self.assertEqual(timeout, 33)
        self.assertFalse(body["stream"])
        self.assertFalse(body["enable_thinking"])
        self.assertNotIn("extra_body", body)
        self.assertNotIn("temperature", body)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_tool_calls_reasoning_and_encrypted_content_round_trip_unchanged(self):
        message = {"role": "assistant", "content": None, "reasoning_content": "protocol-state",
                   "encrypted_content": "opaque-state", "tool_calls": [{"id": "call-1",
                   "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]}
        response = completion(message)
        client = client_with(response)
        tools = [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]
        actual = client.chat([{"role": "user", "content": "read"}], tools=tools)
        self.assertEqual(actual, response)
        messages = [{"role": "user", "content": "read"}, actual["choices"][0]["message"],
                    {"role": "tool", "tool_call_id": "call-1", "content": "file contents"}]
        client.chat(messages, tools=tools)
        body = json.loads(client._opener.calls[-1][0].data)
        self.assertEqual(body["messages"][1], message)
        self.assertEqual(body["tools"], tools)
        self.assertEqual(body["tool_choice"], "auto")

    def test_fetch_models_deduplicates_and_does_not_make_chat_call(self):
        client = client_with({"data": [{"id": "z-model"}, {"id": "a-model"},
                                             {"id": "z-model"}, {"wrong": "x"}]})
        self.assertEqual(client.fetch_models(), ["a-model", "z-model"])
        req, _ = client._opener.calls[0]
        self.assertEqual(req.full_url, "https://example.test/v1/models")
        self.assertEqual(req.method, "GET")
        self.assertIsNone(req.data)

    def test_models_discovery_unsupported_is_optional(self):
        for status in [404, 405, 501]:
            client = client_with(exc=HTTPError("https://example.test", status, "no models", {}, io.BytesIO()))
            with self.subTest(status=status):
                self.assertEqual(client.fetch_models(), [])

    def test_models_auth_failure_is_not_hidden_as_unsupported(self):
        client = client_with(exc=HTTPError("https://example.test", 401, "no", {}, io.BytesIO()))
        with self.assertRaises(ProviderError) as cm:
            client.fetch_models()
        self.assertEqual(cm.exception.status_code, 401)

    def test_error_body_key_and_url_never_appear_in_public_error(self):
        for status in [301, 400, 401, 402, 403, 404, 429, 500]:
            client = client_with(exc=HTTPError("https://example.test/?secret=" + FAKE_KEY, status,
                FAKE_KEY, {}, io.BytesIO(("upstream echoed " + FAKE_KEY).encode())))
            with self.subTest(status=status), self.assertRaises(ProviderError) as cm:
                client.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(cm.exception.status_code, status)
            self.assertNotIn(FAKE_KEY, str(cm.exception))
            self.assertNotIn("example.test", str(cm.exception))
            self.assertEqual(len(client._opener.calls), 1)
            self.assertNotIn(FAKE_KEY, json.dumps(cm.exception.as_dict()))

    def test_network_exception_details_are_sanitized(self):
        for exc, code in [(URLError(FAKE_KEY), "network_error"),
                          (socket.timeout(FAKE_KEY), "timeout"),
                          (URLError(socket.timeout(FAKE_KEY)), "timeout")]:
            client = client_with(exc=exc)
            with self.subTest(code=code), self.assertRaises(ProviderError) as cm:
                client.chat([{"role": "user", "content": "hello"}])
            self.assertEqual(cm.exception.code, code)
            self.assertNotIn(FAKE_KEY, str(cm.exception))

    def test_malformed_chat_and_models_responses_fail(self):
        for result in [b"<html>login</html>", [], {"error": {"message": FAKE_KEY}},
                       {"choices": []}, {"choices": [{"message": "not-object"}]}]:
            with self.subTest(response_type=type(result)), self.assertRaises(ProviderError) as cm:
                client_with(result).chat([{"role": "user", "content": "hello"}])
            self.assertNotIn(FAKE_KEY, str(cm.exception))
        with self.assertRaises(ProviderError):
            client_with({"models": []}).fetch_models()

    def test_invalid_timeout_messages_and_unserializable_inputs_fail_before_network(self):
        client = client_with(completion())
        for timeout in [0, -1, True, float("nan"), float("inf"), 3601]:
            with self.subTest(timeout=timeout), self.assertRaises(ProviderError):
                client.chat([{"role": "user", "content": "hi"}], timeout=timeout)
        for messages in [[], None, ["wrong"]]:
            with self.assertRaises(ProviderError):
                client.chat(messages)
        with self.assertRaises(ProviderError):
            client.chat([{"role": "user", "content": object()}])
        self.assertEqual(client._opener.calls, [])

    def test_response_size_is_bounded(self):
        original = providers.MAX_RESPONSE_BYTES
        try:
            providers.MAX_RESPONSE_BYTES = 40
            with self.assertRaises(ProviderError) as cm:
                client_with(b"x" * 41).chat([{"role": "user", "content": "hi"}])
            self.assertEqual(cm.exception.code, "response_too_large")
        finally:
            providers.MAX_RESPONSE_BYTES = original


class LocalHTTPTests(unittest.TestCase):
    def test_real_http_tool_call_and_redirect_are_handled_without_external_network(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                received.append((self.path, self.headers.get("Authorization"), body))
                if body["model"] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/should-not-receive-key")
                    self.end_headers()
                else:
                    raw = json.dumps(completion()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = {**PROFILE, "base_url": "http://127.0.0.1:" + str(server.server_port) + "/v1"}
            client = ProviderClient(profile)
            self.assertEqual(client.chat([{"role": "user", "content": "hi"}])["choices"][0]["message"]["content"], "ok")
            with self.assertRaises(ProviderError) as cm:
                ProviderClient({**profile, "model": "redirect"}).chat([{"role": "user", "content": "hi"}])
            self.assertEqual(cm.exception.status_code, 302)
            self.assertEqual(len(received), 2)
            self.assertTrue(all(item[0] == "/v1/chat/completions" for item in received))
            self.assertTrue(all(item[1] == "Bearer " + FAKE_KEY for item in received))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
