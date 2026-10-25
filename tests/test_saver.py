"""Offline saver-mode scenarios. All model calls and fixed checks are mocked."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


from runtime import engine as ds


def check_result(code=0, reason=None):
    return [{"name": "fixed acceptance", "exit_code": code, "stop_reason": reason,
             "stdout_tail": "offline fixture output", "stderr_tail": ""}]


def review_result(verdict="pass"):
    findings = [{"file": "editable.py", "line": 1, "problem": "Expected behavior is missing."}]
    return {"review": {"verdict": verdict, "summary": "offline fixture review",
                       "findings": findings if verdict == "changes_requested" else [],
                       "unverified": ["Evidence is insufficient."] if verdict == "blocked" else []},
            "usage": {"input_tokens": 10, "output_tokens": 5}, "input_chars": 100}


class SaverScenarios(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-saver-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "editable.py").write_text("original implementation\n")
        (self.root / "acceptance.py").write_text("immutable acceptance fixture\n")
        self.task = {"workspace": str(self.root), "task": "Implement a bounded low-risk change.",
                     "risk_level": "low", "input_files": ["acceptance.py"],
                     "writable_files": ["editable.py"], "verification_files": ["acceptance.py"],
                     "acceptance_checks": [{"name": "fixed acceptance", "argv": ["python3", "acceptance.py"]}]}
        self.task_path = self.base / "task.json"
        self.state = self.base / "state"
        patch = mock.patch.object(ds, "STATE", self.state)
        patch.start()
        self.addCleanup(patch.stop)
        self.events = []
        self.worker_count = 0

    def args(self, max_repairs=2):
        self.task_path.write_text(json.dumps(self.task))
        return argparse.Namespace(mixed=True, mode="saver", task_file=str(self.task_path),
                                  dry_run=False, model="deepseek-flash", review_model="deepseek-flash",
                                  max_repairs=max_repairs, timeout=300)

    def worker(self, stage, phase, model, key, prompt, timeout):
        self.worker_count += 1
        self.events.append("worker")
        (stage / "editable.py").write_text("worker revision " + str(self.worker_count) + "\n")
        return {"exit_code": 0, "stop_reason": None, "usage": []}

    def execute(self, *, checks=None, reviews=None, worker=None, max_repairs=2):
        check_values = iter(checks if checks is not None else [check_result()])
        review_values = iter(reviews if reviews is not None else [review_result()])

        def fixed_checks(*args, **kwargs):
            self.events.append("checks")
            value = next(check_values)
            if isinstance(value, BaseException):
                raise value
            return value

        def independent_review(*args, **kwargs):
            self.events.append("review")
            value = next(review_values)
            if isinstance(value, BaseException):
                raise value
            return value

        with mock.patch.object(ds, "secret", return_value="OFFLINE_FAKE_KEY"), \
             mock.patch.object(ds, "execute_saver_worker", side_effect=worker or self.worker), \
             mock.patch.object(ds, "run_fixed_checks", side_effect=fixed_checks), \
             mock.patch.object(ds, "independent_review", side_effect=independent_review):
            return ds.run_task(self.args(max_repairs))

    def assert_original_untouched(self):
        self.assertEqual("original implementation\n", (self.root / "editable.py").read_text())
        self.assertEqual("immutable acceptance fixture\n", (self.root / "acceptance.py").read_text())

    def assert_cannot_apply(self, result):
        with self.assertRaises(ValueError):
            ds.apply_run(argparse.Namespace(run_dir=result["run_dir"], desktop_reviewed=True))
        self.assert_original_untouched()

    def test_failed_check_repairs_then_rechecks_and_fresh_review(self):
        result = self.execute(checks=[check_result(1), check_result()])
        self.assertEqual(["worker", "checks", "worker", "checks", "review"], self.events)
        self.assertEqual("needs_desktop_review", result["status"])
        self.assertEqual(1, result["repair_rounds"])
        self.assertEqual(3, result["model_stages"])
        self.assertTrue(result["checks_passed"])
        self.assertTrue(result["review_passed"])
        self.assert_original_untouched()

    def test_review_requested_repair_must_repeat_checks_and_review(self):
        result = self.execute(checks=[check_result(), check_result()],
                              reviews=[review_result("changes_requested"), review_result()])
        self.assertEqual(["worker", "checks", "review", "worker", "checks", "review"], self.events)
        self.assertEqual("needs_desktop_review", result["status"])
        self.assertEqual(1, result["repair_rounds"])
        self.assertEqual(4, result["model_stages"])
        self.assertEqual(result["verified_stage_hashes"]["editable.py"],
                         ds.digest(Path(result["run_dir"]) / "workspace/editable.py"))
        self.assert_original_untouched()

    def test_exhausted_repair_budget_escalates_and_cannot_apply(self):
        result = self.execute(checks=[check_result(1)] * 3)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertEqual(2, result["repair_rounds"])
        self.assertEqual(3, result["model_stages"])
        self.assertEqual(["worker", "checks"] * 3, self.events)
        self.assertFalse(result["checks_passed"])
        self.assert_cannot_apply(result)

    def test_high_risk_and_writable_verification_rejected_before_secret(self):
        variants = [{"risk_level": "high"}, {"requires_codex_review": True},
                    {"writable_files": ["editable.py", "acceptance.py"]}]
        for values in variants:
            with self.subTest(values=values):
                original = dict(self.task)
                self.task.update(values)
                with mock.patch.object(ds, "secret") as secret, \
                     mock.patch.object(ds, "execute_saver_worker") as worker:
                    with self.assertRaises(ValueError):
                        ds.run_task(self.args())
                    secret.assert_not_called()
                    worker.assert_not_called()
                self.task = original

    def test_modified_verification_escalates_before_checks_or_review(self):
        def changed_input(stage, *rest):
            result = self.worker(stage, *rest)
            (stage / "acceptance.py").write_text("worker weakened the acceptance\n")
            return result

        result = self.execute(worker=changed_input)
        self.assertEqual(["worker"], self.events)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertTrue(result["issues"])
        self.assert_cannot_apply(result)

    def test_extra_output_escalates_without_checks_or_review(self):
        def out_of_scope(stage, *rest):
            result = self.worker(stage, *rest)
            (stage / "extra.txt").write_text("not authorized\n")
            return result

        result = self.execute(worker=out_of_scope)
        self.assertEqual(["worker"], self.events)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertTrue(any("extra.txt" in issue for issue in result["issues"]))
        self.assert_cannot_apply(result)

    def test_symlink_output_produces_escalation_summary(self):
        def symlink_output(stage, *rest):
            result = self.worker(stage, *rest)
            (stage / "editable.py").unlink()
            (stage / "editable.py").symlink_to(self.root / "editable.py")
            return result

        result = self.execute(worker=symlink_output)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertEqual(["worker"], self.events)
        self.assertIn("符号链接", result["escalation_reason"])
        self.assertTrue((Path(result["run_dir"]) / "handoff.json").exists())
        self.assert_cannot_apply(result)

    def test_invalid_review_escalates_without_repair(self):
        result = self.execute(reviews=[ValueError("DS 复审结果不符合协议")])
        self.assertEqual(["worker", "checks", "review"], self.events)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertFalse(result["review_passed"])
        self.assert_cannot_apply(result)

    def test_blocked_review_escalates_without_repair(self):
        result = self.execute(reviews=[review_result("blocked")])
        self.assertEqual(["worker", "checks", "review"], self.events)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assert_cannot_apply(result)

    def test_check_timeout_escalates_without_repair(self):
        result = self.execute(checks=[check_result(None, "timeout")])
        self.assertEqual(["worker", "checks"], self.events)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertFalse(result["checks_passed"])
        self.assert_cannot_apply(result)

    def test_api_or_worker_failure_escalates_without_checks(self):
        def failed_worker(*args):
            self.events.append("worker")
            raise OSError("offline simulated provider error")

        result = self.execute(worker=failed_worker)
        self.assertEqual(["worker"], self.events)
        self.assertEqual("needs_codex_attention", result["status"])
        self.assert_cannot_apply(result)

    def test_files_changed_during_fixed_checks_invalidate_evidence(self):
        def mutate_during_checks(stage, *rest):
            self.events.append("checks")
            (stage / "editable.py").write_text("changed while checking\n")
            return check_result()

        with mock.patch.object(ds, "secret", return_value="OFFLINE_FAKE_KEY"), \
             mock.patch.object(ds, "execute_saver_worker", side_effect=self.worker), \
             mock.patch.object(ds, "run_fixed_checks", side_effect=mutate_during_checks), \
             mock.patch.object(ds, "independent_review") as reviewer:
            result = ds.run_task(self.args())
            reviewer.assert_not_called()
        self.assertEqual("needs_codex_attention", result["status"])
        self.assert_cannot_apply(result)

    def test_apply_rejects_stage_hash_changed_after_success(self):
        result = self.execute()
        (Path(result["run_dir"]) / "workspace/editable.py").write_text("changed after verified\n")
        self.assert_cannot_apply(result)

    def test_apply_rejects_original_read_only_context_changed_after_success(self):
        result = self.execute()
        (self.root / "acceptance.py").write_text("human changed acceptance after verified\n")
        with self.assertRaises(ValueError):
            ds.apply_run(argparse.Namespace(run_dir=result["run_dir"], desktop_reviewed=True))
        self.assertEqual("original implementation\n", (self.root / "editable.py").read_text())
        self.assertEqual("human changed acceptance after verified\n", (self.root / "acceptance.py").read_text())

    def test_apply_rejects_changed_acceptance_contract(self):
        result = self.execute()
        path = Path(result["run_dir"]) / "task.json"
        contract = json.loads(path.read_text())
        contract["acceptance_checks"][0]["argv"] = ["python3", "-c", "pass"]
        ds.save_json(path, contract)
        self.assert_cannot_apply(result)

    def test_apply_requires_both_success_flags(self):
        result = self.execute()
        path = Path(result["run_dir"]) / "summary.json"
        for flag in ["checks_passed", "review_passed"]:
            with self.subTest(flag=flag):
                changed = dict(result)
                changed[flag] = False
                ds.save_json(path, changed)
                self.assert_cannot_apply(result)

    def test_handoff_is_bounded_and_omits_long_tool_logs(self):
        result = self.execute()
        long_log = "LONG_RAW_TOOL_LOG_" * 20000
        result["tool_evidence"] = [{"command": long_log}]
        result["checks"][0]["stdout_tail"] = long_log
        result["review"]["summary"] = long_log
        result["escalation_reason"] = long_log
        handoff = ds.compact_handoff(result)
        rendered = json.dumps(handoff)
        self.assertNotIn("tool_evidence", handoff)
        self.assertNotIn("stdout_tail", rendered)
        self.assertNotIn(long_log, rendered)
        self.assertLess(len(rendered), 2500)
        self.assertEqual("pending_desktop_codex", handoff["final_acceptance"])











    def fixed_check_fixture(self, *, stdout=b"", stderr=b"", markers=None):
        check = {"name": "fixed test count", "argv": ["python3", "acceptance.py"]}
        if markers is not None:
            check["success_contains"] = markers
        process_result = {"exit_code": 0, "stop_reason": None, "stdout": stdout, "stderr": stderr}
        with mock.patch.object(ds, "codex_binary", return_value="/fake/codex"), mock.patch.object(ds, "bounded_process", return_value=process_result):
            return ds.run_fixed_checks(self.root, [check], self.base / "fixed-check-evidence",
                                       ds.time.monotonic() + 20)

    def test_zero_collected_tests_rejected_despite_exit_zero(self):
        for output in [b"Ran 0 tests in 0.000s\n\nOK\n", b"no tests ran in 0.01s\n",
                       b"collected 0 items\n"]:
            with self.subTest(output=output):
                records = self.fixed_check_fixture(stderr=output)
                self.assertEqual(0, records[0]["exit_code"])
                self.assertEqual("no_tests_executed", records[0]["stop_reason"])
                self.assertFalse(ds.checks_passed(records, 1))

    def test_missing_fixed_success_marker_rejected_despite_exit_zero(self):
        records = self.fixed_check_fixture(stderr=b"Ran 7 tests in 0.01s\n\nOK\n",
                                           markers=["Ran 8 tests", "OK"])
        self.assertEqual("missing_success_evidence", records[0]["stop_reason"])
        self.assertFalse(ds.checks_passed(records, 1))

    def test_success_markers_present_across_both_streams_pass(self):
        records = self.fixed_check_fixture(stdout=b"OK\n", stderr=b"Ran 8 tests in 0.01s\n",
                                           markers=["Ran 8 tests", "OK"])
        self.assertIsNone(records[0]["stop_reason"])
        self.assertTrue(ds.checks_passed(records, 1))


if __name__ == "__main__":
    unittest.main()
