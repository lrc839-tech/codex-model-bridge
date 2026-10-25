import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from benchmarks.runner.adapters import CommandPlan
from benchmarks.runner.coordinator import checkpoint, execute_run, prepare_run, record_grade, tree_manifest
from benchmarks.runner.records import atomic_json


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.fixture = self.base / "fixture"; self.fixture.mkdir()
        (self.fixture / "solution.py").write_text("raise NotImplementedError\n")
        self.controls = self.base / "controls"
        self.run = prepare_run(self.controls, "task-one", self.fixture, "codex_pure", 1, "Solve this")

    def plan(self, code):
        return CommandPlan([sys.executable, "-c", code], dict(os.environ), "Solve this",
                           "offline_fake", "codex_pure", {"test_only": True})

    def execute(self, plan, **kwargs):
        return execute_run(self.run, plan, authorized_model_requests=True,
                           launch_guard=lambda plan: {"allowed": True, "test_only": True}, **kwargs)

    def test_independent_copies_have_identical_source(self):
        second = prepare_run(self.controls, "task-one", self.fixture, "deepseek_harness", 1, "Solve this")
        self.assertEqual(tree_manifest(self.run / "workspace"), tree_manifest(second / "workspace"))
        (self.run / "workspace" / "solution.py").write_text("answer-one")
        self.assertNotEqual((self.run / "workspace" / "solution.py").read_text(),
                            (second / "workspace" / "solution.py").read_text())

    def test_existing_run_not_overwritten(self):
        with self.assertRaises(RuntimeError):
            prepare_run(self.controls, "task-one", self.fixture, "codex_pure", 1, "Solve this")

    def test_symlink_fixture_rejected(self):
        (self.fixture / "link").symlink_to(self.base)
        with self.assertRaises(ValueError):
            prepare_run(self.controls, "task-two", self.fixture, "codex_pure", 1, "Solve this")

    def test_hidden_agent_config_rejected(self):
        (self.fixture / ".agents").mkdir()
        with self.assertRaises(ValueError):
            tree_manifest(self.fixture)

    def test_no_authorization_blocks_before_launch(self):
        with self.assertRaises(RuntimeError):
            execute_run(self.run, self.plan("raise RuntimeError"))
        self.assertEqual(checkpoint(self.run)["state"], "prepared")

    def test_no_guard_blocks_before_launch(self):
        with self.assertRaises(RuntimeError):
            execute_run(self.run, self.plan("print('x')"), authorized_model_requests=True)

    def test_guard_denial_preserves_prepared(self):
        with self.assertRaises(RuntimeError):
            execute_run(self.run, self.plan("print('x')"), authorized_model_requests=True,
                        launch_guard=lambda plan: {"allowed": False, "reason": "budget"})
        self.assertEqual(checkpoint(self.run)["state"], "prepared")

    def test_prompt_mismatch_blocks(self):
        plan = self.plan("print('x')"); plan.prompt = "different"
        with self.assertRaises(RuntimeError): self.execute(plan)

    def test_dirty_initial_workspace_blocks(self):
        (self.run / "workspace" / "solution.py").write_text("leaked answer")
        with self.assertRaises(RuntimeError): self.execute(self.plan("print('x')"))

    def test_uncertain_submission_never_resubmitted(self):
        atomic_json(self.run / "checkpoint.json", {"state": "running", "pid": -1})
        self.assertEqual(checkpoint(self.run)["state"], "uncertain")
        with self.assertRaises(RuntimeError): self.execute(self.plan("print('x')"))

    def test_finished_run_never_resubmitted(self):
        result = self.execute(self.plan("print('{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":3,\"output_tokens\":2}}')"))
        self.assertEqual(result["state"], "completed")
        self.assertGreater(result["wall_seconds"], 0)
        with self.assertRaises(RuntimeError): self.execute(self.plan("print('x')"))

    def test_nonzero_attempt_preserved(self):
        result = self.execute(self.plan("import sys; sys.exit(2)"))
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["usage"]["records"][0]["status"], "unknown")

    def test_empty_zero_exit_is_uncertain(self):
        self.assertEqual(self.execute(self.plan("pass"))["state"], "uncertain")

    def test_timeout_retained(self):
        result = self.execute(self.plan("import time; time.sleep(5)"), timeout_seconds=0.05)
        self.assertEqual(result["state"], "timeout")

    def test_output_limit_retained(self):
        result = self.execute(self.plan("print('x'*5000)"), output_limit_bytes=100)
        self.assertEqual(result["state"], "output_limit")

    def test_self_report_not_grade(self):
        self.execute(self.plan("pass"))
        with self.assertRaises(ValueError): record_grade(self.run, {"source": "model"}, grader_files=[])

    def test_separate_grader_records_hash(self):
        self.execute(self.plan("pass"))
        grader = self.base / "grader.py"; grader.write_text("# fixed grader")
        record = record_grade(self.run, {"source": "independent_grader", "passed": 3, "total": 5}, grader_files=[grader])
        self.assertEqual(record["grade"]["passed"], 3)
        self.assertEqual(len(record["grader_files"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
