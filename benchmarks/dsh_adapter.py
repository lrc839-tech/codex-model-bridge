#!/usr/bin/env python3
"""Official DSH SDK worker with a separate home and two macOS Seatbelt fences.

No real provider key belongs in this process. The only network grant is the
coordinator's authenticated loopback model broker. Model-created shell tools
receive a second profile with *no* network grant. No HOME/CODEX_HOME overrides.
"""
from __future__ import annotations

import argparse
import dataclasses
import http.server
import json
import os
from pathlib import Path
import shutil
import socket
import shlex
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse


HERE = Path(__file__).resolve().parent
VENV = HERE / ".runtime" / "dsh-venv"
SDK_PYTHON = VENV / "bin" / "python"
VERSION = "0.2.1rc2"
_TOOL_SERVERS: dict[str, object] = {}


def _quote(value: str | Path) -> str:
    # SBPL accepts literal UTF-8 paths, not JSON's \uXXXX encoding.
    return json.dumps(str(value), ensure_ascii=False)


def _port(broker_url: str) -> int:
    parsed = urllib.parse.urlsplit(broker_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise ValueError("DSH broker must be an explicit http://127.0.0.1:PORT URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Credentials and query parameters do not belong in broker URL")
    return parsed.port


def _runtime_roots() -> list[Path]:
    # Reading this venv is safe: it contains only pinned runtime dependencies,
    # not the benchmark fixtures/evaluators alongside its parent directory.
    interpreter = SDK_PYTHON.resolve(strict=True)
    roots = [VENV.resolve(), interpreter.parent.parent]
    if "/Frameworks/Python.framework/" in str(interpreter):
        roots.append(Path(str(interpreter).split("/Frameworks/Python.framework/")[0]))
    return sorted(set(roots))


def seatbelt_profile(workspace: Path, state_dir: Path, *, broker_port: int | None,
                     tool: bool = False, tool_broker_port: int | None = None) -> str:
    workspace = workspace.resolve(strict=True)
    state_dir = state_dir.resolve(strict=True)
    # Never grant /System wholesale: /System/Volumes/Data also contains homes.
    read_roots = [Path("/System/Library"), Path("/System/Applications"),
                  Path("/System/Volumes/Preboot/Cryptexes/OS"),
                  Path("/usr"), Path("/bin"), Path("/sbin"),
                  Path("/Library/Apple"), Path("/private/var/db/dyld"), *_runtime_roots()]
    forms = ["(version 1)", "(deny default)", "(allow process-exec)",
             "(allow process-fork)", "(allow signal (target self))", "(allow signal (target children))",
             "(allow sysctl-read)",
             '(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.PowerManagement.control"))',
             "(allow file-read-metadata)", "(allow dynamic-code-generation)"]
    # libSystem's sandbox initialization requires opening the root directory.
    # A literal grant does not grant reads of any descendant user directory.
    forms.append('(allow file-read* (literal "/"))')
    forms.append("(allow file-read* " + " ".join(f"(subpath {_quote(p)})" for p in read_roots) + ")")
    forms.append("(allow file-read* " + " ".join(f"(literal {_quote(p)})" for p in [
        "/dev/null", "/dev/random", "/dev/urandom", "/dev/tty", "/dev/fd", "/private/etc/localtime"
    ]) + " (subpath \"/dev/fd\"))")
    forms.append(f"(allow file-read* (literal {_quote(Path(__file__).resolve())}))")
    forms.append(f"(allow file-read* file-write* (subpath {_quote(workspace)}))")
    forms.append("(allow file-write* (literal \"/dev/null\") (literal \"/dev/tty\"))")
    forms.append("(allow file-ioctl (subpath \"/dev\"))")
    if tool:
        # Only the temporary tool scratch is readable/writable. The tool
        # cannot edit SDK profile overrides, sessions, or its own launcher.
        scratch = state_dir / "tool-tmp"
        forms.append(f"(allow file-read* file-write* (subpath {_quote(scratch)}))")
    else:
        forms.append(f"(allow file-read* file-write* (subpath {_quote(state_dir)}))")
    if broker_port is not None:
        # Seatbelt accepts only '*' or 'localhost' as the host token here.
        forms.append(f'(allow network-outbound (remote ip "localhost:{broker_port}"))')
    if tool_broker_port is not None:
        forms.append(f'(allow network-outbound (remote ip "localhost:{tool_broker_port}"))')
    return "\n".join(forms) + "\n"


def child_env(state_dir: Path, broker_url: str, api_key: str) -> dict[str, str]:
    # A positive allowlist avoids inheriting real model/cloud/GitHub credentials,
    # shell startup hooks, agents, custom Python paths or other provider endpoints.
    keep = {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL"}
    env = {key: value for key, value in os.environ.items() if key in keep}
    env.update({
        "PATH": f"{VENV / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "DSH_HOME": str(state_dir / "home"),
        "DSH_TELEMETRY_DISABLED": "1", "DSH_TELEMETRY_MODE": "DISABLED",
        "DSH_PERMISSION_MODE": "workspace-write",
        "DEEPSEEK_BASE_URL": broker_url,
        "DEEPSEEK_API_KEY": api_key,
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(state_dir / "tool-tmp"),
        "TMP": str(state_dir / "tool-tmp"), "TEMP": str(state_dir / "tool-tmp"),
        "XDG_CACHE_HOME": str(state_dir / "cache"),
        "XDG_CONFIG_HOME": str(state_dir / "config"),
        "XDG_DATA_HOME": str(state_dir / "data"),
        "XDG_RUNTIME_DIR": str(state_dir / "tool-tmp"),
        # The official packaged Node binary extracts native addons here;
        # otherwise it writes ~/.cache/pkg even with XDG_CACHE_HOME set.
        "PKG_NATIVE_CACHE_PATH": str(state_dir / "native-cache"),
    })
    return env


def _build_command(workspace: Path, state_dir: Path, broker_url: str,
                  model: str = "deepseek-flash", max_tokens: int = 8192,
                  api_key: str = "local-benchmark-dummy") -> tuple[list[str], dict[str, str]]:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeError("DSH benchmark adapter requires verified macOS sandbox-exec")
    workspace = Path(workspace).resolve(strict=True)
    state_dir = Path(state_dir).resolve()
    if state_dir == workspace or state_dir.is_relative_to(workspace):
        raise ValueError("DSH state/control files must stay outside the candidate workspace")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for part in ["home", "tool-tmp"]:
        (state_dir / part).mkdir(exist_ok=True, mode=0o700)
    port = _port(broker_url)
    outer = state_dir / "outer.sb"
    inner = state_dir / "tool.sb"
    inner.write_text(seatbelt_profile(workspace, state_dir, broker_port=None, tool=True))
    tool_url, tool_token = _start_tool_broker(workspace, state_dir, inner)
    outer.write_text(seatbelt_profile(workspace, state_dir, broker_port=port,
                                     tool_broker_port=_port(tool_url)))
    patches = [
        {"id": "llm-deepseek", "config": {"baseURL": broker_url, "thinking": "disabled",
          "reasoningEffort": "off", "maxTokens": max_tokens}},
        {"id": "sandbox-policy", "config": {"mode": "workspace-write", "workspaceRoot": str(workspace)}},
        {"id": "approval", "config": {"policy": "never"}},
        {"id": "sandbox", "config": {
            "runnerCommand": [str(SDK_PYTHON.resolve()), str(Path(__file__).resolve()),
                              "--tool-runner", "--tool-broker", tool_url, "--tool-token", tool_token],
            "runnerFailureSignatures": ["sandbox-exec:", "dsh-benchmark-tool-runner:"]}},
        {"id": "skill-filesystem", "config": {"includeDefaultRoots": False,
          "customSkillDirs": [], "watch": False}},
        {"id": "agent-instructions", "disabled": True},
    ]
    for plugin in ["tool-web", "web-search-deepseek", "web-deepseek-api", "web-fetch-http", "web",
                   "plugin-manager", "tool-plugin-manager", "config-editor", "settings", "tool-skill"]:
        patches.append({"id": plugin, "disabled": True})
    patch_file = state_dir / "benchmark.patch.yaml"
    patch_file.write_text(json.dumps(patches, indent=2) + "\n")
    command = ["/usr/bin/sandbox-exec", "-f", str(outer), str(SDK_PYTHON), "-B",
               str(Path(__file__).resolve()), "--worker", "--workspace", str(workspace),
               "--state-dir", str(state_dir), "--broker-url", broker_url,
               "--model", model, "--max-tokens", str(max_tokens)]
    return command, child_env(state_dir, broker_url, api_key)


def build_command(workspace: Path, state_dir: Path, broker_url: str,
                  model: str = "deepseek-flash", max_tokens: int = 8192,
                  api_key: str = "local-benchmark-dummy") -> tuple[list[str], dict[str, str]]:
    try:
        return _build_command(workspace, state_dir, broker_url, model, max_tokens, api_key)
    except BaseException:
        cleanup(state_dir)
        raise


def _tool_runner(args: list[str]) -> None:
    # sandbox-local's documented override appends bwrap-compatible flags before
    # '--'. We accept only the known grammar, then apply our strict Seatbelt.
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool-broker", required=True)
    parser.add_argument("--tool-token", required=True)
    namespace, rest = parser.parse_known_args(args)
    try:
        delimiter = rest.index("--")
    except ValueError:
        raise ValueError("dsh-benchmark-tool-runner: missing command separator")
    flags = rest[:delimiter]
    command = rest[delimiter + 1:]
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag in {"--ro-bind", "--bind"}:
            index += 3
        elif flag in {"--dev", "--proc", "--tmpfs"}:
            index += 2
        elif flag in {"--unshare-pid", "--die-with-parent"}:
            index += 1
        else:
            raise ValueError(f"dsh-benchmark-tool-runner: unsupported flag {flag}")
    if not command:
        raise ValueError("dsh-benchmark-tool-runner: empty command")
    _port(namespace.tool_broker)
    # This process inherits the whole-runtime Seatbelt; macOS forbids applying
    # a second Seatbelt here. The trusted unsandboxed coordinator launches the
    # command under an independent, network-denied profile instead.
    import http.client
    parsed = urllib.parse.urlsplit(namespace.tool_broker)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=95)
    body = json.dumps({"argv": command}).encode()
    connection.request("POST", "/execute", body=body,
                       headers={"Content-Type": "application/json", "Authorization": "Bearer " + namespace.tool_token})
    response = connection.getresponse()
    value = json.loads(response.read())
    connection.close()
    if response.status != 200:
        raise RuntimeError("dsh-benchmark-tool-runner: tool broker rejected the request")
    sys.stdout.write(value.get("stdout", "")); sys.stderr.write(value.get("stderr", ""))
    raise SystemExit(value["exit_code"])


def _start_tool_broker(workspace: Path, state_dir: Path, profile: Path) -> tuple[str, str]:
    cleanup(state_dir)
    token = secrets.token_urlsafe(32)
    active: set[subprocess.Popen] = set()
    lock = threading.Lock()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            if self.path != "/execute" or self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(403); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128 * 1024:
                    raise ValueError("Invalid request length")
                payload = json.loads(self.rfile.read(length))
                if set(payload) != {"argv"}:
                    raise ValueError("Only argv is configurable")
                argv = payload["argv"]
                if not isinstance(argv, list) or not 1 <= len(argv) <= 64 or not all(isinstance(x, str) and x and "\x00" not in x for x in argv):
                    raise ValueError("Invalid argv")
                env = {key: value for key, value in os.environ.items() if key in {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL"}}
                env.update({"PATH": f"{VENV / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
                            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                            "TMPDIR": str(state_dir / "tool-tmp")})
                command = ["/usr/bin/sandbox-exec", "-f", str(profile), *argv]
                limit, stop_reason = 2 * 1024 * 1024, None
                with tempfile.TemporaryFile(dir=state_dir / "tool-tmp") as out, tempfile.TemporaryFile(dir=state_dir / "tool-tmp") as err:
                    process = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                        stdout=out, stderr=err, start_new_session=True)
                    with lock: active.add(process)
                    try:
                        deadline = time.monotonic() + 90
                        while process.poll() is None:
                            if time.monotonic() >= deadline: stop_reason = "time_limit"
                            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > limit: stop_reason = "output_limit"
                            if stop_reason:
                                os.killpg(process.pid, signal.SIGKILL); process.wait(); break
                            time.sleep(0.025)
                    finally:
                        # Reap background descendants even if their shell exited.
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        process.wait()
                        with lock: active.discard(process)
                    out.seek(0); err.seek(0)
                    stdout = out.read(limit).decode(errors="replace"); stderr = err.read(limit).decode(errors="replace")
                if stop_reason: stderr += "\ndsh-benchmark-tool-runner: " + stop_reason
                result = {"stdout": stdout, "stderr": stderr, "exit_code": process.returncode}
                data = json.dumps(result).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            except (ValueError, OSError, TypeError, json.JSONDecodeError):
                self.send_error(400, "Restricted tool execution failed")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    _TOOL_SERVERS[str(state_dir.resolve())] = (server, active, lock)
    return f"http://127.0.0.1:{server.server_port}", token


def cleanup(state_dir: Path) -> None:
    value = _TOOL_SERVERS.pop(str(Path(state_dir).resolve()), None)
    if value is None:
        return
    server, active, lock = value
    with lock:
        for process in tuple(active):
            if process.poll() is None:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
    server.shutdown(); server.server_close()


def _worker(args: argparse.Namespace) -> int:
    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
    prompt = sys.stdin.read()
    if not prompt.strip():
        raise ValueError("Worker requires one task prompt on stdin")
    state = Path(args.state_dir)
    route_key = os.environ["DEEPSEEK_API_KEY"]
    def emit(notification) -> None:
        event = (notification.model_dump(mode="json") if hasattr(notification, "model_dump")
                 else dataclasses.asdict(notification) if dataclasses.is_dataclass(notification) else notification)
        print(json.dumps(event, ensure_ascii=False, default=str).replace(route_key, "[LOCAL_ROUTE_TOKEN]"), flush=True)
    config = DeepSeekHarnessConfig(
        provider="deepseek-official", model=args.model, reasoning_effort="off",
        max_tokens=args.max_tokens, cwd=args.workspace, runtime_cwd=args.workspace,
        profile="sdk", dsh_home=str(state / "home"),
        patches=(str(state / "benchmark.patch.yaml"),),
        base_url=args.broker_url, api_key=route_key,
        initialize_timeout_seconds=45, request_timeout_seconds=600, shutdown_timeout_seconds=2,
    )
    started = time.monotonic()
    try:
        with DeepSeekHarness(config) as harness:
            result = harness.run(prompt, on_notification=emit)
        record = {"finish_reason": result.finish_reason,
                  "final_response": result.final_response.replace(route_key, "[LOCAL_ROUTE_TOKEN]"),
                  "session_id": result.session_id, "elapsed_seconds": time.monotonic() - started,
                  "adapter": "official-deepseek-harness-sdk", "version": VERSION}
        (state / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return 0 if result.finish_reason == "completed" else 2
    except Exception as error:
        record = {"finish_reason": "error", "error_type": type(error).__name__,
                  "detail": str(error).replace(route_key, "[LOCAL_ROUTE_TOKEN]"),
                  "elapsed_seconds": time.monotonic() - started}
        (state / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        return 2


def _probe_code(workspace: Path, outside: Path, sibling: Path, broker_port: int,
                forbidden_port: int) -> str:
    # All probed files contain artificial canaries, never real credentials.
    outside_alias = Path("/System/Volumes/Data") / str(outside).lstrip("/")
    targets = {"outside": str(outside), "data_volume_alias": str(outside_alias),
               "sibling": str(sibling), "symlink": str(workspace / "escape-link")}
    return "\n".join([
        "import json, pathlib, socket",
        "result = {}",
        f"targets = {targets!r}",
        "for name, target in targets.items():",
        "    try:", "        pathlib.Path(target).read_bytes()", "        result[name + '_read_denied'] = False",
        "    except PermissionError:", "        result[name + '_read_denied'] = True",
        "    except OSError as exc:", "        result[name + '_read_denied'] = False",
        "    try:", "        pathlib.Path(target).write_text('UNEXPECTED_WRITE')",
        "        result[name + '_write_denied'] = False",
        "    except PermissionError:", "        result[name + '_write_denied'] = True",
        "    except OSError:", "        result[name + '_write_denied'] = False",
        f"result['workspace_read'] = pathlib.Path({str(workspace / 'input.txt')!r}).read_text() == 'PUBLIC_FIXTURE'",
        f"pathlib.Path({str(workspace / 'allowed-write.txt')!r}).write_text('PUBLIC_WRITE')",
        "result['workspace_write'] = True",
        f"for name, host, port in [('broker', '127.0.0.1', {broker_port}), ('other_loopback', '127.0.0.1', {forbidden_port}), ('external', '1.1.1.1', 443)]:",
        "    sock = socket.socket(); sock.settimeout(0.3)",
        "    try:", "        sock.connect((host, port)); result[name + '_connect'] = True",
        "    except PermissionError:", "        result[name + '_connect'] = False; result[name + '_denied_by_kernel'] = True",
        "    except OSError as exc:", "        result[name + '_connect'] = False; result[name + '_error'] = type(exc).__name__",
        "    finally:", "        sock.close()",
        f"pathlib.Path({str(workspace / 'tool-probe.json')!r}).write_text(json.dumps(result))",
        "print(json.dumps(result))",
    ])


def _messages_events(blocks: list[dict], stop_reason: str) -> list[dict]:
    events = [{"type": "message_start", "message": {"id": "offline-prefight",
        "type": "message", "role": "assistant", "model": "deepseek-flash", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 2, "cache_read_input_tokens": 1, "output_tokens": 0}}}]
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events.extend([{"type": "content_block_start", "index": index, "content_block": start},
                       {"type": "content_block_delta", "index": index, "delta": delta},
                       {"type": "content_block_stop", "index": index}])
    events.extend([{"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": 3}}, {"type": "message_stop"}])
    return events


def preflight() -> dict:
    """Real SDK + native read/bash + kernel probes; all model replies are local fakes."""
    if sys.platform != "darwin" or not SDK_PYTHON.is_file():
        return {"passed": False, "error": "Pinned official DSH runtime / macOS unavailable"}
    control = HERE / ".private" / "dsh-preflight"
    control.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="probe-", dir=control) as temporary:
        base = Path(temporary)
        workspace = base / "workspace"; workspace.mkdir()
        state = base / "state"
        outside = base / "control-canary.txt"; outside.write_text("ARTIFICIAL_CONTROL_CANARY")
        sibling = base / "sibling" / "answer-canary.txt"; sibling.parent.mkdir(); sibling.write_text("ARTIFICIAL_SIBLING_CANARY")
        (workspace / "input.txt").write_text("PUBLIC_FIXTURE")
        (workspace / "escape-link").symlink_to(outside)
        requests = []
        command_box = {}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "model": body.get("model"),
                                 "thinking": body.get("thinking"), "max_tokens": body.get("max_tokens"),
                                 "tool_names": [tool.get("name") for tool in body.get("tools", [])]})
                if len(requests) == 1:
                    blocks = [{"type": "tool_use", "id": "offline-bash-probe", "name": "bash",
                        "input": {"command": command_box["command"], "description": "Check benchmark isolation using synthetic canaries"}},
                        {"type": "tool_use", "id": "offline-read-probe", "name": "read",
                         "input": {"file_path": str(outside)}}]
                    events = _messages_events(blocks, "tool_use")
                else:
                    events = _messages_events([{"type": "text", "text": "OFFLINE_PREFLIGHT_OK"}], "end_turn")
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                for event in events:
                    self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        forbidden = socket.socket(); forbidden.bind(("127.0.0.1", 0)); forbidden.listen(2)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        # Exercise the real trusted API relay too; only its upstream opener is
        # replaced. The replacement can contact this one local fake server and
        # checks the intended official URL, so this cannot spend provider quota.
        from benchmarks.api_broker import APIBroker, CampaignBudget, NATIVE_MESSAGES_URL
        from urllib import request as urllib_request
        budget = CampaignBudget(base / "broker-control")
        budget.start_run()
        relay = APIBroker({"base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash",
                           "api_key": "offline-never-real-provider-key"}, budget).start()
        class OfflineUpstream:
            def open(self, request, timeout):
                if request.full_url != NATIVE_MESSAGES_URL:
                    raise ValueError("Unexpected upstream URL during offline probe")
                local = urllib_request.Request(f"http://127.0.0.1:{server.server_port}/v1/messages",
                    data=request.data, headers=dict(request.headers), method="POST")
                return urllib_request.urlopen(local, timeout=timeout)
        relay.upstream_opener = OfflineUpstream()
        route = relay.route("deepseek_harness", base / "broker-run")
        broker_port = _port(route["base_url"])
        try:
            code = _probe_code(workspace, outside, sibling, broker_port, forbidden.getsockname()[1])
            argv, env = build_command(workspace, state, route["base_url"], api_key=route["api_key"])
            # First probe the exact whole-runtime profile directly, independently
            # of what the Harness chooses to expose through its tools.
            outer_command = ["/usr/bin/sandbox-exec", "-f", str(state / "outer.sb"), str(SDK_PYTHON), "-B", "-c", code]
            outer = subprocess.run(outer_command, cwd=workspace, env=env, text=True,
                                   capture_output=True, timeout=20)
            outer_result = json.loads(outer.stdout) if outer.returncode == 0 else {"error": outer.stderr[-1000:]}
            (workspace / "tool-probe.json").unlink(missing_ok=True)
            command_box["command"] = "python3 -B -c " + shlex.quote(code)
            process = subprocess.run(argv, input="Use the requested local tools and then report completion.",
                cwd=workspace, env=env, text=True, capture_output=True, timeout=60)
            record = json.loads((state / "result.json").read_text()) if (state / "result.json").exists() else {}
            tool_result = json.loads((workspace / "tool-probe.json").read_text()) if (workspace / "tool-probe.json").exists() else {}
            events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
            native_denied = False
            tool_results = []
            for event in events:
                payload = event.get("payload", {}).get("event", {})
                if payload.get("type") == "tool/result":
                    tool_results.append(payload.get("data", {}).get("message", {}))
                if payload.get("type") == "tool/result" and "offline-read-probe" in json.dumps(payload):
                    native_denied = any(term in json.dumps(payload).lower() for term in ["eperm", "not permitted", "permission denied"])
            denial_keys = [name + "_" + operation + "_denied" for name in ["outside", "data_volume_alias", "sibling", "symlink"] for operation in ["read", "write"]]
            outer_ok = all(outer_result.get(key) is True for key in denial_keys) and outer_result.get("broker_connect") is True
            outer_ok = outer_ok and all(outer_result.get(key + "_denied_by_kernel") is True for key in ["other_loopback", "external"])
            tool_ok = all(tool_result.get(key) is True for key in denial_keys)
            tool_ok = tool_ok and all(tool_result.get(key + "_denied_by_kernel") is True for key in ["broker", "other_loopback", "external"])
            tool_ok = tool_ok and tool_result.get("workspace_read") is True and tool_result.get("workspace_write") is True
            completed = process.returncode == 0 and record.get("finish_reason") == "completed"
            report = {"passed": bool(outer_ok and tool_ok and native_denied and completed),
                "version": VERSION, "paid_requests": 0, "mock_requests": len(requests),
                "outer_profile": outer_result, "actual_harness_bash_tool": tool_result,
                "actual_harness_native_read_denied": native_denied,
                "completed": completed, "request_metadata": requests,
                "real_api_broker_roundtrip": True,
                "mock_usage": budget.ledger.summary(),
                "evidence_note": "Synthetic canaries, real kernel denials, official runtime and local fake Messages SSE only"}
            import hashlib
            import plistlib
            runtime = VENV / "lib/python3.14/site-packages/deepseek_harness_runtime/runtime/deepseek-harness-sdk-runtime-macos-arm64"
            report["runtime_artifacts"] = {name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size} for name, path in {
                    "dsh_wrapper": VENV / "bin/dsh", "native_runtime": runtime,
                    "spawn_helper": Path(str(runtime) + "-spawn-helper"), "runtime_rg": Path(str(runtime) + "-rg")}.items()}
            report["installed_packages"] = {"deepseek-harness-sdk": VERSION, "deepseek-harness-runtime-bin": VERSION,
                                              "pydantic": "2.14.1", "pydantic_core": "2.50.1"}
            app_info = Path("/Applications/DeepSeek Harness 2.app/Contents/Info.plist")
            if app_info.is_file():
                info = plistlib.loads(app_info.read_bytes())
                report["installed_desktop_version"] = {key: info.get(key) for key in ["CFBundleShortVersionString", "CFBundleVersion"]}
            report["isolation_adapter"] = "Official SDK/native provider with coordinator-applied uniform macOS isolation; not a GUI desktop run"
            if not report["passed"]:
                report.update({"exit_code": process.returncode, "stderr_tail": process.stderr[-1500:],
                               "tool_results": tool_results, "worker_result": record})
            (HERE / "dsh-preflight-evidence.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            return report
        finally:
            cleanup(state)
            relay.close()
            server.shutdown(); server.server_close(); forbidden.close()


def main() -> int:
    if "--tool-runner" in sys.argv:
        args = sys.argv[1:]
        args.remove("--tool-runner")
        _tool_runner(args)
        return 127
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--workspace")
    parser.add_argument("--state-dir")
    parser.add_argument("--broker-url")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()
    if args.worker:
        return _worker(args)
    parser.error("Use build_command() from the coordinator; do not launch an unconfined worker")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
