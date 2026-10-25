"""Trusted host controls. A candidate sandbox cannot access these control files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from .records import atomic_json, clock_record
from .records import sha256_file


def snapshot_sources(control_root, repository_root, expected_sources):
    """Retain actual frozen source bytes before launching; hashes alone are insufficient."""
    control, repository = Path(control_root).resolve(), Path(repository_root).resolve()
    snapshot = control / "source-snapshot"
    for relative, expected in expected_sources.items():
        parts = Path(relative)
        if parts.is_absolute() or ".." in parts.parts or not relative.endswith(".py"):
            raise ValueError("Invalid implementation source snapshot path")
        original = repository / relative
        if original.is_symlink() or sha256_file(original) != expected:
            raise RuntimeError("Implementation changed while snapshotting")
        target = snapshot / relative
        if target.exists() or target.is_symlink():
            if target.is_symlink() or sha256_file(target) != expected:
                raise RuntimeError("Refusing to replace an existing source snapshot")
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = original.read_bytes()
        target.write_bytes(data); target.chmod(0o600)
        if sha256_file(target) != expected:
            raise RuntimeError("Source changed during copy; experiment must not start")
    atomic_json(snapshot / "snapshot-manifest.json", {"schema_version": 1,
                "sources": expected_sources, "verified": True, "created": clock_record(),
                "model_requests": 0})
    return snapshot


def stop_request(control_root):
    path = Path(control_root) / "stop-after-current.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        return {"requested": True, "reason": "unsafe_stop_control_file"}
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError):
        return {"requested": True, "reason": "invalid_stop_control_file"}
    if not isinstance(value, dict) or value.get("requested") is not True:
        return {"requested": True, "reason": "invalid_stop_control_file"}
    return {"requested": True, "reason": "operator_stop_after_current", "requested_at": value.get("requested_at")}


def request_stop(control_root):
    control = Path(control_root).resolve()
    value = {"schema_version": 1, "requested": True, "reason": "operator_stop_after_current",
             "requested_at": clock_record(), "effect": "finish current run/grading/accounting; do not launch next"}
    atomic_json(control / "stop-after-current.json", value)
    return value


def clear_stop(control_root):
    """Explicit operator action; retains history and never resumes or resends a model request."""
    control = Path(control_root).resolve()
    path = control / "stop-after-current.json"
    if not path.exists() and not path.is_symlink():
        return {"cleared": False, "reason": "no_stop_request"}
    if path.is_symlink():
        raise ValueError("Refusing to clear a symlinked control file")
    history = control / "stop-control-history"
    history.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = history / (str(time.time_ns()) + ".json")
    path.replace(destination)
    return {"cleared": True, "automatic_resume": False, "automatic_retry": False}


def settle_accounting(broker, budget, timeout_seconds=8):
    """Wait for already-submitted requests only; never issue a retry or call budget.check."""
    if hasattr(broker, "settle"):
        value = broker.settle(timeout_seconds=timeout_seconds)
        if not isinstance(value, dict) or not isinstance(value.get("settled"), bool):
            return {"settled": False, "usage_complete": False, "reason": "invalid_settlement_result"}
        return value
    deadline = time.monotonic() + timeout_seconds
    while True:
        summary = budget.ledger.summary()
        unfinished = summary.get("unfinished_requests")
        pending = len(unfinished) if isinstance(unfinished, list) else unfinished
        if pending == 0 or time.monotonic() >= deadline:
            return {"settled": pending == 0, "pending_requests": pending,
                    "requests_started": summary.get("requests_started"),
                    "requests_finished": summary.get("requests_finished"),
                    "usage_complete": summary.get("usage_complete") is True,
                    "source": "trusted_ledger_poll_no_new_requests"}
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description="宿主控制：当前任务结束后停止；不会调用模型")
    parser.add_argument("action", choices=("stop", "status", "clear"))
    parser.add_argument("control_root")
    args = parser.parse_args()
    value = request_stop(args.control_root) if args.action == "stop" else clear_stop(args.control_root) if args.action == "clear" else stop_request(args.control_root)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
