"""A bounded file-only Bridge channel: candidate commands receive no network or key."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

from .coordinator import _terminate_group, safe_relative
from .isolation import clean_environment
from .records import atomic_json, canonical_hash, clock_record, sha256_file

REQUEST = re.compile(r"^\.bridge-request-(\d{3})\.json$")
REVIEW = re.compile(r"^\.bridge-review-(\d{3})\.json$")
MAX_REQUEST_BYTES = 48 * 1024


def validate_request(value):
    """No model-selected workspace, paths, command argv, URL or credentials."""
    if not isinstance(value, dict) or set(value) - {"task", "acceptance", "smoke_test", "revision_reason"}:
        raise ValueError("Request allows only task, acceptance, smoke_test, revision_reason")
    task, acceptance, smoke = value.get("task"), value.get("acceptance"), value.get("smoke_test", "")
    if not isinstance(task, str) or not 10 <= len(task) <= 20000:
        raise ValueError("task must contain 10..20000 characters")
    if (not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 16 or
            not all(isinstance(item, str) and 1 <= len(item) <= 1000 for item in acceptance)):
        raise ValueError("acceptance must be 1..16 nonempty criteria")
    if not isinstance(smoke, str) or len(smoke) > 16000:
        raise ValueError("smoke_test must be at most 16000 Python characters")
    compile(smoke, "benchmark_smoke.py", "exec")
    reason = value.get("revision_reason", "")
    if not isinstance(reason, str) or len(reason) > 3000:
        raise ValueError("revision_reason must be bounded text")
    return {"task": task, "acceptance": acceptance, "smoke_test": smoke, "revision_reason": reason}


class BridgeMailbox:
    """Process each immutable request once; uncertain submissions are never replayed."""
    def __init__(self, workspace, control_dir, handler, *, max_delegates=2, deadline=None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.control = Path(control_dir).resolve()
        if self.control.is_relative_to(self.workspace):
            raise ValueError("Mailbox control records must be outside the candidate workspace")
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handler, self.max_delegates = handler, max_delegates
        self.deadline = deadline
        self.stop = threading.Event()
        self.thread = None
        self.error = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Mailbox watcher already started")
        self.thread = threading.Thread(target=self._loop, daemon=True, name="bounded-bridge-mailbox")
        self.thread.start()
        return self

    def close(self):
        self.stop.set()
        if hasattr(self.handler, "cancel"):
            self.handler.cancel()
        if self.thread:
            self.thread.join(timeout=10)
        return {"watcher_stopped": not self.thread or not self.thread.is_alive(), "error": self.error}

    def _loop(self):
        try:
            while not self.stop.wait(0.1):
                self.scan_once()
        except Exception as exc:
            self.error = type(exc).__name__
            atomic_json(self.control / "watcher-error.json", {"error": self.error, "time": clock_record()})

    def scan_once(self):
        for path in sorted(self.workspace.glob(".bridge-request-*.json")):
            match = REQUEST.fullmatch(path.name)
            if not match:
                continue
            index = int(match.group(1))
            record_path = self.control / f"request-{index:03d}.json"
            response_path = self.workspace / f".bridge-response-{index:03d}.json"
            if record_path.exists():
                # A crash after claimed/started can have spent provider tokens. Never retry it.
                continue
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
                atomic_json(record_path, {"status": "rejected", "reason": "unsafe_request_file"})
                atomic_json(response_path, {"status": "rejected", "reason": "unsafe_request_file"})
                continue
            if time.time() - path.stat().st_mtime < 0.2:
                continue
            try:
                request = validate_request(json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, SyntaxError, OSError) as exc:
                response = {"status": "rejected", "reason": type(exc).__name__}
                atomic_json(record_path, response); atomic_json(response_path, response)
                continue
            claimed = len(list(self.control.glob("request-*.json")))
            if (not 1 <= index <= self.max_delegates or claimed >= self.max_delegates or
                    (self.deadline is not None and time.monotonic() >= self.deadline)):
                response = {"status": "rejected", "reason": "delegation_or_time_limit"}
                atomic_json(record_path, response); atomic_json(response_path, response)
                continue
            claim = {"status": "started", "request_sha256": canonical_hash(request),
                     "time": clock_record(), "automatic_retry_allowed": False}
            atomic_json(record_path, claim)
            try:
                response = self.handler(request, index)
                if not isinstance(response, dict):
                    raise ValueError("Invalid controller response")
            except Exception as exc:
                response = {"status": "controller_error", "reason": type(exc).__name__,
                            "usage_status": "unknown", "automatic_retry_allowed": False}
            atomic_json(self.control / f"response-{index:03d}.json", response)
            atomic_json(response_path, response)
            atomic_json(record_path, {**claim, "status": "finished", "finished": clock_record(),
                                      "response_sha256": canonical_hash(response)})
        self.scan_reviews()

    def scan_reviews(self):
        for path in sorted(self.workspace.glob(".bridge-review-*.json")):
            if self.stop.is_set() or (self.deadline is not None and time.monotonic() >= self.deadline):
                return
            match = REVIEW.fullmatch(path.name)
            if not match: continue
            index = int(match.group(1))
            claimed = self.control / f"review-{index:03d}.json"
            receipt = self.workspace / f".bridge-receipt-{index:03d}.json"
            if claimed.exists(): continue
            if time.time() - path.lstat().st_mtime < .2: continue
            if (not 1 <= index <= self.max_delegates or path.is_symlink() or not path.is_file() or
                    path.stat().st_size > 128 * 1024 or not hasattr(self.handler, "arbitrate")):
                response = {"status": "rejected", "reason": "unsafe_or_unsupported_review"}
                atomic_json(claimed, response); atomic_json(receipt, response)
                continue
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(report, dict): raise ValueError("Expected review object")
            except (ValueError, OSError):
                response = {"status": "rejected", "reason": "invalid_review_json"}
                atomic_json(claimed, response); atomic_json(receipt, response)
                continue
            atomic_json(claimed, {"status": "started", "report_sha256": canonical_hash(report),
                                  "time": clock_record(), "automatic_retry_allowed": False})
            try:
                response = self.handler.arbitrate(index, report)
            except Exception as exc:
                response = {"status": "rejected", "reason": type(exc).__name__, "copied_files": [],
                            "automatic_retry_allowed": False}
            atomic_json(receipt, response)
            atomic_json(claimed, {"status": "finished", "report_sha256": canonical_hash(report),
                                  "receipt_sha256": canonical_hash(response), "time": clock_record(),
                                  "automatic_retry_allowed": False})


class BridgeSubprocessHandler:
    """Trusted controller that binds paths/argv/profile before receiving model text."""
    def __init__(self, workspace, fixture_files, solution_files, control_dir, bridge_script,
                 state_dir, *, timeout_seconds=180, max_repairs=1, codex_binary=None):
        self.workspace = Path(workspace).resolve(strict=True)
        self.fixture_files = tuple(safe_relative(path) for path in fixture_files)
        self.solution_files = tuple(safe_relative(path) for path in solution_files)
        if not self.solution_files or set(self.solution_files) - set(self.fixture_files):
            raise ValueError("Solutions must belong to fixed fixture files")
        self.control = Path(control_dir).resolve(); self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bridge_script = str(Path(bridge_script).resolve(strict=True))
        self.state = str(Path(state_dir).resolve(strict=True))
        self.timeout = timeout_seconds
        self.max_repairs = max_repairs
        self.codex_binary = codex_binary
        self.active_process = None
        self.accepted_hashes = None
        self.immutable_hashes = {name: sha256_file(self.workspace / name) for name in self.fixture_files
                                 if name not in self.solution_files}

    def cancel(self):
        if self.active_process is not None:
            _terminate_group(self.active_process)

    def private_run(self, output):
        run = Path(output["run_dir"])
        if (run.is_symlink() or not run.resolve().is_relative_to((Path(self.state) / "runs").resolve())):
            raise ValueError("Bridge run outside the fixed private state")
        return run.resolve()

    @staticmethod
    def regular_file(base, name):
        current = Path(base)
        if current.is_symlink(): raise ValueError("Symlinked candidate root")
        for part in Path(name).parts:
            current = current / part
            if current.is_symlink(): raise ValueError("Candidate path contains a symlink")
        if not current.is_file() or current.stat().st_nlink != 1:
            raise ValueError("Candidate must be one regular unlinked file")
        return current

    def expose_candidate(self, output, index):
        run = self.private_run(output)
        stage = run / "workspace"
        preview = self.workspace / f".bridge-candidate-{index:03d}"
        if preview.exists() or preview.is_symlink():
            raise ValueError("Refusing to overwrite a diagnostic preview")
        preview.mkdir(mode=0o700)
        expected = {}
        for name in self.solution_files:
            candidate = self.regular_file(stage, name)
            if candidate.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("Unsafe candidate file")
            target = preview / name; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(candidate.read_bytes()); expected[name] = sha256_file(candidate)
        template = json.loads((run / "desktop-review.template.json").read_text())
        allowed = {key: template.get(key) for key in ("decision", "contract_sha256", "stage_hashes", "criteria", "external_review_resolution", "note")}
        atomic_json(preview / "desktop-review.template.json", allowed)
        atomic_json(self.control / f"delegate-{index:03d}" / "preview-binding.json", expected)
        return preview.name

    def arbitrate(self, index, report):
        """One local apply, without any model request or model-selected path/command."""
        phase = self.control / f"delegate-{index:03d}"
        output = json.loads((phase / "controller-result.json").read_text())
        run = self.private_run(output)
        eligible = bool(output.get("execution_passed") is True and not output.get("issues") and
                        (output.get("verified_stage_hashes") or output.get("checked_stage_hashes")) and
                        output.get("checks_passed") and (output.get("review_passed") or
                        output.get("review", {}).get("verdict") in {"blocked", "changes_requested"}))
        if not eligible:
            raise ValueError("Candidate fixed checks did not pass")
        binding = json.loads((phase / "preview-binding.json").read_text())
        preview = self.workspace / f".bridge-candidate-{index:03d}"
        for name, expected in binding.items():
            candidate = self.regular_file(preview, name)
            if sha256_file(candidate) != expected:
                raise ValueError("Preview was modified; review evidence cannot bind to this candidate")
        for name, expected in self.immutable_hashes.items():
            if sha256_file(self.workspace / name) != expected:
                raise ValueError("Immutable task specification changed")
        before = json.loads((phase / "candidate-before.json").read_text())
        for name in self.solution_files:
            if sha256_file(self.workspace / name) != before[name]:
                raise ValueError("Candidate submission changed before accepted apply")
        report_path = phase / "desktop-review.json"; atomic_json(report_path, report)
        command = [str(Path(sys.executable).resolve()), self.bridge_script, "apply", "--run-dir", str(run),
                   "--desktop-reviewed", "--review-file", str(report_path)]
        env = clean_environment(); env["CODEX_MODEL_BRIDGE_STATE"] = self.state
        if self.codex_binary: env["CODEX_MODEL_BRIDGE_CODEX"] = self.codex_binary
        with (phase / "apply-stdout.json").open("wb") as stdout, (phase / "apply-stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(command, cwd=phase / "input", env=env, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
            self.active_process = process
            try:
                process.wait(timeout=140)
            except subprocess.TimeoutExpired:
                _terminate_group(process)
                raise ValueError("Apply checks timed out") from None
            finally:
                self.active_process = None
        applied = json.loads((phase / "apply-stdout.json").read_text())
        if (process.returncode != 0 or applied.get("status") != "integrated_pending_desktop_acceptance" or
                applied.get("integration_checks_passed") is not True):
            return {"status": "rejected", "reason": "engine_apply_or_checks_rejected", "copied_files": [],
                    "model_requests": 0}
        copied = []
        for name in self.solution_files:
            source = self.regular_file(phase / "input", name)
            if sha256_file(source) != binding[name]:
                raise ValueError("Applied file differs from the reviewed candidate")
            (self.workspace / name).write_bytes(source.read_bytes()); copied.append(name)
        self.accepted_hashes = {name: sha256_file(self.workspace / name) for name in self.solution_files}
        return {"status": "integrated_after_codex_review", "copied_files": copied,
                "submission_hashes": self.accepted_hashes, "model_requests": 0,
                "integration_checks_passed": True}

    def submission_status(self):
        try:
            for name in self.solution_files: self.regular_file(self.workspace, name)
        except ValueError:
            return {"matches_accepted_apply": False, "reason": "unsafe_submission_file",
                    "model_requests_for_arbitration": 0}
        current = {name: sha256_file(self.workspace / name) for name in self.solution_files}
        return {"matches_accepted_apply": self.accepted_hashes is not None and current == self.accepted_hashes,
                "accepted_hashes": self.accepted_hashes, "submitted_hashes": current,
                "model_requests_for_arbitration": 0}

    def __call__(self, request, index):
        phase = self.control / f"delegate-{index:03d}"
        source = phase / "input"
        source.mkdir(parents=True, mode=0o700)
        before = {}
        for name in self.fixture_files:
            candidate = self.workspace / name
            cursor = self.workspace
            for part in Path(name).parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("Candidate path contains a symlink")
            if not candidate.is_file():
                raise ValueError("Candidate fixture no longer a regular file")
            before[name] = sha256_file(candidate)
            if name in self.immutable_hashes and before[name] != self.immutable_hashes[name]:
                raise ValueError("Codex modified an immutable fixture; protocol violation")
            target = source / name; target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(candidate.read_bytes())
        smoke = source / "benchmark_smoke.py"
        smoke.write_text(request["smoke_test"] + "\nprint('BRIDGE_SMOKE_OK')\n")
        compile_source = "import pathlib; " + "; ".join(
            "compile(pathlib.Path(" + repr(name) + ").read_text()," + repr(name) + ",'exec')"
            for name in self.solution_files) + "; print('BRIDGE_COMPILE_OK')"
        checks = [{"name": "public_syntax", "argv": [str(Path(sys.executable).resolve()), "-B", "-c", compile_source],
                   "timeout_seconds": 20, "success_contains": ["BRIDGE_COMPILE_OK"]},
                  {"name": "codex_authored_smoke", "argv": [str(Path(sys.executable).resolve()), "-B", "benchmark_smoke.py"],
                   "timeout_seconds": 30, "success_contains": ["BRIDGE_SMOKE_OK"]}]
        task = {"workspace": str(source), "risk_level": "low", "task": request["task"],
                "input_files": [*self.fixture_files, "benchmark_smoke.py"], "writable_files": list(self.solution_files),
                "verification_files": ["benchmark_smoke.py"], "acceptance": request["acceptance"],
                "acceptance_checks": checks}
        task_path = phase / "task.json"; atomic_json(task_path, task)
        atomic_json(phase / "candidate-before.json", before)
        env = clean_environment(); env["CODEX_MODEL_BRIDGE_STATE"] = self.state
        if self.codex_binary:
            env["CODEX_MODEL_BRIDGE_CODEX"] = self.codex_binary
        command = [str(Path(sys.executable).resolve()), self.bridge_script, "run", "--mixed", "--details",
                   "--task-file", str(task_path), "--timeout", str(self.timeout),
                   "--max-repairs", str(self.max_repairs)]
        with (phase / "engine-stdout.json").open("wb") as stdout, (phase / "engine-stderr.txt").open("wb") as stderr:
            try:
                process = subprocess.Popen(command, cwd=source, env=env, stdout=stdout, stderr=stderr,
                                           start_new_session=True)
                self.active_process = process
                process.wait(timeout=self.timeout + 15)
            except subprocess.TimeoutExpired:
                _terminate_group(process)
                return {"status": "timeout", "usage_status": "unknown", "copied_files": [],
                        "automatic_retry_allowed": False}
            finally:
                self.active_process = None
        output = json.loads((phase / "engine-stdout.json").read_text())
        atomic_json(phase / "controller-result.json", output)
        preview = self.expose_candidate(output, index) if output.get("run_dir") else None
        ready = bool(output.get("execution_passed") is True and not output.get("issues") and
                     (output.get("verified_stage_hashes") or output.get("checked_stage_hashes")) and
                     output.get("checks_passed") and (output.get("review_passed") or
                     output.get("review", {}).get("verdict") in {"blocked", "changes_requested"}))
        # Only bounded evidence crosses the mailbox; profile, run paths and hidden graders do not.
        review = output.get("review") or {}
        return {"status": "awaiting_codex_review" if ready else "needs_codex_attention",
                "engine_exit": process.returncode, "copied_files": [], "candidate_preview": preview,
                "review_file": f".bridge-review-{index:03d}.json", "receipt_file": f".bridge-receipt-{index:03d}.json",
                "integration": "unsubmitted_preview_only",
                "checks_passed": bool(output.get("checks_passed")),
                "checks": [{"name": row.get("name"), "exit_code": row.get("exit_code"),
                            "stop_reason": row.get("stop_reason")} for row in output.get("checks", [])],
                "review": {key: review.get(key) for key in ("verdict", "summary", "findings", "unverified")},
                "external_usage": output.get("external_usage"),
                "failure_class": output.get("failure_class"),
                "escalation_reason": str(output.get("escalation_reason", ""))[:2000],
                "next": "Inspect preview and complete its review template, write .bridge-review-NNN.json; only a successful trusted apply receipt submits solution files."}


def hybrid_instructions():
    return """This is the hybrid treatment. You own planning and final acceptance. The external model must write the main solution.
Do not implement the main solution yourself. Read INSTRUCTIONS.md and INTERFACE.md, formulate the algorithm, acceptance criteria and useful local smoke assertions.
To delegate, atomically write .bridge-request-001.json in this workspace with exactly these JSON fields:
{\"task\":\"a complete scoped implementation plan\",\"acceptance\":[\"specific verifiable criteria\"],\"smoke_test\":\"Python smoke-test source using only this workspace and stdlib\"}
No path, workspace, argv, API URL, model or key fields are allowed. A trusted outside controller binds all permissions and supplies the external model. The request file must be written atomically (temporary file then rename).
Wait for .bridge-response-001.json with one bounded Python polling loop (up to 210 seconds, sleeping 0.5 seconds between local existence checks) instead of repeatedly asking the model to poll. The controller may take several minutes. Do not read other projects, history, hidden tests or reference answers. You receive no networking or API credential.
If awaiting_codex_review, inspect .bridge-candidate-001/<solution> and its desktop-review.template.json. The final solution remains unchanged. Run public smoke checks against that preview without editing it, and decide each criterion with concrete evidence.
If you accept, copy the template into .bridge-review-001.json, set decision=accept and every criterion status=pass with locating evidence. If external_review_resolution is present, resolve every finding/unverified id with specific evidence and explain the arbitration reason. The trusted host performs the real Bridge apply and fixed checks; this uses no new model request and does not count as another delegation.
Wait for .bridge-receipt-001.json. Only integrated_after_codex_review submits the final solution. Do not copy or write the preview/main solution yourself: the coordinator verifies final hashes against the accepted apply receipt. Preview edits invalidate review.
If something is wrong, diagnose the concrete mismatch; decide whether your plan/checks were incomplete or the implementation was wrong. At most one revised delegation is allowed, named .bridge-request-002.json, with a revision_reason describing the evidence and changed plan. Do not blindly repeat the same request.
Do not silently switch to implementing the solution yourself. If blocked after two delegations, report the failure accurately. End with a concise acceptance report, identifying any unverified requirement. No hidden evaluator is available in the workspace.
"""
