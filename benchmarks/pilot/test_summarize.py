"""Artificial fixtures test arithmetic only; these are NOT product measurements."""

import csv
from pathlib import Path
import tempfile
import unittest

import summarize as subject


def fixture(arm="codex_only", **changes):
    """Deliberately synthetic values, never an observed model run."""
    row = dict.fromkeys(subject.FIELDS, "")
    row.update(task_id="SYNTHETIC_TASK", repeat="1", arm=arm, status="pass", hidden_pass="true",
               codex_input_tokens="100", codex_cached_input_tokens="40", codex_output_tokens="20",
               codex_reasoning_output_tokens="5", ds_input_tokens="0", ds_output_tokens="0",
               human_interventions="0", usage_complete="true")
    row.update(changes)
    return row


class SummarizeTests(unittest.TestCase):
    def parse(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=subject.FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            return subject.read_runs(path)

    def summary(self, rows):
        return subject.summarize(self.parse(rows))

    def test_empty_template_has_no_claim(self):
        report = self.summary([])
        self.assertEqual(report["recorded_attempts"], 0)
        self.assertEqual(report["comparisons"], [])

    def test_subsets_not_double_counted(self):
        result = self.summary([fixture()])["arms"]["codex_only"]
        self.assertEqual(result["codex_total_tokens"], 120)

    def test_measured_savings_includes_output(self):
        report = self.summary([fixture(), fixture("codex_ds_bridge", codex_input_tokens="40", codex_cached_input_tokens="10", codex_output_tokens="20")])
        self.assertEqual(report["comparisons"][0]["codex_token_savings_fraction"], 0.5)

    def test_missing_usage_never_becomes_zero(self):
        report = self.summary([fixture(), fixture("codex_ds_bridge", codex_input_tokens="", codex_cached_input_tokens="", usage_complete="false")])
        self.assertIsNone(report["arms"]["codex_ds_bridge"]["codex_total_tokens"])
        self.assertIsNone(report["comparisons"][0]["codex_token_savings_fraction"])

    def test_flag_incomplete_blocks_claim_even_when_numbers_present(self):
        report = self.summary([fixture(), fixture("codex_ds_bridge", usage_complete="false")])
        self.assertIsNone(report["comparisons"][0]["codex_token_savings_fraction"])

    def test_missing_pair_blocks_overall_claim(self):
        report = self.summary([fixture(), fixture("codex_ds_bridge"), fixture(task_id="SYNTHETIC_EXTRA")])
        self.assertIsNone(report["comparisons"][0]["codex_token_savings_fraction"])
        self.assertEqual(len(report["comparisons"][0]["missing_in_candidate"]), 1)

    def test_failure_cost_counts_per_success(self):
        result = self.summary([fixture(), fixture(repeat="2", status="timeout", hidden_pass="false")])["arms"]["codex_only"]
        self.assertEqual(result["verified_successes"], 1)
        self.assertEqual(result["success_rate"], 0.5)
        self.assertEqual(result["codex_tokens_per_verified_success"], 240)
        self.assertEqual(result["independent_tasks"], 1)

    def test_self_reported_pass_is_insufficient(self):
        result = self.summary([fixture(hidden_pass="false")])["arms"]["codex_only"]
        self.assertEqual(result["verified_successes"], 0)
        self.assertIsNone(result["codex_tokens_per_verified_success"])

    def test_manual_rework_not_success(self):
        result = self.summary([fixture(human_interventions="1")])["arms"]["codex_only"]
        self.assertEqual(result["verified_successes"], 0)

    def test_unknown_quality_is_not_assumed(self):
        result = self.summary([fixture(hidden_pass="")])["arms"]["codex_only"]
        self.assertIsNone(result["success_rate"])
        self.assertEqual(result["unknown_quality_runs"], 1)

    def test_cost_missing_stays_null(self):
        self.assertIsNone(self.summary([fixture()])["arms"]["codex_only"]["usd_cost_total"])

    def test_duplicate_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复 run"):
            self.parse([fixture(), fixture()])

    def test_bad_subset_rejected(self):
        with self.assertRaisesRegex(ValueError, "不能大于"):
            self.parse([fixture(codex_cached_input_tokens="101")])

    def test_false_complete_rejected(self):
        with self.assertRaisesRegex(ValueError, "总输入/输出缺测"):
            self.parse([fixture(ds_output_tokens="")])

    def test_non_finite_or_negative_rejected(self):
        for value in ("nan", "inf", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse([fixture(usd_cost=value)])

    def test_markdown_renders_without_measured_data(self):
        result = subject.to_markdown(self.summary([]))
        self.assertIn("0 次完整尝试", result)
        self.assertIn("不能宣称", result)


if __name__ == "__main__":
    unittest.main()
