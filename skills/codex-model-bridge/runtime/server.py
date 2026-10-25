"""Loopback-only configuration UI with CSRF and Host validation."""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time
import webbrowser
from . import config
from .providers import ProviderClient, ProviderError

UI = Path(__file__).resolve().parent.parent / 'ui/index.html'
PROBE_TOOLS = [{'type':'function', 'function':{'name':'bridge_connection_probe',
    'description':'连接测试专用，无任何副作用。收到 token 后原样调用一次本工具。',
    'parameters':{'type':'object','properties':{'token':{'type':'string'}},'required':['token'],'additionalProperties':False}}}]


def test_connection(profile):
    client = ProviderClient(profile)
    token = secrets.token_hex(5)
    messages = [{'role':'system','content':'你正在做工具兼容性测试。请调用 bridge_connection_probe，参数 token 使用用户提供的字符串。不要只输出文字。'},
                {'role':'user','content':'调用工具，token='+token}]
    response = client.chat(messages, tools=PROBE_TOOLS, timeout=55)
    choice = response['choices'][0]
    message = choice['message']
    calls = message.get('tool_calls') or []
    if not isinstance(calls, list):
        calls = []
    valid = []
    for call in calls:
        try:
            fn = call['function']
            if (call.get('type') == 'function' and fn['name'] == 'bridge_connection_probe' and
                    json.loads(fn['arguments']) == {'token':token} and
                    isinstance(call.get('id'), str) and call['id']):
                valid.append(call)
        except (AttributeError, TypeError, KeyError, ValueError):
            pass
    if len(valid) != 1 or len(calls) != 1 or choice.get('finish_reason') not in {'tool_calls', 'stop'}:
        return {'ok':False,'chat_ok':True,'tools_ok':False,
                'detail':'API 已连通，但模型未正确调用测试工具。请换用支持工具调用的文本模型后再测。'}
    # Preserve vendor protocol fields in assistant message for the follow-up.
    follow = client.chat(messages + [message, {'role':'tool','tool_call_id':valid[0]['id'],'content':'{"ok":true}'}],
                         tools=PROBE_TOOLS, timeout=55)
    final = follow['choices'][0]
    final_text = final['message'].get('content')
    complete = (final.get('finish_reason') == 'stop' and isinstance(final_text, str) and
                bool(final_text.strip()) and not final['message'].get('tool_calls'))
    return {'ok':complete,'chat_ok':True,'tools_ok':complete,
            'detail':'连接与工具调用往返测试通过，可以保存并按需使用。测试使用供应商 API 额度。' if complete else '工具已被调用，但后续对话未完整结束；请更换模型或核对供应商兼容接口。'}


class ConfigServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def __init__(self, port=0):
        super().__init__(('127.0.0.1', port), ConfigHandler)
        self.token = secrets.token_urlsafe(32)
        self.origin = 'http://127.0.0.1:' + str(self.server_port)
        self.session = secrets.token_urlsafe(32)
        self.last_activity = time.monotonic()


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = 'ModelBridge'
    def log_message(self, *args):
        pass

    def send_body(self, code, value, mime='application/json; charset=utf-8', cookie=False):
        data = value.encode() if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        # Self-contained local UI: inline code only from the immutable shipped file.
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', 'bridge_session='+self.server.session+'; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        self.server.last_activity = time.monotonic()
        if (len(self.headers.get_all('Host', [])) != 1 or
                self.headers.get('Host') != self.server.origin.split('//')[1]):
            self.send_body(403, {'error':'仅允许本机配置页面访问。'})
            return False
        return True

    def authenticated(self, post=False):
        cookie = self.headers.get('Cookie', '')
        cookies = dict(part.strip().split('=',1) for part in cookie.split(';') if '=' in part)
        def same_token(value, expected):
            return isinstance(value, str) and value.isascii() and secrets.compare_digest(value, expected)
        if (len(self.headers.get_all('Cookie', [])) != 1 or
                not same_token(cookies.get('bridge_session',''), self.server.session)):
            self.send_body(403, {'error':'请从配置入口重新打开本机页面。'})
            return False
        if post and (len(self.headers.get_all('Origin', [])) > 1 or
                     self.headers.get('Origin') not in {None,self.server.origin} or
                     len(self.headers.get_all('X-CSRF-Token', [])) != 1 or
                     not same_token(self.headers.get('X-CSRF-Token',''), self.server.token)):
            self.send_body(403, {'error':'请求来源验证失败。请重新打开配置页面。'})
            return False
        return True

    def do_GET(self):
        if not self.valid_host():
            return
        if self.path == '/':
            self.send_body(200, UI.read_text(encoding='utf-8').replace('__CSRF_TOKEN__',self.server.token),
                           'text/html; charset=utf-8',cookie=True)
            return
        if not self.authenticated():
            return
        try:
            if self.path == '/api/config':
                self.send_body(200, config.public_profile())
            elif self.path == '/api/models':
                self.send_body(200, {'models':ProviderClient(config.load_profile()).fetch_models(), 'manual_allowed':True})
            else:
                self.send_body(404, {'error':'页面不存在。'})
        except (ValueError, OSError, ProviderError) as error:
            self.send_body(400, {'error':str(error)})

    def do_POST(self):
        if not self.valid_host() or not self.authenticated(post=True):
            return
        try:
            size = int(self.headers.get('Content-Length','0'))
            if not 0 < size <= 16384 or self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                raise ValueError('请求格式不正确。')
            data = json.loads(self.rfile.read(size))
            if self.path == '/api/config':
                self.send_body(200, config.save_profile(data))
            elif self.path == '/api/test':
                self.send_body(200, test_connection(config.resolve_profile(data)))
            else:
                self.send_body(404, {'error':'接口不存在。'})
        except (ValueError, OSError, ProviderError) as error:
            # Never reflect upstream bodies or user-supplied secret values.
            message = str(error)
            for key in [data.get('api_key','') if isinstance(locals().get('data'),dict) else '']:
                if isinstance(key, str) and key:
                    message = message.replace(key,'[REDACTED]')
            self.send_body(400, {'error':message})


def serve(port=0, open_browser=True, idle_seconds=1800):
    server = ConfigServer(port)
    print(json.dumps({'configuration_url':server.origin, 'expires_after_idle_minutes':idle_seconds//60},ensure_ascii=False),flush=True)
    if open_browser:
        webbrowser.open(server.origin)
    def expire():
        while time.monotonic()-server.last_activity < idle_seconds:
            time.sleep(5)
        server.shutdown()
    threading.Thread(target=expire,daemon=True).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
