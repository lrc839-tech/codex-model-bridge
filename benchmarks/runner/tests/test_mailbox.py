import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import sys
import shutil

from benchmarks.runner.mailbox import BridgeMailbox, BridgeSubprocessHandler, validate_request
from benchmarks.runner.records import atomic_json, sha256_file


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.work = self.base / "work"; self.work.mkdir()
        self.control = self.base / "control"
        self.request = {"task": "Implement a correct pure Python solution", "acceptance": ["returns the required value"],
                        "smoke_test": "assert 2 + 3 == 5"}

    def put(self, value=None, index=1):
        path = self.work / f".bridge-request-{index:03d}.json"
        path.write_text(json.dumps(self.request if value is None else value))
        os.utime(path, (time.time() - 2, time.time() - 2))
        return path

    def test_model_cannot_select_paths_or_argv(self):
        for key in ("workspace", "input_files", "writable_files", "argv", "base_url", "api_key"):
            with self.assertRaises(ValueError): validate_request({**self.request, key: "anything"})

    def test_invalid_smoke_code_rejected_before_launch(self):
        with self.assertRaises(SyntaxError): validate_request({**self.request, "smoke_test": "if !!!"})

    def test_request_claimed_once(self):
        calls = []
        box = BridgeMailbox(self.work, self.control, lambda request, index: calls.append(index) or {"status": "ok"})
        self.put(); box.scan_once(); box.scan_once()
        self.assertEqual(calls, [1])
        self.assertEqual(json.loads((self.work / ".bridge-response-001.json").read_text())["status"], "ok")

    def test_uncertain_claim_never_replayed(self):
        calls = []
        box = BridgeMailbox(self.work, self.control, lambda request, index: calls.append(index) or {})
        atomic_json(self.control / "request-001.json", {"status": "started"})
        self.put(); box.scan_once()
        self.assertEqual(calls, [])

    def test_max_two_delegations(self):
        calls = []
        box = BridgeMailbox(self.work, self.control, lambda request, index: calls.append(index) or {})
        for index in (1, 2, 3): self.put(index=index)
        box.scan_once()
        self.assertEqual(calls, [1, 2])
        response = json.loads((self.work / ".bridge-response-003.json").read_text())
        self.assertEqual(response["reason"], "delegation_or_time_limit")

    def test_deadline_rejects_new_delegation(self):
        box = BridgeMailbox(self.work, self.control, lambda request, index: self.fail("must not launch"), deadline=0)
        self.put(); box.scan_once()
        self.assertEqual(json.loads((self.work / ".bridge-response-001.json").read_text())["status"], "rejected")

    def test_symlink_request_never_read(self):
        outside = self.base / "secret.json"; outside.write_text(json.dumps(self.request))
        (self.work / ".bridge-request-001.json").symlink_to(outside)
        box = BridgeMailbox(self.work, self.control, lambda request, index: self.fail("must not read link"))
        box.scan_once()
        self.assertEqual(json.loads((self.work / ".bridge-response-001.json").read_text())["reason"], "unsafe_request_file")

    def test_control_directory_not_visible_to_candidate(self):
        with self.assertRaises(ValueError): BridgeMailbox(self.work, self.work / "controls", lambda request, index: {})

    def test_handler_failure_keeps_unknown_not_zero(self):
        def failure(request, index): raise OSError("not exposed")
        box = BridgeMailbox(self.work, self.control, failure)
        self.put(); box.scan_once()
        response = json.loads((self.work / ".bridge-response-001.json").read_text())
        self.assertEqual(response["usage_status"], "unknown")
        self.assertNotIn("not exposed", json.dumps(response))

    def test_review_mailbox_is_single_claim_and_does_not_add_delegate(self):
        class Handler:
            def __init__(self): self.reviews = 0
            def arbitrate(self, index, report):
                self.reviews += 1
                return {"status": "integrated_after_codex_review", "model_requests": 0}
        handler = Handler()
        box = BridgeMailbox(self.work, self.control, handler)
        path = self.work / ".bridge-review-001.json"
        path.write_text('{"decision":"accept"}')
        os.utime(path, (time.time()-2, time.time()-2))
        box.scan_once(); box.scan_once()
        self.assertEqual(handler.reviews, 1)
        self.assertEqual(len(list(self.control.glob("request-*.json"))), 0)
        self.assertEqual(json.loads((self.work / ".bridge-receipt-001.json").read_text())["model_requests"], 0)


class SubprocessHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.work = self.base / "work"; self.work.mkdir()
        (self.work / "solution.py").write_text("raise NotImplementedError\n")
        (self.work / "INSTRUCTIONS.md").write_text("Fixed task requirements")
        self.state = self.base / "state"; self.state.mkdir()
        self.script = self.base / "fake_bridge.py"
        self.script.write_text("""import json, pathlib, sys, os
state = pathlib.Path(os.environ['CODEX_MODEL_BRIDGE_STATE'])
with (state/'actions.txt').open('a') as log: log.write(sys.argv[1]+'\\n')
if sys.argv[1] == 'apply':
 run=pathlib.Path(sys.argv[sys.argv.index('--run-dir')+1])
 report=json.loads(pathlib.Path(sys.argv[sys.argv.index('--review-file')+1]).read_text())
 if report.get('decision')!='accept':
  print(json.dumps({'status':'error'}));sys.exit(2)
 task=json.loads((run/'task.json').read_text())
 (pathlib.Path(task['workspace'])/'solution.py').write_bytes((run/'workspace/solution.py').read_bytes())
 print(json.dumps({'status':'integrated_pending_desktop_acceptance','integration_checks_passed':True}));sys.exit(0)
task = json.loads(pathlib.Path(sys.argv[sys.argv.index('--task-file')+1]).read_text())
run=state/'runs'/pathlib.Path(task['workspace']).parent.name
stage=run/'workspace';stage.mkdir(parents=True)
(stage/'solution.py').write_text('def answer(): return 5\\n')
(run/'task.json').write_text(json.dumps(task))
(run/'desktop-review.template.json').write_text(json.dumps({'decision':'pending','contract_sha256':'fixed',
 'stage_hashes':{},'criteria':[{'criterion':'returns five','status':'unverified','evidence':[]}],
 'external_review_resolution':None}))
print(json.dumps({'status':'needs_desktop_review','run_dir':str(run),'checks_passed':True,
 'execution_passed':True,'issues':[],'checked_stage_hashes':{'solution.py':'fixture'},
 'review_passed':True,'checks':[],'review':{'verdict':'pass'},'external_usage':{'input_tokens':1}}))
""")

    def handler(self):
        return BridgeSubprocessHandler(self.work, ["solution.py", "INSTRUCTIONS.md"], ["solution.py"],
                                       self.base / "control", self.script, self.state)

    def test_trusted_task_binds_paths_and_readonly_smoke(self):
        handler = self.handler()
        response = handler({"task": "Implement answer returning five", "acceptance": ["returns five"],
                            "smoke_test": "from solution import answer\nassert answer() == 5"}, 1)
        task = json.loads((self.base / "control/delegate-001/task.json").read_text())
        self.assertIn("benchmark_smoke.py", task["input_files"])
        self.assertNotIn("benchmark_smoke.py", task["writable_files"])
        self.assertEqual(response["status"], "awaiting_codex_review")
        self.assertEqual(response["integration"], "unsubmitted_preview_only")
        self.assertEqual(response["copied_files"], [])
        self.assertEqual((self.work / "solution.py").read_text(), "raise NotImplementedError\n")
        self.assertIn("return 5", (self.work / ".bridge-candidate-001/solution.py").read_text())
        self.assertNotIn(str(self.state), json.dumps(response))

    def test_structured_review_triggers_local_apply_and_copies_only_solution(self):
        handler = self.handler()
        handler({"task": "Implement answer returning five", "acceptance": ["returns five"], "smoke_test": ""}, 1)
        report = json.loads((self.work / ".bridge-candidate-001/desktop-review.template.json").read_text())
        report["decision"] = "accept"
        receipt = handler.arbitrate(1, report)
        self.assertEqual(receipt["status"], "integrated_after_codex_review", (handler.control / "delegate-001/apply-stdout.json").read_text())
        self.assertEqual(receipt["model_requests"], 0)
        self.assertTrue(handler.submission_status()["matches_accepted_apply"])
        self.assertEqual((self.state / "actions.txt").read_text().splitlines(), ["run", "apply"])
        self.assertEqual((self.work / "INSTRUCTIONS.md").read_text(), "Fixed task requirements")

    def test_preview_edits_reject_arbitration_without_apply(self):
        handler = self.handler()
        handler({"task": "Implement answer returning five", "acceptance": ["returns five"], "smoke_test": ""}, 1)
        (self.work / ".bridge-candidate-001/solution.py").write_text("changed preview")
        with self.assertRaises(ValueError): handler.arbitrate(1, {"decision": "accept"})
        self.assertEqual((self.state / "actions.txt").read_text().splitlines(), ["run"])

    def test_codex_direct_main_solution_edits_do_not_count_as_external_delivery(self):
        handler = self.handler()
        (self.work / "solution.py").write_text("def answer(): return 5\n")
        self.assertFalse(handler.submission_status()["matches_accepted_apply"])

    def test_real_product_apply_resolves_blocked_review_without_api_profile(self):
        from benchmarks.runner.isolation import discover_codex
        try: codex = discover_codex()
        except RuntimeError: self.skipTest("Local Codex sandbox required for offline apply integration")
        repo = Path(__file__).resolve().parents[3]
        sys.path.insert(0, str(repo / "skills/codex-model-bridge"))
        from runtime import engine
        handler = BridgeSubprocessHandler(self.work, ["solution.py", "INSTRUCTIONS.md"], ["solution.py"],
                                         self.base / "real-control", repo / "skills/codex-model-bridge/scripts/bridge.py",
                                         self.state, codex_binary=codex)
        phase = handler.control / "delegate-001"; source = phase / "input"; source.mkdir(parents=True)
        for name in handler.fixture_files: shutil.copyfile(self.work / name, source / name)
        (source / "benchmark_smoke.py").write_text("from solution import answer\nassert answer()==5\nprint('BRIDGE_SMOKE_OK')\n")
        files = [*handler.fixture_files, "benchmark_smoke.py"]
        run = self.state / "runs" / "offline-checked-candidate"; stage = run / "workspace"; stage.mkdir(parents=True)
        baseline = run / "baseline"; baseline.mkdir()
        before = {}
        for name in files:
            shutil.copyfile(source / name, stage / name); shutil.copyfile(source / name, baseline / name)
            before[name] = {"sha256": sha256_file(source / name)}
        (stage / "solution.py").write_text("def answer(): return 5\n")
        checks = [{"name": "fixed_smoke", "argv": [str(Path(sys.executable).resolve()), "-B", "benchmark_smoke.py"],
                   "timeout_seconds": 10, "success_contains": ["BRIDGE_SMOKE_OK"]}]
        task = {"workspace": str(source), "task": "Implement answer returning five", "risk_level": "low",
                "input_files": files, "writable_files": ["solution.py"], "verification_files": ["benchmark_smoke.py"],
                "acceptance": ["returns five"], "acceptance_checks": checks}
        atomic_json(run / "task.json", task); atomic_json(run / "before.json", before)
        actual_checks = engine.run_fixed_checks(stage, checks, run / "prechecks", time.monotonic()+20)
        self.assertTrue(engine.checks_passed(actual_checks, 1))
        changes, issues, _ = engine.collect_changes(baseline, stage, files, {"solution.py"}, before)
        self.assertFalse(issues)
        output = {"status": "needs_codex_attention", "mode": "saver", "run_dir": str(run), "workspace": str(source),
                  "execution_passed": True, "issues": [], "checks": actual_checks,
                  "checks_passed": True, "review_passed": False, "changes": changes,
                  "checked_stage_hashes": engine.snapshot(stage, files),
                  "acceptance_contract_sha256": sha256_file(run / "task.json"),
                  "review": {"verdict": "blocked", "summary": "Offline protocol issue fixture", "findings": [], "unverified": ["Review protocol format issue"]}}
        atomic_json(run / "summary.json", output)
        template = {"decision": "pending", "contract_sha256": output["acceptance_contract_sha256"],
                    "stage_hashes": output["checked_stage_hashes"],
                    "criteria": [{"criterion": "returns five", "status": "unverified", "evidence": []}],
                    "external_review_resolution": engine.review_resolution_template(output)}
        atomic_json(run / "desktop-review.template.json", template)
        atomic_json(phase / "controller-result.json", output)
        atomic_json(phase / "candidate-before.json", {name: sha256_file(self.work/name) for name in handler.fixture_files})
        handler.expose_candidate(output, 1)
        template["decision"] = "accept"; template["criteria"][0].update(status="pass", evidence=["Isolated fixed smoke BRIDGE_SMOKE_OK"])
        template["external_review_resolution"]["reason"] = "Local isolated smoke and code evidence resolve the protocol-only issue"
        template["external_review_resolution"]["items"][0].update(status="resolved", evidence=["Candidate local smoke passed; no requirement left unverified"])
        self.assertFalse((self.state / "profile.json").exists())
        receipt = handler.arbitrate(1, template)
        self.assertEqual(receipt["status"], "integrated_after_codex_review", (phase / "apply-stdout.json").read_text())
        self.assertEqual(receipt["model_requests"], 0)
        self.assertTrue(handler.submission_status()["matches_accepted_apply"])

    def test_codex_cannot_rewrite_original_specification(self):
        handler = self.handler()
        (self.work / "INSTRUCTIONS.md").write_text("easy answer instead")
        with self.assertRaises(ValueError):
            handler({"task": "Implement answer returning five", "acceptance": ["returns five"], "smoke_test": ""}, 1)


if __name__ == "__main__": unittest.main()
