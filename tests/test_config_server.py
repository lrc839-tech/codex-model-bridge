"""Configuration security and actual loopback HTTP tests; only disposable keys."""
from __future__ import annotations

import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/codex-model-bridge'))
from runtime import config, server
from runtime.providers import ProviderError


FAKE_KEY = 'unit-test-disposable-config-key'
PROFILE = {'base_url': 'https://example.test/v1', 'api_key': FAKE_KEY, 'model': 'manual-model'}


class DisposableConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'state'
        self.profile = self.state / 'profile.json'
        self.patches = [patch.object(config, 'STATE', self.state), patch.object(config, 'PROFILE', self.profile)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()


class ConfigTests(DisposableConfig):
    def test_save_is_atomic_private_and_public_result_hides_key(self):
        public = config.save_profile(PROFILE)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.profile.stat().st_mode), 0o600)
        self.assertEqual(config.load_profile(), PROFILE)
        self.assertNotIn(FAKE_KEY, json.dumps(public))
        self.assertNotIn('api_key', public)
        self.assertTrue(public['key_configured'])
        self.assertEqual(public['default'], 'pure_codex')
        self.assertEqual(list(self.state.glob('.profile-*')), [])

    def test_blank_key_retained_only_for_exact_normalized_saved_address(self):
        config.save_profile(PROFILE)
        public = config.save_profile({**PROFILE, 'api_key': '', 'model': 'new-model',
                                      'base_url': 'https://example.test/v1/chat/completions'})
        self.assertEqual(public['model'], 'new-model')
        self.assertEqual(config.load_profile()['api_key'], FAKE_KEY)
        with self.assertRaisesRegex(ValueError, '重新填写'):
            config.save_profile({**PROFILE, 'api_key': '', 'base_url': 'https://other.test/v1'})
        self.assertEqual(config.load_profile()['base_url'], PROFILE['base_url'])

    def test_first_configuration_requires_key(self):
        with self.assertRaises(ValueError):
            config.save_profile({**PROFILE, 'api_key': ''})
        self.assertFalse(self.profile.exists())

    def test_group_or_world_readable_profile_is_rejected(self):
        config.save_profile(PROFILE)
        self.profile.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '600'):
            config.load_profile()

    def test_profile_and_state_symlinks_rejected_before_read(self):
        target = Path(self.temp.name) / 'unrelated.json'
        target.write_text(json.dumps(PROFILE))
        target.chmod(0o600)
        self.state.mkdir()
        self.profile.symlink_to(target)
        with self.assertRaises(ValueError):
            config.load_profile()
        self.profile.unlink()
        self.state.rmdir()
        self.state.symlink_to(Path(self.temp.name) / 'missing-dir')
        with self.assertRaises(ValueError):
            config.load_profile(required=False)
        self.assertEqual(json.loads(target.read_text()), PROFILE)

    def test_malformed_or_oversized_profile_rejected(self):
        self.state.mkdir(mode=0o700)
        for value in ['not-json', '[]', ' ' * 65537]:
            self.profile.write_text(value)
            self.profile.chmod(0o600)
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                config.load_profile()

    def test_not_owned_state_is_rejected(self):
        self.state.mkdir(mode=0o700)
        with patch.object(config.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, '属于当前用户'):
                config.ensure_state()

    def test_non_regular_profile_is_rejected_without_blocking(self):
        self.state.mkdir(mode=0o700)
        os.mkfifo(self.profile, mode=0o600)
        with self.assertRaisesRegex(ValueError, '600'):
            config.load_profile()

    def test_empty_public_config_does_not_require_credentials(self):
        self.assertEqual(config.public_profile(), {
            'base_url': '', 'model': '', 'key_configured': False, 'default': 'pure_codex'})


class ConfigHTTPTests(DisposableConfig):
    def setUp(self):
        super().setUp()
        self.server = server.ConfigServer()
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.cookie = ''

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def call(self, method='GET', path='/api/config', data=None, headers=None, raw=False):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        outgoing = dict(headers or {})
        if self.cookie:
            outgoing.setdefault('Cookie', self.cookie)
        if data is not None:
            data = data if isinstance(data, bytes) else json.dumps(data).encode()
            outgoing.setdefault('Content-Type', 'application/json')
        connection.request(method, path, body=data, headers=outgoing)
        response = connection.getresponse()
        body = response.read().decode()
        result = response.status, dict(response.getheaders()), body if raw else json.loads(body)
        connection.close()
        return result

    def open_page(self):
        status, headers, html = self.call(path='/', raw=True)
        self.assertEqual(status, 200)
        self.assertIn(self.server.token, html)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.cookie = cookie.split(';')[0]
        return headers

    def post_headers(self):
        return {'Origin': self.server.origin, 'X-CSRF-Token': self.server.token}

    def test_loopback_binding_cookie_and_security_headers(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        headers = self.open_page()
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_get_config_requires_session_and_never_returns_saved_key(self):
        config.save_profile(PROFILE)
        self.assertEqual(self.call()[0], 403)
        self.open_page()
        status, _, body = self.call()
        self.assertEqual(status, 200)
        self.assertTrue(body['key_configured'])
        self.assertNotIn(FAKE_KEY, json.dumps(body))
        self.assertNotIn('api_key', body)

    def test_host_header_blocks_rebinding_and_aliases(self):
        for host in ['evil.test', 'localhost:' + str(self.server.server_port), '127.0.0.1']:
            with self.subTest(host=host):
                self.assertEqual(self.call(path='/', headers={'Host': host})[0], 403)

    def test_post_requires_csrf_cookie_and_matching_origin(self):
        self.open_page()
        invalid = [{}, {'X-CSRF-Token': 'wrong', 'Origin': self.server.origin},
                   {'X-CSRF-Token': self.server.token, 'Origin': 'https://evil.test'},
                   {'X-CSRF-Token': self.server.token, 'Origin': 'null'},
                   {**self.post_headers(), 'Cookie': 'bridge_session=wrong'}]
        for headers in invalid:
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.call('POST', data=PROFILE, headers=headers)[0], 403)
        self.assertFalse(self.profile.exists())

    def test_non_ascii_cookie_or_csrf_fails_safely(self):
        self.open_page()
        for headers in [{**self.post_headers(), 'Cookie': 'bridge_session=é'},
                        {**self.post_headers(), 'X-CSRF-Token': 'é'}]:
            with self.subTest(headers=list(headers)):
                self.assertEqual(self.call('POST', data=PROFILE, headers=headers)[0], 403)

    def test_save_and_switch_address_blank_key_without_leaking(self):
        self.open_page()
        status, _, body = self.call('POST', data=PROFILE, headers=self.post_headers())
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY, json.dumps(body))
        status, _, body = self.call('POST', data={**PROFILE, 'api_key': '',
            'base_url': 'https://other.test/v1'}, headers=self.post_headers())
        self.assertEqual(status, 400)
        self.assertNotIn(FAKE_KEY, json.dumps(body))
        self.assertEqual(config.load_profile(), PROFILE)

    def test_invalid_json_and_request_size_are_controlled_errors(self):
        self.open_page()
        for data, extra in [(b'{bad', {}), (b'{}', {'Content-Type': 'text/plain'}),
                            (b'x' * 16385, {}), (b'[]', {}),
                            (json.dumps({**PROFILE, 'api_key': True}).encode(), {})]:
            with self.subTest(data_size=len(data)):
                self.assertEqual(self.call('POST', data=data,
                    headers={**self.post_headers(), **extra})[0], 400)

    def test_duplicate_host_is_rejected(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.putrequest('GET', '/', skip_host=True)
        connection.putheader('Host', self.server.origin.split('//')[1])
        connection.putheader('Host', 'evil.test')
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

    def test_api_error_does_not_echo_submitted_key(self):
        self.open_page()
        with patch.object(server, 'test_connection', side_effect=ValueError('bad ' + FAKE_KEY)):
            status, _, body = self.call('POST', '/api/test', data=PROFILE, headers=self.post_headers())
        self.assertEqual(status, 400)
        self.assertNotIn(FAKE_KEY, json.dumps(body))

    def test_actual_provider_redirect_returns_error_without_following(self):
        hits = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                hits.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(302)
                self.send_header('Location', '/leak-target')
                self.end_headers()

        vendor = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        worker = threading.Thread(target=vendor.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        worker.start()
        try:
            self.open_page()
            profile = {**PROFILE, 'base_url': 'http://127.0.0.1:' + str(vendor.server_port) + '/v1'}
            status, _, body = self.call('POST', '/api/test', data=profile, headers=self.post_headers())
            self.assertEqual(status, 400)
            self.assertIn('HTTP 302', body['error'])
            self.assertNotIn(FAKE_KEY, json.dumps(body))
            self.assertEqual(hits, ['/v1/chat/completions'])
        finally:
            vendor.shutdown()
            vendor.server_close()
            worker.join(timeout=2)

    def test_actual_provider_tool_probe_round_trip_over_http(self):
        requests = []

        class Vendor(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                requests.append(body)
                if len(requests) == 1:
                    token = body['messages'][-1]['content'].split('token=')[1]
                    result = {'choices': [{'finish_reason': 'tool_calls', 'message': {
                        'role': 'assistant', 'content': None, 'reasoning_content': 'vendor-state',
                        'encrypted_content': 'opaque-state', 'tool_calls': [{
                            'id': 'real-http-call', 'type': 'function', 'function': {
                                'name': 'bridge_connection_probe',
                                'arguments': json.dumps({'token': token})}}]}}]}
                else:
                    result = {'choices': [{'finish_reason': 'stop', 'message': {
                        'role': 'assistant', 'content': 'probe completed'}}]}
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        vendor = ThreadingHTTPServer(('127.0.0.1', 0), Vendor)
        worker = threading.Thread(target=vendor.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        worker.start()
        try:
            self.open_page()
            profile = {**PROFILE, 'base_url': 'http://127.0.0.1:' + str(vendor.server_port) + '/v1'}
            status, _, body = self.call('POST', '/api/test', data=profile, headers=self.post_headers())
            self.assertEqual(status, 200)
            self.assertTrue(body['ok'])
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]['messages'][:2], requests[0]['messages'])
            self.assertEqual(requests[1]['messages'][2]['reasoning_content'], 'vendor-state')
            self.assertEqual(requests[1]['messages'][2]['encrypted_content'], 'opaque-state')
            self.assertEqual(requests[1]['messages'][3]['tool_call_id'], 'real-http-call')
        finally:
            vendor.shutdown()
            vendor.server_close()
            worker.join(timeout=2)


class ProbeTests(unittest.TestCase):
    def probe(self, first_reason='tool_calls', final_reason='stop', tool=True, final_text='probe received', malformed=None):
        records = []

        class FakeClient:
            def __init__(self, _):
                pass

            def chat(self, messages, **kwargs):
                records.append(copy.deepcopy(messages))
                if len(records) == 1:
                    token = messages[-1]['content'].split('token=')[1]
                    calls = [{'id': 'test-call', 'type': 'function', 'function': {
                        'name': 'bridge_connection_probe', 'arguments': json.dumps({'token': token})}}]
                    if malformed is not None:
                        calls = malformed
                    message = {'role': 'assistant', 'content': None, 'reasoning_content': 'must-replay',
                               'encrypted_content': 'opaque-must-replay'}
                    if tool:
                        message['tool_calls'] = calls
                    return {'choices': [{'finish_reason': first_reason, 'message': message}]}
                return {'choices': [{'finish_reason': final_reason, 'message': {
                    'role': 'assistant', 'content': final_text}}]}

        with patch.object(server, 'ProviderClient', FakeClient):
            result = server.test_connection(PROFILE)
        return result, records

    def test_full_initial_context_and_assistant_protocol_fields_round_trip(self):
        result, records = self.probe()
        self.assertTrue(result['ok'])
        self.assertEqual(records[1][:2], records[0])
        self.assertEqual(records[1][2]['reasoning_content'], 'must-replay')
        self.assertEqual(records[1][2]['encrypted_content'], 'opaque-must-replay')
        self.assertEqual(records[1][3]['tool_call_id'], 'test-call')

    def test_chat_only_is_not_tool_compatibility(self):
        result, records = self.probe(tool=False)
        self.assertTrue(result['chat_ok'])
        self.assertFalse(result['ok'])
        self.assertFalse(result['tools_ok'])
        self.assertEqual(len(records), 1)

    def test_truncated_first_or_last_response_does_not_pass(self):
        for kwargs in [{'first_reason': 'length'}, {'first_reason': None},
                       {'final_reason': 'length'}, {'final_reason': 'content_filter'},
                       {'final_text': ''}, {'final_text': '   '}, {'final_text': True}]:
            with self.subTest(kwargs=kwargs):
                self.assertFalse(self.probe(**kwargs)[0]['ok'])

    def test_malformed_tool_calls_do_not_crash_or_pass(self):
        for malformed in ['not-list', [None], [123], [{'function': None}],
                          [{'id': 'x', 'type': 'function', 'function': {
                              'name': 'bridge_connection_probe', 'arguments': 'not-json'}}]]:
            with self.subTest(value=malformed):
                self.assertFalse(self.probe(malformed=malformed)[0]['ok'])


if __name__ == '__main__':
    unittest.main()
