"""Offline request accounting, crash recovery, and content-exclusion tests."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from runtime.telemetry import UsageLedger


def usage(inputs=10, outputs=4, cached=3, reasoning=2):
    return {"prompt_tokens": inputs, "completion_tokens": outputs, "total_tokens": inputs + outputs,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-telemetry-tests-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private" / "requests.jsonl"
        self.ledger = UsageLedger(self.path)

    def start(self, ledger=None):
        return (ledger or self.ledger).start("deepseek", "fixture-model", "executor")

    def finish(self, raw=None, **kwargs):
        request_id = self.start()
        self.ledger.finish(request_id, "ok", usage() if raw is None else raw, **kwargs)
        return self.ledger.summary()

    def test_empty_ledger_has_zero_actual_requests(self):
        result = self.ledger.summary()
        self.assertEqual(0, result["requests_started"])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(0, result["total_tokens"])

    def test_started_but_unfinished_request_is_unknown(self):
        self.start()
        result = self.ledger.summary()
        self.assertEqual((1, 0, 0), (result["requests_started"], result["requests_finished"], result["requests_failed"]))
        self.assertFalse(result["usage_complete"])
        self.assertEqual("unknown", result["usage_status"])
        self.assertEqual(1, result["unfinished_requests"])
        self.assertIsNone(result["total_tokens"])
        self.assertIn("unfinished_requests", result["unknown_reasons"])

    def test_cache_and_reasoning_are_subsets_not_added_to_total(self):
        result = self.finish(finish_reason="stop")
        self.assertEqual((10, 3, 4, 2, 14), tuple(result[key] for key in
                         ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")))
        self.assertTrue(result["usage_complete"])
        self.assertTrue(result["cache_detail_complete"])
        self.assertTrue(result["reasoning_detail_complete"])

    def test_deepseek_cache_hit_and_miss_are_not_extra_input(self):
        result = self.finish({"prompt_tokens": 10, "completion_tokens": 4,
                              "prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 3})
        self.assertEqual(10, result["input_tokens"])
        self.assertEqual(7, result["cached_input_tokens"])
        self.assertEqual(14, result["total_tokens"])
        self.assertIsNone(result["reasoning_output_tokens"])

    def test_responses_style_aliases_and_total_derived_when_absent(self):
        result = self.finish({"input_tokens": 7, "output_tokens": 3,
                              "input_tokens_details": {"cached_tokens": 2},
                              "output_tokens_details": {"reasoning_tokens": 1}})
        self.assertEqual(10, result["total_tokens"])
        self.assertEqual(2, result["cached_input_tokens"])
        self.assertEqual(1, result["reasoning_output_tokens"])

    def test_absent_optional_details_are_unknown_instead_of_zero(self):
        result = self.finish({"prompt_tokens": 10, "completion_tokens": 2})
        self.assertTrue(result["usage_complete"])
        self.assertEqual(12, result["total_tokens"])
        self.assertIsNone(result["cached_input_tokens"])
        self.assertIsNone(result["reasoning_output_tokens"])
        self.assertFalse(result["cache_detail_complete"])

    def test_error_without_usage_is_counted_and_not_reported_as_free(self):
        request_id = self.start()
        self.ledger.finish(request_id, "error", None, error_code="timeout")
        result = self.ledger.summary()
        self.assertEqual(1, result["requests_finished"])
        self.assertEqual(1, result["requests_failed"])
        self.assertEqual(1, result["unknown_requests"])
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["output_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertIn("missing_or_invalid_usage", result["unknown_reasons"])

    def test_error_with_recorded_usage_still_counts_billable_tokens(self):
        request_id = self.start()
        self.ledger.finish(request_id, "error", usage(), finish_reason="length", error_code="incomplete_response")
        result = self.ledger.summary()
        self.assertEqual(1, result["requests_failed"])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(14, result["total_tokens"])

    def test_one_unknown_request_nulls_whole_total_and_retains_partial_evidence(self):
        self.finish()
        self.start()
        result = self.ledger.summary()
        self.assertEqual(2, result["requests_started"])
        self.assertIsNone(result["total_tokens"])
        self.assertEqual(14, result["known_totals"]["total_tokens"])
        self.assertEqual(1, result["unknown_requests"])

    def test_restore_pending_request_without_resending_or_altering_file(self):
        request_id = self.start()
        before = self.path.read_bytes()
        restored = UsageLedger(self.path)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(1, restored.summary()["unfinished_requests"])
        restored.finish(request_id, "ok", usage())
        self.assertEqual(14, self.ledger.summary()["total_tokens"])

    def test_unknown_and_duplicate_finish_are_rejected_without_appending(self):
        request_id = self.start()
        self.ledger.finish(request_id, "ok", usage())
        before = self.path.read_bytes()
        for identifier in [request_id, "0" * 32]:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.ledger.finish(identifier, "ok", usage())
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(1, self.ledger.summary()["requests_finished"])

    def test_invalid_status_or_freeform_error_does_not_append(self):
        request_id = self.start()
        before = self.path.read_bytes()
        for kwargs in [{"status": "failed"}, {"error_code": "contains arbitrary response text"},
                       {"finish_reason": "https://private.example/path"}]:
            arguments = {"status": "error", "usage": None, **kwargs}
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.ledger.finish(request_id, **arguments)
        self.assertEqual(before, self.path.read_bytes())

    def test_start_and_finish_flush_fsync_each_event(self):
        with mock.patch("runtime.telemetry.os.fsync", wraps=os.fsync) as sync:
            request_id = self.start()
            self.assertEqual(1, len(self.path.read_text().splitlines()))
            self.ledger.finish(request_id, "ok", usage())
            self.assertEqual(2, len(self.path.read_text().splitlines()))
        self.assertEqual(2, sync.call_count)

    def test_ledger_and_immediate_parent_have_private_modes(self):
        self.assertEqual(0o700, self.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.path.chmod(0o644)
        self.path.parent.chmod(0o755)
        UsageLedger(self.path)
        self.assertEqual(0o700, self.path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)

    def test_symlink_and_hardlink_targets_are_not_written(self):
        target = Path(self.temp.name) / "target.txt"
        target.write_text("do not modify")
        link = self.path.parent / "symlink.jsonl"
        link.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            UsageLedger(link)
        hardlink = self.path.parent / "hardlink.jsonl"
        os.link(target, hardlink)
        with self.assertRaises(ValueError):
            UsageLedger(hardlink)
        self.assertEqual("do not modify", target.read_text())

    def test_only_known_numeric_usage_fields_are_persisted(self):
        secret = "TELEMETRY_PRIVATE_KEY_FIXTURE"
        raw = {**usage(), "api_key": secret, "messages": [{"content": secret}],
               "endpoint": "https://private.invalid/" + secret,
               "prompt_tokens_details": {"cached_tokens": 3, "provider_note": secret},
               "completion_tokens_details": {"reasoning_tokens": 2, "hidden_text": secret}}
        result = self.finish(raw)
        self.assertEqual(14, result["total_tokens"])
        contents = self.path.read_text()
        for forbidden in [secret, "messages", "endpoint", "api_key", "hidden_text", "provider_note"]:
            self.assertNotIn(forbidden, contents)
        events = [json.loads(line) for line in contents.splitlines()]
        self.assertEqual(usage(), events[1]["usage"])

    def test_labels_cannot_store_prompt_or_url(self):
        for kwargs in [{"provider": "https://private.invalid/v1"}, {"stage": "entire prompt text"},
                       {"model": "fixture\nPRIVATE_BODY"}, {"kind": "Bearer private-key"}]:
            arguments = {"provider": "custom", "model": "fixture-model", "stage": "executor", **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.ledger.start(**arguments)
        self.assertEqual("", self.path.read_text())

    def test_usage_with_only_total_does_not_invent_input_output_split(self):
        result = self.finish({"total_tokens": 14})
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["total_tokens"])

    def test_conflicting_aliases_total_and_impossible_subsets_are_unknown(self):
        variants = [{**usage(), "input_tokens": 11}, {**usage(), "total_tokens": 20},
                    {**usage(), "prompt_cache_hit_tokens": 5},
                    {**usage(), "prompt_tokens_details": {"cached_tokens": 11}},
                    {**usage(), "completion_tokens_details": {"reasoning_tokens": 5}}]
        for index, raw in enumerate(variants):
            with self.subTest(raw=raw):
                ledger = UsageLedger(self.path.parent / (str(index) + ".jsonl"))
                request_id = self.start(ledger)
                ledger.finish(request_id, "ok", raw)
                self.assertFalse(ledger.summary()["usage_complete"])
                self.assertIsNone(ledger.summary()["total_tokens"])

    def test_invalid_numeric_values_are_not_persisted_as_tokens(self):
        for invalid in [-1, True, 1.5, "PRIVATE_TOKEN_FIELD"]:
            with self.subTest(invalid=invalid):
                request_id = self.start()
                self.ledger.finish(request_id, "ok", {"prompt_tokens": invalid, "completion_tokens": 2})
        self.assertEqual(4, self.ledger.summary()["unknown_requests"])
        self.assertNotIn("PRIVATE_TOKEN_FIELD", self.path.read_text())

    def test_multiple_instances_do_not_lose_parallel_request_pairs(self):
        def record(index):
            ledger = UsageLedger(self.path)
            request_id = ledger.start("custom", "fixture-model", "executor")
            ledger.finish(request_id, "ok", usage(inputs=index + 10))
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(record, range(18)))
        result = self.ledger.summary()
        self.assertEqual((18, 18, 0), (result["requests_started"], result["requests_finished"], result["requests_failed"]))
        self.assertEqual(sum(index + 14 for index in range(18)), result["total_tokens"])
        self.assertEqual(36, len(self.path.read_text().splitlines()))

    def test_corrupt_ledger_is_not_silently_discarded_or_echoed(self):
        self.path.write_text('{"PRIVATE_INVALID_CONTENT":')
        with self.assertRaisesRegex(ValueError, "invalid record at line 1") as error:
            UsageLedger(self.path)
        self.assertNotIn("PRIVATE_INVALID_CONTENT", str(error.exception))
        self.assertEqual('{"PRIVATE_INVALID_CONTENT":', self.path.read_text())


if __name__ == "__main__":
    unittest.main()
