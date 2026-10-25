import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.runner.records import (atomic_json, canonical_hash, import_codex_jsonl,
                                       import_request_ledger, normalize_usage, summarize_usage)


class UsageTests(unittest.TestCase):
    def test_cache_and_reasoning_are_subsets(self):
        row = normalize_usage({"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 30,
                               "reasoning_output_tokens": 20}, "test", "turn")
        self.assertEqual(row["total_tokens"], 130)
        self.assertEqual(row["cached_input_tokens"], 60)

    def test_unknown_is_not_zero(self):
        summary = summarize_usage([normalize_usage(None, "test", "request")])
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["unknown_counts"]["input_tokens"], 1)

    def test_boolean_and_negative_usage_are_invalid(self):
        row = normalize_usage({"input_tokens": True, "output_tokens": -1}, "test", "request")
        self.assertEqual(row["status"], "partial")
        self.assertIsNone(row["total_tokens"])

    def test_vendor_cache_field(self):
        row = normalize_usage({"prompt_tokens": 100, "completion_tokens": 20,
                               "prompt_cache_hit_tokens": 70}, "external", "request")
        self.assertEqual(row["cached_input_tokens"], 70)
        self.assertEqual(row["total_tokens"], 120)

    def test_invalid_usage_details_do_not_crash_or_report_complete(self):
        row = normalize_usage({"input_tokens": 10, "output_tokens": 2, "input_tokens_details": "invalid"}, "test", "turn")
        self.assertEqual(row["status"], "partial")
        self.assertIsNone(row["total_tokens"])

    def test_alias_conflicts_and_impossible_subsets_rejected(self):
        for usage in ({"input_tokens": 10, "prompt_tokens": 11, "output_tokens": 2},
                      {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 11},
                      {"input_tokens": 10, "output_tokens": 2, "reasoning_output_tokens": 3},
                      {"input_tokens": 10, "output_tokens": 2, "total_tokens": 13},
                      {"input_tokens": 10, "output_tokens": 2, "prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 8}):
            row = normalize_usage(usage, "test", "turn")
            self.assertNotEqual(row["status"], "reported")
            self.assertIsNone(row["total_tokens"])
            self.assertTrue(row["validation_errors"])

    def test_missing_optional_subsets_do_not_destroy_main_totals(self):
        row = normalize_usage({"input_tokens": 10, "output_tokens": 2}, "test", "turn")
        self.assertEqual(row["status"], "reported")
        self.assertEqual(row["total_tokens"], 12)
        self.assertIsNone(row["cached_input_tokens"])
        self.assertIsNone(row["reasoning_output_tokens"])

    def test_partial_sum_is_marked_partial(self):
        known = normalize_usage({"input_tokens": 4, "output_tokens": 2}, "test", "request")
        summary = summarize_usage([known, normalize_usage(None, "test", "request")])
        self.assertEqual(summary["known_sums"]["total_tokens"], 6)
        self.assertFalse(summary["complete"])

    def test_codex_import_turns_not_requests(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text('\n'.join(json.dumps(row) for row in [
                {"type": "thread.started", "thread_id": "test-thread"},
                {"type": "turn.completed", "usage": {"input_tokens": 123, "cached_input_tokens": 42, "output_tokens": 17}},
                {"type": "turn.failed"}]) + '\ninvalid\n')
            result = import_codex_jsonl(path)
            self.assertEqual(result["records"][0]["total_tokens"], 140)
            self.assertEqual(result["records"][0]["measurement_context"], "cli_control")
            self.assertIsNone(result["request_count"])
            self.assertEqual(len(result["errors"]), 2)

    def test_missing_cli_usage_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"; path.write_text("")
            result = import_codex_jsonl(path)
            self.assertEqual(result["records"][0]["status"], "unknown")

    def test_ledger_import_excludes_secrets_and_keeps_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            rows = [{"request_id": "one", "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                     "api_key": "should-not-copy", "headers": {"Authorization": "never"}},
                    {"request_id": "two", "outcome": "transport_error", "usage": None}]
            path.write_text('\n'.join(map(json.dumps, rows)))
            result = import_request_ledger(path)
            self.assertEqual(result["request_count"], 2)
            self.assertEqual(result["unknown_usage_requests"], 1)
            self.assertNotIn("should-not-copy", json.dumps(result))

    def test_duplicate_request_id_not_double_counted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_text('{"request_id":"one"}\n{"request_id":"one"}\n')
            with self.assertRaises(ValueError):
                import_request_ledger(path)

    def test_append_only_start_finish_ledger_counts_one_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_text('\n'.join(map(json.dumps, [
                {"event": "start", "request_id": "one", "stage": "executor", "model": "test-model"},
                {"event": "finish", "request_id": "one", "status": "ok", "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
                {"event": "start", "request_id": "two", "stage": "review"}])))
            result = import_request_ledger(path)
            self.assertEqual(result["request_count"], 2)
            self.assertEqual(result["unknown_usage_requests"], 1)
            self.assertEqual(result["records"][0]["total_tokens"], 5)
            self.assertEqual(result["records"][1]["outcome"], "unfinished")

    def test_finish_without_start_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_text('{"event":"finish","request_id":"one"}\n')
            with self.assertRaises(ValueError): import_request_ledger(path)

    def test_ledger_explicit_incomplete_must_not_be_repaired_by_inference(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ledger.jsonl"
            path.write_text('\n'.join(map(json.dumps, [
                {"event": "start", "request_id": "one"},
                {"event": "finish", "request_id": "one", "status": "ok", "usage_complete": False,
                 "usage": {"prompt_tokens": 3, "completion_tokens": 2}}])))
            row = import_request_ledger(path)["records"][0]
            self.assertEqual(row["status"], "partial")
            self.assertIsNone(row["total_tokens"])
            self.assertIn("usage_marked_incomplete", row["validation_errors"])

    def test_atomic_records_restrict_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            atomic_json(path, {"x": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(canonical_hash({"b": 2, "a": 1}), canonical_hash({"a": 1, "b": 2}))


if __name__ == "__main__":
    unittest.main()
