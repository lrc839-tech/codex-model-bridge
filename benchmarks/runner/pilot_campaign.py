"""Opt-in sequential 6×3 pilot; root supplies budget broker and audited DSH adapter."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from .adapters import CommandPlan, codex_plan, probe_codex_capabilities
from .coordinator import (checkpoint, execute_run, prepare_run, record_grade, safe_relative,
                          tree_manifest)
from .control import settle_accounting, snapshot_sources, stop_request
from .isolation import clean_environment, probe_isolation, sandbox_command
from .mailbox import BridgeMailbox, BridgeSubprocessHandler, hybrid_instructions
from .records import (atomic_json, canonical_hash, clock_record, import_request_ledger,
                      sha256_file, summarize_usage)

ARMS = ("codex_pure", "codex_bridge", "deepseek_harness")
REPO = Path(__file__).resolve().parents[2]


def implementation_hashes(repo=REPO):
    repo = Path(repo)
    files = [repo / "benchmarks/api_broker.py", repo / "benchmarks/dsh_adapter.py",
             repo / "skills/codex-model-bridge/scripts/bridge.py"]
    files += list((repo / "benchmarks/runner").glob("*.py"))
    files += list((repo / "skills/codex-model-bridge/runtime").glob("*.py"))
    return {path.relative_to(repo).as_posix(): sha256_file(path) for path in sorted(set(files))}


def load_tasks(candidate_path, repo=REPO):
    document = json.loads(Path(candidate_path).read_text())
    if document.get("schema_version") != 1 or len(document.get("tasks", [])) != 6:
        raise ValueError("This pre-registered pilot requires exactly six frozen tasks")
    for task in document["tasks"]:
        for directory, files in (("fixture_dir", "fixture_files"), ("evaluator_dir", "evaluator_files")):
            root = Path(repo) / safe_relative(task[directory])
            for name, expected in task[files].items():
                target = root / safe_relative(name)
                if target.is_symlink() or sha256_file(target) != expected:
                    raise RuntimeError("Frozen benchmark source changed: " + task["id"] + "/" + name)
    return document


def schedule(tasks):
    """Balanced arm position; all six arm permutations occur exactly once."""
    orders = ((0, 1, 2), (1, 2, 0), (2, 0, 1), (0, 2, 1), (2, 1, 0), (1, 0, 2))
    return [{"task": task, "arm": ARMS[arm], "repeat": 1}
            for task, order in zip(tasks, orders) for arm in order]


def common_prompt(task):
    return ("Implement this self-contained Python exercise in the current workspace.\n"
            "Read INSTRUCTIONS.md and INTERFACE.md for the complete public requirements.\n"
            "Main solution files: " + ", ".join(task["solution_files"]) + ".\n"
            "Use only Python standard library. Preserve public interfaces and exception messages.\n"
            "Only modify the solution files; temporary self-written smoke checks may be created locally.\n"
            "Do not read other directories/projects, past answers, chats, memories, hidden evaluators, "
            "reference solutions or external search. No network, package downloads or extra dependencies.\n"
            "The official hidden tests are withheld from every solver. You can derive local smoke checks "
            "from the same public specification. Finish with a concise evidence-based acceptance report.\n")


def independent_grade(run_dir, task, *, repo=REPO, codex=None):
    """Only frozen official tests/helpers plus final solution enter a new grader sandbox."""
    run_dir = Path(run_dir)
    grade_root = run_dir / "grader-workspace"
    if grade_root.exists():
        return json.loads((run_dir / "grade.json").read_text())["grade"]
    grade_root.mkdir(mode=0o700)
    evaluator = Path(repo) / task["evaluator_dir"]
    copied, grader_paths = set(), []
    for name in task["grading"]["copy_files"]:
        name = safe_relative(name)
        if name.startswith("reference/") or name not in task["evaluator_files"]:
            raise ValueError("Reference answers must never enter the grader")
        original = evaluator / name
        if sha256_file(original) != task["evaluator_files"][name]:
            raise RuntimeError("Official grader changed after pre-registration")
        target = grade_root / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original.read_bytes()); copied.add(name); grader_paths.append(original)
    for name in task["solution_files"]:
        name = safe_relative(name)
        candidate = run_dir / "workspace" / name
        if candidate.is_symlink() or not candidate.is_file():
            grade = {"source": "independent_grader", "passed": False, "tests_passed": 0,
                     "tests_total": task["grading"]["total_tests"], "reason": "missing_or_unsafe_solution"}
            record_grade(run_dir, grade, grader_files=grader_paths)
            return grade
        (grade_root / name).write_bytes(candidate.read_bytes())
    argv = [str(Path(sys.executable).resolve()), "-B", "-m", "unittest", "discover", "-s", ".",
            "-p", task["grading"]["pattern"], "-v"]
    before_hashes = tree_manifest(grade_root)
    started = clock_record()
    try:
        process = subprocess.run(sandbox_command(grade_root, argv, codex, access="read"), cwd=grade_root,
                                 env=clean_environment(), capture_output=True, text=True,
                                 timeout=task["grading"].get("timeout_seconds", 60))
        output = process.stdout + "\n" + process.stderr
        ran = re.search(r"Ran (\d+) tests?", output)
        count = int(ran.group(1)) if ran else 0
        counters = {key: int(value) for key, value in re.findall(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)", output)}
        failed = sum(counters.values())
        success = (process.returncode == 0 and count == task["grading"]["total_tests"] and
                   not counters and re.search(r"^OK\s*$", output, re.MULTILINE) is not None)
        grade = {"source": "independent_grader", "passed": success, "exit_code": process.returncode,
                 "tests_run": count, "tests_passed": max(0, count - failed),
                 "tests_total": task["grading"]["total_tests"], "counters": counters,
                 "criterion": task["grading"]["pass_rule"]}
        grade["file_snapshot_unchanged"] = tree_manifest(grade_root) == before_hashes
        grade["original_grader_unchanged"] = all(
            sha256_file(evaluator / name) == task["evaluator_files"][name]
            for name in task["grading"]["copy_files"])
        if not grade["file_snapshot_unchanged"] or not grade["original_grader_unchanged"]:
            grade.update({"passed": False, "reason": "grader_artifact_changed"})
        (run_dir / "grader-stdout.txt").write_text(process.stdout)
        (run_dir / "grader-stderr.txt").write_text(process.stderr)
    except subprocess.TimeoutExpired:
        grade = {"source": "independent_grader", "passed": False, "reason": "grader_timeout",
                 "tests_passed": None, "tests_total": task["grading"]["total_tests"]}
    finish = clock_record()
    grade["wall_seconds"] = (finish["monotonic_ns"] - started["monotonic_ns"]) / 1e9
    grade["sandbox_access"] = "read"
    grade["anti_cheating_limit"] = "Read-only files prevent direct test edits; Python in-memory monkeypatching is not fully prevented."
    record_grade(run_dir, grade, grader_files=grader_paths)
    return grade


def run_campaign(control_root, *, budget, broker, dsh_factory, dsh_preflight,
                 candidates=None, repo=REPO, codex_model="gpt-6.1-sol", reasoning_effort="medium",
                 per_run_seconds=360, bridge_timeout=180, authorized_model_requests=False,
                 grader=independent_grade, mailbox_handler_factory=None, dsh_cleanup=None):
    """Caller owns provider credentials. This function never discovers historical sessions."""
    if not authorized_model_requests:
        raise RuntimeError("Pass explicit authorization only after the user's campaign budget is accepted")
    control = Path(control_root).resolve(); control.mkdir(parents=True, exist_ok=True, mode=0o700)
    document = load_tasks(candidates or Path(repo) / "benchmarks/task-candidates.json", repo)
    capability = probe_codex_capabilities()
    probe = probe_isolation(capability["binary"])
    if not capability["passed"] or not probe["passed"]:
        raise RuntimeError("Codex capability or command isolation preflight failed")
    dsh_evidence = dsh_preflight()
    if not isinstance(dsh_evidence, dict) or not dsh_evidence.get("passed"):
        atomic_json(control / "blocked-dsh-preflight.json", dsh_evidence or {"passed": False})
        raise RuntimeError("DSH preflight failed; no arm will start before all three are ready")
    roster = schedule(document["tasks"])
    preregistration = {"schema_version": 1, "measurement_context": "cli_control",
        "desktop_product_equivalent": False, "candidate_source_sha256": canonical_hash(document),
        "codex_model": codex_model, "reasoning_effort": reasoning_effort,
        "external_model": "deepseek-flash", "external_thinking": "disabled", "external_max_tokens": 8192,
        "per_run_seconds": per_run_seconds, "bridge_timeout_seconds": bridge_timeout,
        "max_bridge_delegates": 2, "max_internal_repairs_per_delegate": 1,
        "schedule": [{"task_id": row["task"]["id"], "arm": row["arm"], "repeat": 1} for row in roster],
        "codex_capability": capability, "command_isolation": probe, "dsh_preflight": dsh_evidence,
        "hybrid_instruction_sha256": canonical_hash(hybrid_instructions()),
        "implementation_sources": implementation_hashes(repo),
        "notes": ["Official public tasks may have appeared in model training; measures workflow on this sample.",
                  "One repetition is a pilot, not a statistically established general ranking.",
                  "Codex CLI token accounting does not claim desktop chat usage."]}
    prereg_path = control / "preregistration.json"
    if prereg_path.exists():
        old = json.loads(prereg_path.read_text())
        for key in ("candidate_source_sha256", "codex_model", "reasoning_effort", "schedule",
                    "hybrid_instruction_sha256", "per_run_seconds", "bridge_timeout_seconds"):
            if old[key] != preregistration[key]:
                raise RuntimeError("Existing campaign treatment changed; use a new campaign instead")
        if old.get("implementation_sources") != preregistration["implementation_sources"]:
            raise RuntimeError("Implementation changed after campaign freeze; do not resume as the same experiment")
    else:
        atomic_json(prereg_path, preregistration)
    snapshot_sources(control, repo, preregistration["implementation_sources"])
    results = []
    for position, assignment in enumerate(roster, 1):
        if implementation_hashes(repo) != preregistration["implementation_sources"]:
            atomic_json(control / "stopped.json", {"reason": "implementation_changed_after_freeze",
                                                     "schedule_position": position, "time": clock_record()})
            break
        task, arm, repeat = assignment["task"], assignment["arm"], assignment["repeat"]
        run_dir = control / "runs" / task["id"] / arm / "r01"
        if run_dir.exists() and checkpoint(run_dir)["state"] != "prepared":
            state = checkpoint(run_dir)["state"]
            grade = (json.loads((run_dir / "grade.json").read_text())["grade"]
                     if (run_dir / "grade.json").exists() else grader(run_dir, task, repo=repo, codex=capability["binary"]))
            results.append({"task_id": task["id"], "arm": arm, "state": state, "grade": grade,
                            "resumed_without_resubmission": True})
            continue
        requested_stop = stop_request(control)
        if requested_stop:
            atomic_json(control / "stopped.json", {"reason": requested_stop["reason"],
                        "schedule_position": position, "time": clock_record(),
                        "current_attempt_finished": True, "automatic_retry_allowed": False})
            break
        try:
            budget.check(new_run=True)
        except Exception as exc:
            atomic_json(control / "stopped.json", {"reason": type(exc).__name__,
                        "schedule_position": position, "time": clock_record()})
            break
        prompt = common_prompt(task)
        if arm == "codex_bridge": prompt += "\n\n" + hybrid_instructions()
        if not run_dir.exists():
            run_dir = prepare_run(control / "runs", task["id"], Path(repo) / task["fixture_dir"], arm, repeat,
                                  prompt, writable_files=task["solution_files"],
                                  source_metadata={"commit": task["source_commit"],
                                                   "fixture_sha256": task["fixture_sha256"],
                                                   "grader_sha256": task["grader_sha256"], "schedule_position": position})
        work = run_dir / "workspace"
        mailbox = None
        route = None
        if arm in {"codex_pure", "codex_bridge"}:
            plan = codex_plan(work, prompt, codex_model, probe, capability, reasoning_effort=reasoning_effort)
            plan.adapter = arm
            if arm == "codex_bridge":
                route = broker.route(arm, run_dir)
                state_dir = run_dir / "bridge-state"; state_dir.mkdir(mode=0o700, exist_ok=True)
                atomic_json(state_dir / "profile.json", route)
                handler_kwargs = {"workspace": work, "fixture_files": task["fixture_files"],
                    "solution_files": task["solution_files"], "control_dir": run_dir / "mailbox-control",
                    "bridge_script": Path(repo) / "skills/codex-model-bridge/scripts/bridge.py",
                    "state_dir": state_dir, "timeout_seconds": bridge_timeout, "max_repairs": 1,
                    "codex_binary": capability["binary"]}
                handler = ((mailbox_handler_factory or BridgeSubprocessHandler)(**handler_kwargs))
                mailbox = BridgeMailbox(work, run_dir / "mailbox-claims", handler, max_delegates=2,
                                        deadline=time.monotonic() + per_run_seconds)
        else:
            route = broker.route(arm, run_dir)
            plan = dsh_factory(work, run_dir / "dsh-state", route, prompt)
            if not isinstance(plan, CommandPlan) or plan.adapter != "deepseek_harness":
                raise TypeError("DSH factory must provide an audited CommandPlan")
        print(json.dumps({"event": "run_start", "position": position, "total": len(roster),
                          "task_id": task["id"], "arm": arm}, ensure_ascii=False), flush=True)
        started = time.monotonic()
        def launch_guard(plan):
            result = budget.start_run()
            if mailbox: mailbox.start()
            return result
        try:
            actual_timeout = min(per_run_seconds, budget.remaining_seconds()) if hasattr(budget, "remaining_seconds") else per_run_seconds
            result = execute_run(run_dir, plan, timeout_seconds=max(0.05, actual_timeout),
                                 authorized_model_requests=True, launch_guard=launch_guard)
        finally:
            if mailbox: atomic_json(run_dir / "mailbox-close.json", mailbox.close())
            if arm == "deepseek_harness" and dsh_cleanup:
                dsh_cleanup(run_dir / "dsh-state")
            if route is not None:
                broker.revoke(route["api_key"])
        settlement_seconds = min(8, budget.remaining_seconds()) if hasattr(budget, "remaining_seconds") else 8
        settlement = settle_accounting(broker, budget, max(0, settlement_seconds))
        atomic_json(run_dir / "accounting-settlement.json", settlement)
        if arm == "codex_bridge" and hasattr(handler, "submission_status"):
            submission = handler.submission_status()
            atomic_json(run_dir / "bridge-submission.json", submission)
            if not submission.get("matches_accepted_apply"):
                result["state"] = "failed"
                result["protocol_violations"] = ["final_solution_does_not_match_accepted_bridge_apply"]
                atomic_json(run_dir / "result.json", result)
                atomic_json(run_dir / "checkpoint.json", {"state": "failed", "updated": clock_record(),
                                                           "automatic_retry_allowed": False})
        grade = grader(run_dir, task, repo=repo, codex=capability["binary"])
        row = {"task_id": task["id"], "arm": arm, "state": result["state"], "grade": grade,
               "wall_seconds": result["wall_seconds"], "wall_with_grading_seconds": time.monotonic() - started,
               "usage": result.get("usage"), "external_ledger": str(run_dir / "external-usage.jsonl")}
        ledger_path = run_dir / "external-usage.jsonl"
        if ledger_path.exists():
            imported = import_request_ledger(ledger_path, source="trusted_api_broker")
            row["external_usage"] = {**imported, "summary": summarize_usage(imported["records"])}
        elif arm == "codex_pure":
            row["external_usage"] = {"request_count": 0, "reason": "pure_arm_no_external_model_route"}
        else:
            row["external_usage"] = {"request_count": None, "reason": "external_ledger_missing", "status": "unknown"}
        results.append(row)
        atomic_json(control / "progress.json", {"completed_attempts": len(results), "scheduled": len(roster),
                                                 "latest": row, "updated": clock_record()})
        print(json.dumps({"event": "run_end", "position": position, "task_id": task["id"], "arm": arm,
                          "state": result["state"], "passed": grade.get("passed"),
                          "wall_seconds": result["wall_seconds"]}, ensure_ascii=False), flush=True)
        if not settlement.get("settled") or settlement.get("usage_complete") is not True:
            atomic_json(control / "stopped.json", {"reason": "api_accounting_unknown_after_settlement",
                        "schedule_position": position, "time": clock_record(),
                        "automatic_retry_allowed": False})
            break
    summary = {"schema_version": 1, "measurement_context": "cli_control", "scheduled": len(roster),
               "recorded_attempts": len(results), "results": results,
               "api_estimated_cny": budget.estimate(), "budget": budget.state,
               "desktop_product_equivalent": False, "finished": clock_record()}
    atomic_json(control / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="18-run pilot; inference requires --execute")
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--codex-model", default="gpt-6.1-sol")
    parser.add_argument("--reasoning-effort", choices=("medium", "high"), default="medium")
    args = parser.parse_args()
    if not args.execute:
        parser.error("No requests sent: pass --execute only after approving the registered budget and isolation")
    from benchmarks.api_broker import APIBroker, CampaignBudget
    from benchmarks import dsh_adapter
    sys.path.insert(0, str(REPO / "skills/codex-model-bridge"))
    from runtime import config
    profile = {**config.load_profile(), "model": "deepseek-flash"}
    budget = CampaignBudget(args.control_root, max_runs=18, max_seconds=7200, max_cny=10)
    broker = APIBroker(profile, budget).start()
    def dsh_factory(work, state, route, prompt):
        argv, env = dsh_adapter.build_command(work, state, route["base_url"], api_key=route["api_key"],
                                               model=route["model"], max_tokens=8192)
        return CommandPlan(argv, env, prompt, "cli_control", "deepseek_harness",
                           {"outcome_file": str(Path(state) / "result.json"), "preflight": "verified_before_campaign"})
    try:
        run_campaign(args.control_root, budget=budget, broker=broker, dsh_factory=dsh_factory,
                     dsh_preflight=dsh_adapter.preflight, codex_model=args.codex_model,
                     reasoning_effort=args.reasoning_effort, authorized_model_requests=True,
                     dsh_cleanup=dsh_adapter.cleanup)
    finally:
        broker.close()


if __name__ == "__main__": raise SystemExit(main())
