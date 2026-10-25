"""Fresh-workspace execution, durable checkpoints and independent grading hooks."""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import subprocess
import tempfile
import time

from .adapters import parse_dsh_outcome
from .records import (append_jsonl, atomic_json, canonical_hash, clock_record,
                      import_codex_jsonl, sha256_file, summarize_usage)

ARMS = {"codex_pure", "codex_bridge", "deepseek_harness"}
TERMINAL = {"completed", "failed", "timeout", "output_limit", "uncertain", "launch_failed"}
FORBIDDEN = {".git", ".codex", ".agents", ".ssh", ".env", "credentials.json", "auth.json", "profile.json"}


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Expected a nonempty POSIX relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("Relative traversal is forbidden")
    if any(part in FORBIDDEN or part.startswith(".env.") for part in relative.parts):
        raise ValueError("Credentials and agent configuration are not task inputs")
    return str(relative)


def tree_manifest(root):
    root = Path(root).resolve()
    rows = []
    for path in sorted(root.rglob("*")):
        relative = safe_relative(path.relative_to(root).as_posix())
        if path.is_symlink():
            raise ValueError("Symlink in task tree: " + relative)
        mode = path.stat().st_mode
        if path.is_dir():
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Special file in task tree: " + relative)
        if path.stat().st_nlink != 1:
            raise ValueError("Hardlink in task tree: " + relative)
        rows.append({"path": relative, "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return rows


def prepare_run(control_root, task_id, fixture_dir, arm, repeat, prompt, *, source_metadata=None,
                writable_files=None):
    if arm not in ARMS or not isinstance(repeat, int) or not 1 <= repeat <= 100:
        raise ValueError("Invalid arm or repeat")
    if safe_relative(task_id) != task_id or "/" in task_id:
        raise ValueError("Task ID must be one safe path component")
    control_root = Path(control_root).resolve()
    run_dir = control_root / task_id / arm / f"r{repeat:02d}"
    if run_dir.exists():
        raise RuntimeError("Run already exists; use checkpoint, never overwrite a prior attempt")
    fixture = Path(fixture_dir).resolve(strict=True)
    original = tree_manifest(fixture)
    if writable_files is not None:
        writable_files = sorted(set(safe_relative(path) for path in writable_files))
    if not original or sum(row["bytes"] for row in original) > 64 * 1024 * 1024:
        raise ValueError("Expected a nonempty task fixture at most 64 MiB")
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    work = run_dir / "workspace"
    work.mkdir(mode=0o700)
    for row in original:
        source = fixture / row["path"]
        target = work / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Copy bytes rather than link/reuse Git shared metadata or historical worktrees.
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    if tree_manifest(work) != original:
        raise RuntimeError("Fixture changed during copy")
    manifest = {"schema_version": 1, "task_id": task_id, "arm": arm, "repeat": repeat,
                "prepared": clock_record(), "fixture": original,
                "fixture_sha256": canonical_hash(original), "prompt_sha256": canonical_hash(prompt),
                "source": source_metadata or {}, "workspace": str(work),
                "writable_files": writable_files,
                "measurement_context": "not_started", "status": "prepared"}
    atomic_json(run_dir / "manifest.json", manifest)
    atomic_json(run_dir / "checkpoint.json", {"state": "prepared", "updated": clock_record()})
    return run_dir


def checkpoint(run_dir):
    """Crash after submission is uncertain. A coordinator never silently sends it again."""
    path = Path(run_dir) / "checkpoint.json"
    record = json.loads(path.read_text())
    if record["state"] in {"launching", "running"}:
        supervisor = record.get("coordinator_pid")
        if isinstance(supervisor, int) and supervisor > 0:
            try:
                os.kill(supervisor, 0)
                return record
            except ProcessLookupError:
                pass
            except PermissionError:
                return record
        record.update({"state": "uncertain", "reason": "interrupted_after_launch_intent",
                       "automatic_retry_allowed": False, "updated": clock_record()})
        atomic_json(path, record)
    return record


def _terminate_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    # Reap descendants even if a shell exited before its children.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def execute_run(run_dir, plan, *, timeout_seconds=600, output_limit_bytes=16 * 1024 * 1024,
                authorized_model_requests=False, launch_guard=None):
    """Execute exactly once. launch_guard enforces campaign budget and final adapter audit."""
    if not authorized_model_requests:
        raise RuntimeError("Model requests not authorized; only offline probes/tests may run")
    run_dir = Path(run_dir)
    state = checkpoint(run_dir)
    if state["state"] != "prepared":
        raise RuntimeError("Run is not fresh/prepared; completed and uncertain attempts are never resubmitted")
    if not callable(launch_guard):
        raise RuntimeError("Campaign budget and adapter-isolation launch guard required")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    work = Path(manifest["workspace"])
    if tree_manifest(work) != manifest["fixture"]:
        raise RuntimeError("Workspace no longer matches original fixture before execution")
    if canonical_hash(plan.prompt) != manifest["prompt_sha256"]:
        raise RuntimeError("Prompt differs from pre-registered task treatment")
    if plan.adapter != manifest["arm"]:
        raise RuntimeError("Adapter does not match assigned arm")
    guard = launch_guard(plan)
    if not isinstance(guard, dict) or not guard.get("allowed"):
        atomic_json(run_dir / "blocked.json", guard if isinstance(guard, dict) else {"reason": "invalid_guard"})
        raise RuntimeError("Budget or isolation guard refused experiment launch")
    manifest.update({"measurement_context": plan.context, "command": plan.public_record(),
                     "launch_guard": guard})
    atomic_json(run_dir / "manifest.json", manifest)
    start = clock_record()
    atomic_json(run_dir / "checkpoint.json", {"state": "launching", "started": start,
                                               "coordinator_pid": os.getpid(),
                                               "automatic_retry_allowed": False})
    stdout_path, stderr_path = run_dir / "events.jsonl", run_dir / "stderr.txt"
    reason, returncode = None, None
    first_event_ns = None
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr, tempfile.TemporaryFile() as input_stream:
        input_stream.write(plan.prompt.encode())
        input_stream.seek(0)
        try:
            process = subprocess.Popen(plan.argv, cwd=work, env=plan.environment,
                                       stdin=input_stream, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
        except OSError as exc:
            reason = "launch_failed"
            launch_error = type(exc).__name__
        else:
            atomic_json(run_dir / "checkpoint.json", {"state": "running", "started": start,
                                                       "coordinator_pid": os.getpid(),
                                                       "pid": process.pid, "automatic_retry_allowed": False})
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if first_event_ns is None and os.fstat(stdout.fileno()).st_size:
                    first_event_ns = time.monotonic_ns()
                if time.monotonic() >= deadline:
                    reason = "timeout"
                elif os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > output_limit_bytes:
                    reason = "output_limit"
                if reason:
                    _terminate_group(process)
                    break
                time.sleep(0.05)
            returncode = process.wait()
            _terminate_group(process)
    finish = clock_record()
    stdout_path.chmod(0o600); stderr_path.chmod(0o600)
    if reason is None and stdout_path.stat().st_size + stderr_path.stat().st_size > output_limit_bytes:
        reason = "output_limit"
    status = reason or ("completed" if returncode == 0 else "failed")
    usage = None
    outcome = None
    if plan.adapter.startswith("codex_"):
        usage = import_codex_jsonl(stdout_path)
        if status == "completed" and not any(row.get("status") == "reported" for row in usage["records"]):
            status = "uncertain"
    elif plan.adapter == "deepseek_harness":
        if plan.safety.get("outcome_file"):
            try:
                sdk_result = json.loads(Path(plan.safety["outcome_file"]).read_text())
                outcome = {"completed": returncode == 0 and sdk_result.get("finish_reason") == "completed",
                           "finish_reason": sdk_result.get("finish_reason"),
                           "usage_status": "requires_broker_request_ledger"}
            except (ValueError, OSError):
                outcome = {"completed": False, "finish_reason": "missing_or_invalid_sdk_result",
                           "usage_status": "unknown"}
        else:
            outcome = parse_dsh_outcome(stdout_path, returncode)
        if status == "completed" and not outcome["completed"]:
            status = "failed"
    result = {"schema_version": 1, "state": status, "exit_code": returncode,
              "started": start, "finished": finish,
              "wall_seconds": (finish["monotonic_ns"] - start["monotonic_ns"]) / 1e9,
              "first_event_observed_seconds": ((first_event_ns - start["monotonic_ns"]) / 1e9
                                                if first_event_ns is not None else None),
              "first_event_precision_seconds": 0.05,
              "first_event_is_first_token": False,
              "measurement_context": plan.context, "automatic_retry_allowed": False,
              "events_sha256": sha256_file(stdout_path), "stderr_sha256": sha256_file(stderr_path),
              "usage": usage, "adapter_outcome": outcome, "grading": "not_run"}
    if reason == "launch_failed":
        result["launch_error"] = launch_error
    try:
        result["artifacts"] = tree_manifest(work)
        if manifest.get("writable_files") is not None:
            final_hashes = {row["path"]: row["sha256"] for row in result["artifacts"]}
            changed_immutable = [row["path"] for row in manifest["fixture"]
                                 if row["path"] not in manifest["writable_files"] and
                                 final_hashes.get(row["path"]) != row["sha256"]]
            if changed_immutable:
                result.update({"state": "failed", "changed_immutable_inputs": changed_immutable})
    except (OSError, ValueError) as exc:
        result.update({"state": "failed", "artifact_error": type(exc).__name__})
    atomic_json(run_dir / "result.json", result)
    atomic_json(run_dir / "checkpoint.json", {"state": result["state"], "updated": finish,
                                               "automatic_retry_allowed": False})
    append_jsonl(run_dir.parents[2] / "campaign-events.jsonl",
                 {"task_id": manifest["task_id"], "arm": manifest["arm"], "repeat": manifest["repeat"],
                  "state": result["state"], "wall_seconds": result["wall_seconds"]})
    return result


def record_grade(run_dir, grade, *, grader_files):
    """Attach a separately executed immutable grader result after the worker has stopped."""
    run_dir = Path(run_dir)
    if checkpoint(run_dir)["state"] not in TERMINAL:
        raise RuntimeError("Worker still active; grading is a separate phase")
    if not isinstance(grade, dict) or grade.get("source") != "independent_grader":
        raise ValueError("Model self-report is not a benchmark grade")
    record = {"grade": grade, "recorded": clock_record(),
              "grader_files": [{"path": str(Path(path).resolve()), "sha256": sha256_file(path)}
                               for path in grader_files],
              "artifact_sha256": canonical_hash(tree_manifest(run_dir / "workspace"))}
    atomic_json(run_dir / "grade.json", record)
    return record
