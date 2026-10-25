"""Fail-closed local command sandbox probes, with no model/network API requests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile

from .records import canonical_hash, clock_record, sha256_file

PROFILE = "benchmark-worker"


def discover_codex():
    candidates = [shutil.which("codex")]
    for app in ("ChatGPT", "Codex"):
        candidates.extend((f"/Applications/{app}.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                           f"/Applications/{app}.app/Contents/Resources/codex"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise RuntimeError("No Codex CLI found; no sandbox fallback is permitted")


def clean_environment():
    # HOME is preserved for authentication discovery, never changed globally or in children.
    env = {key: value for key, value in os.environ.items()
           if key in {"HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update({"PATH": str(Path(sys.executable).resolve().parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def permission_config(read_roots=(), access="write"):
    if access not in {"read", "write"}:
        raise ValueError("Expected read or write access")
    roots = {str(Path(sys.executable).resolve().parent.parent), *map(lambda p: str(Path(p).resolve()), read_roots)}
    rules = {":root": "deny", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny",
             ":workspace_roots": {".": access}}
    rules.update({root: "read" for root in sorted(roots)})
    # TOML inline syntax is deliberately built at one table boundary, without dotted-path escaping.
    filesystem = ",".join(json.dumps(key) + "=" +
                          ("{\".\"=" + json.dumps(access) + "}" if isinstance(value, dict) else json.dumps(value))
                          for key, value in rules.items())
    return f'permissions={{{PROFILE}={{extends=":workspace",filesystem={{{filesystem}}},network={{enabled=false}}}}}}'


def sandbox_command(workspace, argv, codex=None, read_roots=(), access="write"):
    return [codex or discover_codex(), "sandbox", "-C", str(Path(workspace).resolve()),
            "-P", PROFILE, "-c", permission_config(read_roots, access), "--", *map(str, argv)]


def capabilities():
    result = {"platform": platform.system(), "machine": platform.machine(),
              "python": str(Path(sys.executable).resolve()), "python_version": platform.python_version(),
              "sandbox_exec": shutil.which("sandbox-exec"), "docker": shutil.which("docker"),
              "node": shutil.which("node"), "dsh": shutil.which("dsh")}
    try:
        codex = discover_codex()
        version = subprocess.run([codex, "--version"], text=True, capture_output=True, timeout=15)
        result.update({"codex": codex, "codex_sha256": sha256_file(codex),
                       "codex_version": version.stdout.strip(), "codex_version_exit": version.returncode})
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result["codex_error"] = type(exc).__name__
    return result


PROBE = r'''
import json, pathlib, socket, sys
workspace, outside, sibling, host, port = sys.argv[1:]
def readable(path):
    try:
        pathlib.Path(path).read_bytes()
        return True
    except OSError:
        return False
result = {"inside_read":readable(pathlib.Path(workspace)/"inside.txt"),
          "outside_read":readable(outside), "sibling_read":readable(sibling),
          "symlink_read":readable(pathlib.Path(workspace)/"link-outside")}
try:
    pathlib.Path(workspace,"created.txt").write_text("local")
    result["inside_write"] = True
except OSError:
    result["inside_write"] = False
try:
    pathlib.Path(outside).write_text("escaped")
    result["outside_write"] = True
except OSError:
    result["outside_write"] = False
try:
    socket.create_connection((host,int(port)),timeout=2).close()
    result["network_connect"] = True
    result["network_denied_by_policy"] = False
except PermissionError:
    result["network_connect"] = False
    result["network_denied_by_policy"] = True
except OSError as exc:
    result["network_connect"] = False
    result["network_denied_by_policy"] = False
    result["network_error_errno"] = exc.errno
print(json.dumps(result))
'''


def probe_isolation(codex=None, read_roots=()):
    """Probe the actual installed local-command sandbox, not an inference simulation."""
    codex = codex or discover_codex()
    result = {"schema_version": 1, "started": clock_record(), "scope": "local_command_sandbox",
              "agent_input_audit": "not_established_by_this_probe", "passed": False,
              "codex_sha256": sha256_file(codex),
              "policy_sha256": canonical_hash(permission_config(read_roots)),
              "read_roots": sorted(str(Path(p).resolve()) for p in read_roots)}
    with tempfile.TemporaryDirectory(prefix="cbridge-isolation-") as temporary:
        base = Path(temporary).resolve()
        work, other = base / "work", base / "other"
        work.mkdir(); other.mkdir()
        (work / "inside.txt").write_text("allowed")
        outside, sibling = base / "outside.txt", other / "answer.txt"
        outside.write_text("outside-sentinel")
        sibling.write_text("sibling-sentinel")
        (work / "link-outside").symlink_to(outside)
        script = work / "probe.py"
        script.write_text(PROBE)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); listener.listen(4)
            host, port = listener.getsockname()
            socket.create_connection((host, port), timeout=2).close()
            argv = [str(Path(sys.executable).resolve()), str(script), str(work), str(outside),
                    str(sibling), host, str(port)]
            try:
                process = subprocess.run(sandbox_command(work, argv, codex, read_roots),
                                         cwd=work, env=clean_environment(), text=True,
                                         capture_output=True, timeout=20)
                result["exit_code"] = process.returncode
                # Only structured boolean results are persisted, never sentinel contents or tool stderr.
                observations = json.loads(process.stdout.strip())
                result["observations"] = observations
                result["passed"] = (process.returncode == 0 and
                                    observations.get("inside_read") is True and
                                    observations.get("inside_write") is True and
                                    observations.get("outside_read") is False and
                                    observations.get("outside_write") is False and
                                    observations.get("sibling_read") is False and
                                    observations.get("symlink_read") is False and
                                    observations.get("network_denied_by_policy") is True and
                                    outside.read_text() == "outside-sentinel")
                if not result["passed"]:
                    result["error"] = "isolation_probe_failed"
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                result["error"] = type(exc).__name__
    result["finished"] = clock_record()
    return result


def require_probe(probe, codex, read_roots=()):
    if (not probe.get("passed") or probe.get("scope") != "local_command_sandbox" or
            probe.get("codex_sha256") != sha256_file(codex) or
            probe.get("policy_sha256") != canonical_hash(permission_config(read_roots))):
        raise RuntimeError("Missing, failed, or stale isolation probe; experiment launch refused")
