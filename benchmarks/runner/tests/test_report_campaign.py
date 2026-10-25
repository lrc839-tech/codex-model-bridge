import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.report_campaign import ARMS, build_report, combine_usage, csv_text, markdown, write_reports


class CampaignReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name)
        schedule = []
        for task in range(6):
            for arm in ARMS:
                schedule.append({"task_id": "task-" + str(task), "arm": arm, "repeat": 1})
                path = self.control / "runs" / ("task-" + str(task)) / arm / "r01"
                path.mkdir(parents=True)
                tokens = 200 if arm == "codex_pure" else 100
                result = {"state": "completed", "wall_seconds": {"codex_pure": 10, "codex_bridge": 8, "deepseek_harness": 9}[arm],
                          "usage": {"records": [{"status": "reported", "input_tokens": tokens,
                            "cached_input_tokens": tokens // 2, "output_tokens": tokens // 10,
                            "reasoning_output_tokens": 3, "total_tokens": tokens + tokens // 10}]}}
                (path / "result.json").write_text(json.dumps(result))
                grade = {"source": "independent_grader", "passed": True, "tests_passed": 12, "tests_total": 12,
                         "file_snapshot_unchanged": True, "original_grader_unchanged": True, "counters": {}, "wall_seconds": .1}
                (path / "grade.json").write_text(json.dumps({"grade": grade}))
                if arm != "codex_pure":
                    (path / "external-usage.jsonl").write_text('\n'.join(map(json.dumps, [
                        {"event": "start", "request_id": "one", "stage": arm, "model": "test"},
                        {"event": "finish", "request_id": "one", "status": "ok",
                         "usage": {"prompt_tokens": 20, "completion_tokens": 10, "prompt_cache_hit_tokens": 7}}])))
        (self.control / "preregistration.json").write_text(json.dumps({"schedule": schedule, "codex_model": "test-model",
                "reasoning_effort": "medium", "external_model": "test-external"}))
        (self.control / "summary.json").write_text(json.dumps({"budget": {"limits": {"runs": 18}, "pricing": {
                "usd_to_cny_budget_ceiling": 8,
                "usd_per_million_peak": {"input_cached": .006, "input_uncached": .3, "output": 1.2}}}}))

    def run_path(self, arm="codex_bridge", task=0):
        return self.control / "runs" / ("task-" + str(task)) / arm / "r01"

    def mutate(self, path, function):
        value = json.loads(path.read_text()); function(value); path.write_text(json.dumps(value))

    def test_complete_paired_quality_enables_savings(self):
        report = build_report(self.control)
        self.assertTrue(report["comparison"]["eligible"])
        self.assertAlmostEqual(report["comparison"]["codex_token_savings_ratio"], .5)
        self.assertAlmostEqual(report["comparison"]["wall_time_reduction_ratio"], .2)
        self.assertEqual(report["groups"]["codex_pure"]["codex_usage"]["total_tokens"], 1320)

    def test_failed_task_blocks_equal_quality_claim_but_keeps_all_cost(self):
        self.mutate(self.run_path() / "grade.json", lambda value: value["grade"].update(passed=False, tests_passed=11))
        report = build_report(self.control)
        self.assertFalse(report["comparison"]["eligible"])
        self.assertIsNone(report["comparison"]["codex_token_savings_ratio"])
        group = report["groups"]["codex_bridge"]
        self.assertEqual(group["codex_usage"]["total_tokens"], 660)
        self.assertEqual(group["codex_tokens_per_success_including_failures"], 132)
        self.assertEqual(group["seconds_per_success_including_failures"], 48 / 5)

    def test_unknown_cli_usage_not_silently_zero(self):
        self.mutate(self.run_path() / "result.json", lambda value: value.update(usage=None))
        report = build_report(self.control)
        usage = report["groups"]["codex_bridge"]["codex_usage"]
        self.assertIsNone(usage["total_tokens"])
        self.assertEqual(usage["known_lower_sums"]["total_tokens"], 550)
        self.assertFalse(report["comparison"]["eligible"])

    def test_missing_dsh_record_not_zero(self):
        path = self.run_path("deepseek_harness")
        for name in ("result.json", "grade.json", "external-usage.jsonl"): (path / name).unlink()
        (path / "checkpoint.json").write_text('{"state":"running","pid":123}')
        report = build_report(self.control)
        self.assertEqual(report["groups"]["deepseek_harness"]["unknown_or_uncertain"], 1)
        self.assertIsNone(report["groups"]["deepseek_harness"]["external_usage"]["total_tokens"])

    def test_empty_cli_record_array_is_missing(self):
        usage = combine_usage([], granularity="turn")
        self.assertFalse(usage["complete"])
        self.assertIsNone(usage["total_tokens"])

    def test_corrupt_reported_total_cannot_enable_savings(self):
        self.mutate(self.run_path() / "result.json", lambda value: value["usage"]["records"][0].update(total_tokens=1))
        self.assertFalse(build_report(self.control)["comparison"]["eligible"])

    def test_missing_optional_cache_and_reasoning_keep_primary_totals(self):
        def remove(value):
            value["usage"]["records"][0].pop("cached_input_tokens")
            value["usage"]["records"][0].pop("reasoning_output_tokens")
        self.mutate(self.run_path() / "result.json", remove)
        report = build_report(self.control)
        self.assertTrue(report["comparison"]["eligible"])
        self.assertIsNone(report["groups"]["codex_bridge"]["codex_usage"]["cached_input_tokens"])

    def test_failed_api_request_cost_kept(self):
        path = self.run_path() / "external-usage.jsonl"
        with path.open("a") as stream:
            stream.write('\n' + json.dumps({"event": "start", "request_id": "two", "stage": "repair"}))
            stream.write('\n' + json.dumps({"event": "finish", "request_id": "two", "status": "error",
                                          "usage": {"prompt_tokens": 5, "completion_tokens": 2}}))
        report = build_report(self.control)
        group = report["groups"]["codex_bridge"]
        self.assertEqual(group["external_usage"]["request_count"], 7)
        self.assertEqual(group["external_usage"]["total_tokens"], 187)

    def test_pending_api_request_retains_unknown(self):
        path = self.run_path() / "external-usage.jsonl"
        with path.open("a") as stream: stream.write('\n' + json.dumps({"event": "start", "request_id": "two"}))
        group = build_report(self.control)["groups"]["codex_bridge"]
        self.assertIsNone(group["external_usage"]["total_tokens"])
        self.assertIsNone(group["api_estimated_cny_upper_bound"])
        self.assertEqual(group["external_usage"]["known_lower_sums"]["total_tokens"], 180)

    def test_not_started_has_zero_consumption_without_zero_quality(self):
        path = self.run_path("deepseek_harness")
        for name in ("result.json", "grade.json", "external-usage.jsonl"): (path / name).unlink()
        (path / "checkpoint.json").write_text('{"state":"prepared"}')
        report = build_report(self.control)
        row = next(row for row in report["rows"] if row["task_id"] == "task-0" and row["arm"] == "deepseek_harness")
        self.assertEqual(row["state"], "not_started")
        self.assertEqual(row["external_usage"]["total_tokens"], 0)
        self.assertEqual(row["api_estimated_cny_upper_bound"], 0)
        self.assertIsNone(row["grader_passed"])

    def test_ledger_evidence_overrides_false_not_started_assumption(self):
        path = self.run_path("deepseek_harness")
        (path / "result.json").unlink(); (path / "grade.json").unlink()
        report = build_report(self.control)
        row = next(row for row in report["rows"] if row["task_id"] == "task-0" and row["arm"] == "deepseek_harness")
        self.assertEqual(row["state"], "uncertain")
        self.assertEqual(row["external_usage"]["total_tokens"], 30)

    def test_explicit_incomplete_broker_usage_remains_unknown_in_report(self):
        path = self.run_path() / "external-usage.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[1]["usage_complete"] = False
        path.write_text('\n'.join(map(json.dumps, rows)))
        group = build_report(self.control)["groups"]["codex_bridge"]
        self.assertFalse(group["external_usage"]["complete"])
        self.assertIsNone(group["external_usage"]["total_tokens"])
        self.assertIsNone(group["api_estimated_cny_upper_bound"])

    def test_timeout_time_and_cost_are_kept(self):
        self.mutate(self.run_path() / "result.json", lambda value: value.update(state="timeout", wall_seconds=60))
        group = build_report(self.control)["groups"]["codex_bridge"]
        self.assertEqual(group["timeouts"], 1)
        self.assertEqual(group["all_attempts_wall_seconds"], 100)
        self.assertEqual(group["successes"], 5)

    def test_model_self_report_never_grade(self):
        self.mutate(self.run_path() / "grade.json", lambda value: value["grade"].update(source="model_self_report"))
        self.assertFalse(build_report(self.control)["comparison"]["eligible"])

    def test_no_prompt_answer_headers_or_credentials_copied(self):
        secret = "DO_NOT_COPY_PRIVATE_SENTINEL"
        self.mutate(self.run_path() / "result.json", lambda value: value.update(prompt=secret, answer=secret, api_key=secret))
        (self.run_path() / "events.jsonl").write_text(secret)
        report = build_report(self.control)
        combined = json.dumps(report) + markdown(report) + csv_text(report)
        self.assertNotIn(secret, combined)
        self.assertIn("不是桌面 GUI", combined)

    def test_generate_three_shareable_reports(self):
        output = write_reports(self.control)
        self.assertTrue((output / "评测报告.md").is_file())
        self.assertTrue((output / "逐任务数据.csv").is_file())
        self.assertTrue((output / "评测汇总.json").is_file())

    def test_banner_distinguishes_planned_six_from_actual_one(self):
        for task in range(1, 6):
            for arm in ARMS:
                path = self.run_path(arm, task)
                for name in ("result.json", "grade.json", "external-usage.jsonl"):
                    (path / name).unlink(missing_ok=True)
        report = build_report(self.control)
        self.assertEqual(report["planned_task_count"], 6)
        self.assertEqual(report["actual_task_count"], 1)
        self.assertEqual(report["actual_run_count"], 3)
        self.assertEqual(report["not_started_run_count"], 15)
        self.assertIn("实际 1 题、3 次运行，15 次未开始", markdown(report))
        self.assertNotIn("六个公开 Python 任务各一次", markdown(report))

    def test_pure_external_requests_and_cost_are_known_zero(self):
        report = build_report(self.control)
        row = next(row for row in report["rows"] if row["arm"] == "codex_pure")
        self.assertEqual(row["external_usage"]["request_count"], 0)
        self.assertEqual(row["api_estimated_cny_upper_bound"], 0)
        self.assertIsNone(row["external_usage"]["total_tokens"])
        self.assertEqual(report["groups"]["codex_pure"]["api_estimated_cny_upper_bound"], 0)

    def test_posthoc_pass_does_not_upgrade_formal_failure(self):
        self.mutate(self.run_path() / "grade.json", lambda value: value["grade"].update(passed=False, tests_passed=0))
        folder = self.control / "control"; folder.mkdir()
        (folder / "posthoc-bridge-stage-grades.json").write_text(json.dumps({
            "kind": "post_hoc_unsubmitted_stage_diagnostic", "api_requests": 0,
            "records": [{"delegate": "delegate-001", "tests_passed": 12, "tests_total": 12,
                         "stage_unchanged": True, "official_grader_unchanged": True, "original_B_score_changed": False}]}))
        report = build_report(self.control)
        self.assertFalse(report["comparison"]["eligible"])
        self.assertEqual(report["groups"]["codex_bridge"]["successes"], 5)
        self.assertTrue(report["post_hoc_diagnostic"]["formal_score_unchanged"])
        self.assertIn("不计正式成绩", markdown(report))


if __name__ == "__main__": unittest.main()
