import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.runner.control import clear_stop, request_stop, settle_accounting, snapshot_sources, stop_request
from benchmarks.runner.records import sha256_file


class TrustedControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name) / "control"

    def test_stop_does_not_change_any_run_or_start_model(self):
        self.control.mkdir()
        original = self.control / "result.json"; original.write_text('{"state":"failed"}')
        before = original.read_bytes()
        request_stop(self.control)
        self.assertEqual(stop_request(self.control)["reason"], "operator_stop_after_current")
        self.assertEqual(original.read_bytes(), before)

    def test_clear_is_explicit_keeps_history_does_not_resume(self):
        request_stop(self.control)
        result = clear_stop(self.control)
        self.assertTrue(result["cleared"])
        self.assertFalse(result["automatic_resume"])
        self.assertIsNone(stop_request(self.control))
        self.assertEqual(len(list((self.control / "stop-control-history").glob("*.json"))), 1)

    def test_invalid_control_file_fails_closed(self):
        self.control.mkdir()
        (self.control / "stop-after-current.json").write_text("not json")
        self.assertTrue(stop_request(self.control)["requested"])

    def test_symlink_control_fails_closed_without_reading(self):
        self.control.mkdir()
        (self.control / "stop-after-current.json").symlink_to(self.control / "missing-outside")
        self.assertEqual(stop_request(self.control)["reason"], "unsafe_stop_control_file")
        with self.assertRaises(ValueError): clear_stop(self.control)

    def test_settlement_only_waits_never_calls_budget_check(self):
        class Broker:
            def settle(self, timeout_seconds):
                return {"settled": True, "pending_requests": 0, "usage_complete": True}
        class Budget:
            def check(self): raise AssertionError("must not prematurely persist pending stop")
        result = settle_accounting(Broker(), Budget(), 0)
        self.assertTrue(result["settled"])

    def test_unsettled_or_incomplete_is_not_zero(self):
        class Broker:
            def settle(self, timeout_seconds):
                return {"settled": False, "pending_requests": 1, "usage_complete": False}
        result = settle_accounting(Broker(), None, 0)
        self.assertFalse(result["usage_complete"])
        self.assertEqual(result["pending_requests"], 1)

    def test_source_snapshot_retains_exact_bytes_and_refuses_replacement(self):
        repository = Path(self.temporary.name) / "repo"; repository.mkdir()
        source = repository / "module.py"; source.write_text("value = 1\n")
        expected = {"module.py": sha256_file(source)}
        snapshot = snapshot_sources(self.control, repository, expected)
        self.assertEqual((snapshot / "module.py").read_bytes(), source.read_bytes())
        source.write_text("value = 2\n")
        with self.assertRaises(RuntimeError): snapshot_sources(self.control, repository, expected)
        with self.assertRaises(RuntimeError): snapshot_sources(self.control, repository, {"module.py": sha256_file(source)})

    def test_snapshot_rejects_parent_traversal(self):
        with self.assertRaises(ValueError): snapshot_sources(self.control, self.control, {"../outside.py": "hash"})


if __name__ == "__main__": unittest.main()
