"""Offline boundaries of the vendor-neutral execution engine.

No real profile, API credential, vendor request, or Codex subprocess is used.
These tests exercise protocol/state safety beyond the saver controller fixtures.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from runtime import engine
from runtime.providers import ProviderClient, ProviderError


FAKE_KEY = "offline-engine-test-key"
PROFILE = {"base_url": "https://offline.example.test/v1", "model": "fixture-model",
           "api_key": FAKE_KEY}


def response(message=None, finish="stop"):
    return {"choices": [{"message": message or {"role": "assistant", "content": "Done."},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2}}


def call(name="read_file", arguments=None, call_id="call-offline"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {"path": "context.txt"})}}


class EngineFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-engine-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "context.txt").write_text("read-only context\n", encoding="utf-8")
        (self.project / "editable.txt").write_text("original\n", encoding="utf-8")
        self.stage = self.base / "workspace"
        self.stage.mkdir()
        for name in ["context.txt", "editable.txt"]:
            (self.stage / name).write_bytes((self.project / name).read_bytes())
        self.task = {"workspace": str(self.project), "task": "Make one bounded text change.",
                     "input_files": ["context.txt"], "writable_files": ["editable.txt"],
                     "verification_files": ["context.txt"], "risk_level": "low",
                     "acceptance_checks": [{"name": "fixture", "argv": ["python3", "context.txt"]}]}
        self.task_file = self.base / "task.json"
        self.task_file.write_text(json.dumps(self.task), encoding="utf-8")
        self.state_patch = mock.patch.object(engine, "STATE", self.base / "state")
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def args(self, **overrides):
        values = {"mixed": True, "task_file": str(self.task_file),
                  "dry_run": False, "model": "fixture-model", "review_model": "fixture-reviewer",
                  "timeout": 60, "max_repairs": 0}
        values.update(overrides)
        return argparse.Namespace(**values)

    def worker(self, responses):
        client = mock.Mock()
        captures = []
        queue = iter(responses)

        def chat(messages, **kwargs):
            captures.append((copy.deepcopy(messages), copy.deepcopy(kwargs)))
            item = next(queue)
            if isinstance(item, BaseException):
                raise item
            return item

        client.chat.side_effect = chat
        with mock.patch.object(engine.config, "load_profile", return_value=PROFILE), \
             mock.patch.object(engine, "ProviderClient", return_value=client):
            result = engine.execute_saver_worker(self.stage, self.base / "phase", "fixture-model",
                                                  FAKE_KEY, "bounded task", 30)
        return result, captures

    def successful_run(self, worker=None):
        def execute(stage, *unused):
            (stage / "editable.txt").write_text("approved revision\n", encoding="utf-8")
            return {"exit_code": 0, "stop_reason": None, "usage": []}
        self.passing_checks = [{"name": "fixture", "exit_code": 0, "stop_reason": None}]
        approved = {"review": {"verdict": "pass", "summary": "Offline evidence checked.",
                               "findings": [], "unverified": []}, "usage": {}}
        with mock.patch.object(engine, "secret", return_value=FAKE_KEY), \
             mock.patch.object(engine, "execute_saver_worker", side_effect=worker or execute), \
             mock.patch.object(engine, "run_fixed_checks", return_value=self.passing_checks), \
             mock.patch.object(engine, "independent_review", return_value=approved):
            result = engine.run_task(self.args())
        report = json.loads((Path(result['run_dir']) / 'desktop-review.template.json').read_text())
        report['decision'] = 'accept'
        for row in report['criteria']:
            row.update(status='pass', evidence=['Offline fixture evidence: changes.diff and fixed checks.'])
        engine.save_json(Path(result['run_dir']) / 'desktop-review.json', report)
        return result


class ExplicitSelectionTests(EngineFixture):
    def test_default_pure_codex_rejects_before_profile_files_or_provider(self):
        args = self.args(mixed=False, task_file="/does/not/exist/task.json")
        with mock.patch.object(engine.config, "load_profile") as profile, \
             mock.patch.object(engine, "ProviderClient") as provider:
            with self.assertRaisesRegex(ValueError, "默认纯 Codex"):
                engine.run_task(args)
        profile.assert_not_called()
        provider.assert_not_called()
        self.assertFalse((engine.STATE / "runs").exists())

    def test_dry_run_does_not_load_profile_or_construct_provider(self):
        with mock.patch.object(engine.config, "load_profile", side_effect=AssertionError("profile read")) as profile, \
             mock.patch.object(engine, "ProviderClient") as provider:
            result = engine.run_task(self.args(dry_run=True))
            self.assertEqual("dry_run", result["status"])
            self.assertIs(False, result["provider_called"])
            self.assertEqual("original\n", (self.project / "editable.txt").read_text())
            profile.assert_not_called()
            provider.assert_not_called()

    def test_removed_mode_selector_is_rejected_before_task_or_provider(self):
        entry = Path(__file__).resolve().parents[1] / "skills/codex-model-bridge/scripts/bridge.py"
        for mode in ["strict", "saver"]:
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, str(entry), "run", "--mixed", "--mode", mode,
                                         "--task-file", "/does/not/exist/task.json"],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(2, result.returncode)
                self.assertIn("unrecognized arguments: --mode " + mode, result.stderr)
                self.assertNotIn("Traceback", result.stderr)


class ToolBoundaryTests(EngineFixture):
    def action(self, name, arguments):
        return engine.tool_action(name, arguments, self.stage,
                                  {"context.txt", "editable.txt", "new.txt"},
                                  {"editable.txt", "new.txt"}, 10)

    def test_read_and_write_require_explicit_contract_membership(self):
        (self.stage / "not-listed.txt").write_text("present but unauthorized")
        for tool, arguments in [("read_file", {"path": "not-listed.txt"}),
                                ("write_file", {"path": "not-listed.txt", "content": "x"}),
                                ("write_file", {"path": "context.txt", "content": "x"})]:
            with self.subTest(tool=tool, path=arguments["path"]), self.assertRaises(ValueError):
                self.action(tool, arguments)
        self.assertEqual("read-only context\n", (self.stage / "context.txt").read_text())
        self.assertEqual("present but unauthorized", (self.stage / "not-listed.txt").read_text())
        self.assertEqual("read-only context\n", self.action("read_file", {"path": "context.txt"})["content"])

    def test_authorized_new_utf8_file_is_written_with_private_permissions(self):
        result = self.action("write_file", {"path": "new.txt", "content": "离线数据\n"})
        self.assertTrue(result["written"])
        self.assertEqual("离线数据\n", (self.stage / "new.txt").read_text())
        self.assertEqual(0o600, (self.stage / "new.txt").stat().st_mode & 0o777)
        self.assertEqual(engine.digest(self.stage / "new.txt"), result["sha256"])

    def test_whitelisting_does_not_bypass_traversal_or_sensitive_path_checks(self):
        forbidden = ["../outside.txt", "/tmp/outside.txt", "a/../../outside.txt", "a\\file.txt",
                     ".env", "a/.env.production", ".git/config", ".codex/auth.json", "profile.json"]
        for path in forbidden:
            with self.subTest(path=path), self.assertRaises(ValueError):
                engine.tool_action("write_file", {"path": path, "content": "forbidden"},
                                   self.stage, {path}, {path}, 10)
        self.assertFalse((self.base / "outside.txt").exists())

    def test_file_and_parent_symlinks_are_refused_even_when_whitelisted(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "target.txt").write_text("external data")
        (self.stage / "link.txt").symlink_to(outside / "target.txt")
        (self.stage / "linked-dir").symlink_to(outside, target_is_directory=True)
        for path in ["link.txt", "linked-dir/target.txt"]:
            for tool in ["read_file", "write_file"]:
                with self.subTest(path=path, tool=tool), self.assertRaises(ValueError):
                    engine.tool_action(tool, {"path": path, "content": "must not write"},
                                       self.stage, {path}, {path}, 10)
        self.assertEqual("external data", (outside / "target.txt").read_text())

    def test_model_command_receives_scrubbed_environment_and_sandbox(self):
        leaked = {"OPENAI_API_KEY": FAKE_KEY, "DEEPSEEK_API_KEY": FAKE_KEY,
                  "CUSTOM_TOKEN": FAKE_KEY, "PYTHONPATH": "/private/imports", "HOME": str(self.base)}
        process_result = {"exit_code": 0, "stop_reason": None, "stdout": b"command result", "stderr": b""}
        with mock.patch.dict(os.environ, leaked), \
             mock.patch.object(engine, "codex_binary", return_value="/offline/codex"), \
             mock.patch.object(engine, "bounded_process", return_value=process_result) as process:
            result = self.action("run_command", {"argv": ["python3", "-V"], "timeout_seconds": 20})
        command, cwd, env, timeout = process.call_args.args
        self.assertEqual(["/offline/codex", "sandbox"], command[:2])
        self.assertIn("network={enabled=false}", command[command.index("-c") + 1])
        self.assertEqual(self.stage, cwd)
        self.assertEqual(10, timeout)
        self.assertFalse({"OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CUSTOM_TOKEN", "PYTHONPATH"} & env.keys())
        self.assertNotIn(FAKE_KEY, json.dumps(env))
        self.assertEqual("command result", result["stdout"])

    def test_command_rejects_shell_strings_and_invalid_timeouts_without_launch(self):
        bad = [{"argv": "python3 -V"}, {"argv": []}, {"argv": ["python3", "bad\x00arg"]},
               {"argv": ["python3"], "timeout_seconds": True},
               {"argv": ["python3"], "timeout_seconds": 31}]
        with mock.patch.object(engine, "bounded_process") as process:
            for arguments in bad:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    self.action("run_command", arguments)
        process.assert_not_called()


class WorkerProtocolTests(EngineFixture):
    def test_complete_assistant_protocol_state_replayed_but_not_persisted(self):
        assistant = {"role": "assistant", "content": None,
                     "reasoning_content": "PRIVATE_REASONING_FIXTURE",
                     "encrypted_content": "OPAQUE_VENDOR_FIXTURE", "vendor_extension": {"nonce": "fixture"},
                     "tool_calls": [call()]}
        result, captures = self.worker([response(assistant, "tool_calls"),
                                        response({"role": "assistant", "content": "Done " + FAKE_KEY})])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(assistant, captures[1][0][2])
        self.assertEqual("tool", captures[1][0][3]["role"])
        self.assertEqual("call-offline", captures[1][0][3]["tool_call_id"])
        evidence = "\n".join(path.read_text() for path in (self.base / "phase").rglob("*") if path.is_file())
        for secret in ["PRIVATE_REASONING_FIXTURE", "OPAQUE_VENDOR_FIXTURE", FAKE_KEY]:
            self.assertNotIn(secret, evidence)
        self.assertIn("[REDACTED]", evidence)

    def test_truncated_or_filtered_final_is_not_reported_as_success(self):
        for finish in ["length", "content_filter", None]:
            with self.subTest(finish=finish):
                result, _ = self.worker([response(finish=finish)])
                self.assertNotEqual(0, result["exit_code"])
                self.assertEqual("incomplete_response", result["stop_reason"])

    def test_empty_final_is_not_completion(self):
        result, _ = self.worker([response({"role": "assistant", "content": "  "})])
        self.assertEqual("incomplete_response", result["stop_reason"])

    def test_truncated_tool_call_does_not_mutate_files(self):
        assistant = {"role": "assistant", "content": None,
                     "tool_calls": [call("write_file", {"path": "editable.txt", "content": "unsafe change"})]}
        result, captures = self.worker([response(assistant, "length"), response()])
        self.assertEqual("original\n", (self.stage / "editable.txt").read_text())
        self.assertEqual("incomplete_response", result["stop_reason"])
        self.assertEqual(1, len(captures))

    def test_forbidden_tool_write_returns_error_without_touching_context(self):
        assistant = {"role": "assistant", "content": None,
                     "tool_calls": [call("write_file", {"path": "context.txt", "content": "changed"})]}
        _, captures = self.worker([response(assistant, "tool_calls"), response()])
        returned = json.loads(captures[1][0][-1]["content"])
        self.assertIn("error", returned)
        self.assertEqual("read-only context\n", (self.stage / "context.txt").read_text())


class ReviewBoundaryTests(EngineFixture):
    def test_independent_review_has_no_executor_history_or_tools(self):
        review = {"verdict": "pass", "summary": "Reviewed actual files.", "findings": [], "unverified": []}
        client = mock.Mock()
        client.chat.return_value = response({"role": "assistant", "content": json.dumps(review)})
        records = [{"name": "fixture", "exit_code": 0, "stdout_tail": "actual check result",
                    "stdout_path": "/do/not/send/local/path"}]
        with mock.patch.object(engine.config, "load_profile", return_value=PROFILE), \
             mock.patch.object(engine, "ProviderClient", return_value=client):
            result = engine.independent_review(self.task, self.stage, self.project,
                         ["context.txt", "editable.txt"], records, "separate-review-model", FAKE_KEY,
                         self.base / "review", 10)
        messages = client.chat.call_args.args[0]
        kwargs = client.chat.call_args.kwargs
        self.assertEqual(["system", "user"], [message["role"] for message in messages])
        self.assertTrue(kwargs["json_output"])
        self.assertNotIn("tools", kwargs)
        packet = json.loads(messages[1]["content"])
        self.assertEqual(self.task["task"], packet["task"])
        self.assertEqual("original\n", packet["artifacts"][1]["original"])
        self.assertNotIn("stdout_path", packet["checks"][0])
        self.assertEqual("pass", result["review"]["verdict"])
        metadata = json.loads((self.base / "review/request-metadata.json").read_text())
        self.assertFalse(metadata["executor_history_included"])
        self.assertEqual([], metadata["tools"])

    def test_review_cannot_pass_with_findings_or_unverified_items(self):
        finding = {"file": "editable.txt", "line": 1, "problem": "Expected behavior is missing."}
        variants = [{"verdict": "pass", "findings": [finding], "unverified": []},
                    {"verdict": "pass", "findings": [], "unverified": ["No evidence."]},
                    {"verdict": "changes_requested", "findings": [], "unverified": []},
                    {"verdict": "changes_requested", "findings": [finding], "unverified": ["Unverified."]}]
        for review in variants:
            with self.subTest(review=review), self.assertRaises(ValueError):
                engine.validate_review({"summary": "fixture", **review}, ["editable.txt"])

    def test_review_findings_must_locate_contract_file_and_real_line_number(self):
        for file, line in [("outside.txt", 1), ("editable.txt", 0), ("editable.txt", True)]:
            with self.subTest(file=file, line=line), self.assertRaises(ValueError):
                engine.validate_review({"verdict": "changes_requested", "summary": "fixture", "unverified": [],
                    "findings": [{"file": file, "line": line, "problem": "Missing behavior."}]}, ["editable.txt"])

    def test_nonstop_review_is_not_accepted(self):
        client = mock.Mock()
        client.chat.return_value = response({"role": "assistant", "content": json.dumps({
            "verdict": "pass", "summary": "Looks fine.", "findings": [], "unverified": []})}, "length")
        with mock.patch.object(engine.config, "load_profile", return_value=PROFILE), \
             mock.patch.object(engine, "ProviderClient", return_value=client), self.assertRaises(ValueError):
            engine.independent_review(self.task, self.stage, self.project, ["editable.txt"], [],
                                      "fixture-reviewer", FAKE_KEY, self.base / "review", 10)


class EscalationAndIntegrationTests(EngineFixture):
    def test_retry_cannot_reuse_old_review_after_worker_or_check_failure(self):
        change_review = {'review': {'verdict': 'changes_requested', 'summary': 'Check the stated boundary.',
                                   'findings': [{'file': 'editable.txt', 'line': 1, 'problem': 'A concrete concern.'}],
                                   'unverified': []}, 'usage': {}}
        for failure in ('worker_exit', 'worker_exception', 'check_timeout', 'scope_change'):
            with self.subTest(failure=failure):
                calls = []

                def execute(stage, *unused):
                    calls.append(1)
                    if len(calls) == 2:
                        if failure == 'worker_exit':
                            return {'exit_code': 1, 'stop_reason': 'timeout', 'usage': []}
                        if failure == 'worker_exception':
                            raise ProviderError('Offline request failure.')
                        if failure == 'scope_change':
                            (stage / 'outside.txt').write_text('unapproved output')
                    (stage / 'editable.txt').write_text('candidate from first attempt\n')
                    return {'exit_code': 0, 'stop_reason': None, 'usage': []}

                passing = [{'name': 'fixture', 'exit_code': 0, 'stop_reason': None}]
                checks = [passing, [{'name': 'fixture', 'exit_code': None, 'stop_reason': 'timeout'}]] if failure == 'check_timeout' else [passing]
                with mock.patch.object(engine, 'secret', return_value=FAKE_KEY), \
                     mock.patch.object(engine, 'execute_saver_worker', side_effect=execute), \
                     mock.patch.object(engine, 'run_fixed_checks', side_effect=checks), \
                     mock.patch.object(engine, 'independent_review', return_value=change_review):
                    result = engine.run_task(self.args(max_repairs=1))
                self.assertEqual('needs_codex_attention', result['status'])
                self.assertFalse(result['checks_passed'])
                self.assertNotIn('checked_stage_hashes', result)
                self.assertNotIn('review', result)
                run = Path(result['run_dir'])
                report = json.loads((run / 'desktop-review.template.json').read_text())
                report['decision'] = 'accept'
                for row in report['criteria']:
                    row.update(status='pass', evidence=['A later desktop observation.'])
                engine.save_json(run / 'desktop-review.json', report)
                with self.assertRaises(ValueError):
                    engine.apply_run(argparse.Namespace(run_dir=str(run), desktop_reviewed=True))
                self.assertEqual('original\n', (self.project / 'editable.txt').read_text())

    def test_changes_requested_can_be_adjudicated_with_new_desktop_evidence(self):
        def execute(stage, *unused):
            (stage / 'editable.txt').write_text('approved revision\n')
            return {'exit_code': 0, 'stop_reason': None, 'usage': []}
        passing = [{'name': 'fixture', 'exit_code': 0, 'stop_reason': None}]
        review = {'review': {'verdict': 'changes_requested', 'summary': 'Boundary needs checking.',
                            'findings': [{'file': 'editable.txt', 'line': 1, 'problem': 'Check this boundary.'}],
                            'unverified': []}, 'usage': {}}
        with mock.patch.object(engine, 'secret', return_value=FAKE_KEY), \
             mock.patch.object(engine, 'execute_saver_worker', side_effect=execute), \
             mock.patch.object(engine, 'run_fixed_checks', return_value=passing), \
             mock.patch.object(engine, 'independent_review', return_value=review):
            result = engine.run_task(self.args())
        run = Path(result['run_dir'])
        report = json.loads((run / 'desktop-review.template.json').read_text())
        report['decision'] = 'accept'
        for row in report['criteria']:
            row.update(status='pass', evidence=['Observed the actual checked revision and boundary behavior.'])
        report['external_review_resolution']['reason'] = 'A new desktop boundary observation resolves the concern.'
        for row in report['external_review_resolution']['items']:
            row.update(status='resolved', evidence=['Specific boundary check against editable.txt:1 passed.'])
        engine.save_json(run / 'desktop-review.json', report)
        with mock.patch.object(engine, 'run_fixed_checks', return_value=passing):
            applied = engine.apply_run(argparse.Namespace(run_dir=str(run), desktop_reviewed=True))
        self.assertEqual('integrated_pending_desktop_acceptance', applied['status'])
        self.assertEqual('approved revision\n', (self.project / 'editable.txt').read_text())

    def test_unverified_or_failed_desktop_visual_evidence_cannot_be_overridden(self):
        self.task['desktop_acceptance'] = ['Actual visual observation required.']
        self.task_file.write_text(json.dumps(self.task))
        result = self.successful_run()
        run = Path(result['run_dir'])
        report = json.loads((run / 'desktop-review.json').read_text())
        for status in ('unverified', 'fail'):
            with self.subTest(status=status):
                report['criteria'][-1]['status'] = status
                engine.save_json(run / 'desktop-review.json', report)
                with self.assertRaises(ValueError):
                    engine.apply_run(argparse.Namespace(run_dir=str(run), desktop_reviewed=True))
        self.assertEqual('original\n', (self.project / 'editable.txt').read_text())

    def test_summary_review_pass_flag_must_match_current_review(self):
        result = self.successful_run()
        result['review'] = {'verdict': 'blocked', 'summary': 'New evidence required.',
                            'findings': [], 'unverified': ['Actual boundary not observed.']}
        engine.save_json(Path(result['run_dir']) / 'summary.json', result)
        with self.assertRaisesRegex(ValueError, '初审结论与通过标记不一致'):
            engine.apply_run(argparse.Namespace(run_dir=result['run_dir'], desktop_reviewed=True))
        self.assertEqual('original\n', (self.project / 'editable.txt').read_text())

    def blocked_run(self):
        def execute(stage,*unused):
            (stage/'editable.txt').write_text('approved revision\n')
            return {'exit_code':0,'stop_reason':None,'usage':[]}
        self.passing_checks=[{'name':'fixture','exit_code':0,'stop_reason':None}]
        review={'review':{'verdict':'blocked','summary':'Needs Codex evidence.',
                          'findings':[],'unverified':['Observe the final result.']},'usage':{}}
        with mock.patch.object(engine,'secret',return_value=FAKE_KEY), \
             mock.patch.object(engine,'execute_saver_worker',side_effect=execute), \
             mock.patch.object(engine,'run_fixed_checks',return_value=self.passing_checks), \
             mock.patch.object(engine,'independent_review',return_value=review):
            return engine.run_task(self.args())

    def test_blocked_review_needs_explicit_resolutions_and_evidence(self):
        result=self.blocked_run()
        run=Path(result['run_dir'])
        report=json.loads((run/'desktop-review.template.json').read_text())
        report['decision']='accept'
        for row in report['criteria']:
            row.update(status='pass',evidence=['Actually inspected checked artifact and fixed checks.'])
        engine.save_json(run/'desktop-review.json',report)
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=str(run),desktop_reviewed=True))
        self.assertEqual('original\n',(self.project/'editable.txt').read_text())
        report['external_review_resolution']['reason']='Codex obtained the missing observation.'
        for row in report['external_review_resolution']['items']:
            row.update(status='resolved',evidence=['Observed editable.txt equals expected approved revision.'])
        engine.save_json(run/'desktop-review.json',report)
        with mock.patch.object(engine,'run_fixed_checks',return_value=self.passing_checks):
            applied=engine.apply_run(argparse.Namespace(run_dir=str(run),desktop_reviewed=True))
        self.assertEqual('integrated_pending_desktop_acceptance',applied['status'])
        self.assertEqual('approved revision\n',(self.project/'editable.txt').read_text())

    def test_resolved_report_cannot_override_failed_checks(self):
        result=self.blocked_run();run=Path(result['run_dir'])
        result['checks_passed']=False
        engine.save_json(run/'summary.json',result)
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=str(run),desktop_reviewed=True))
        self.assertEqual('original\n',(self.project/'editable.txt').read_text())

    def test_desktop_only_visual_criteria_cannot_be_omitted_at_apply(self):
        self.task['desktop_acceptance']=['Actual rendered appearance verified by Codex.']
        self.task_file.write_text(json.dumps(self.task))
        result=self.successful_run();run=Path(result['run_dir'])
        report=json.loads((run/'desktop-review.json').read_text());report['criteria'].pop()
        engine.save_json(run/'desktop-review.json',report)
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=str(run),desktop_reviewed=True))
    def test_completed_request_usage_survives_later_worker_network_failure(self):
        def client(profile, **kwargs):
            created = ProviderClient(profile, **kwargs)
            created._opener = mock.Mock()
            first = response({"role": "assistant", "content": None, "tool_calls": [call()]}, "tool_calls")
            created._opener.open.side_effect = [io.BytesIO(json.dumps(first).encode()), OSError("offline transport failure")]
            return created
        with mock.patch.object(engine.config, "load_profile", return_value=PROFILE), \
             mock.patch.object(engine, "ProviderClient", side_effect=client):
            result = engine.run_task(self.args())
        self.assertEqual("needs_codex_attention", result["status"])
        summary = result["external_usage"]
        self.assertEqual((2, 2, 1), tuple(summary[key] for key in ("requests_started", "requests_finished", "requests_failed")))
        self.assertEqual(5, summary["known_totals"]["total_tokens"])
        self.assertFalse(summary["usage_complete"])
        self.assertIsNone(summary["total_tokens"])

    def test_old_mode_record_cannot_bypass_desktop_review_gate(self):
        result = self.successful_run()
        summary_path = Path(result["run_dir"]) / "summary.json"
        result["mode"] = "strict"
        engine.save_json(summary_path, result)
        (Path(result["run_dir"]) / "desktop-review.json").unlink()
        with self.assertRaisesRegex(ValueError, "当前仅支持省额流程"):
            engine.apply_run(argparse.Namespace(run_dir=result["run_dir"], desktop_reviewed=True))
        self.assertEqual("original\n", (self.project / "editable.txt").read_text())

    def test_real_provider_exception_becomes_redacted_codex_handoff(self):
        client = mock.Mock()
        client.chat.side_effect = ProviderError("Offline failure " + FAKE_KEY)
        with mock.patch.object(engine.config, "load_profile", return_value=PROFILE), \
             mock.patch.object(engine, "ProviderClient", return_value=client):
            result = engine.run_task(self.args())
        self.assertEqual("needs_codex_attention", result["status"])
        self.assertIn("[REDACTED]", result["escalation_reason"])
        run = Path(result["run_dir"])
        for filename in ["summary.json", "handoff.json"]:
            self.assertNotIn(FAKE_KEY, (run / filename).read_text())
        self.assertEqual("original\n", (self.project / "editable.txt").read_text())

    def test_malformed_tool_protocol_still_creates_codex_handoff(self):
        malformed = [{"role": "assistant", "content": None, "tool_calls": [None]},
                     {"role": "assistant", "content": None, "tool_calls": [{
                         "id": "call-malformed", "function": {"name": "write_file", "arguments": 17}}]}]
        for message in malformed:
            with self.subTest(message=message):
                client = mock.Mock()
                client.chat.side_effect = [response(message, "tool_calls"), response()]
                with mock.patch.object(engine.config, "load_profile", return_value=PROFILE), \
                     mock.patch.object(engine, "ProviderClient", return_value=client):
                    result = engine.run_task(self.args())
                self.assertEqual("needs_codex_attention", result["status"])
                self.assertTrue((Path(result["run_dir"]) / "handoff.json").is_file())
                self.assertEqual("original\n", (self.project / "editable.txt").read_text())

    def test_apply_requires_explicit_desktop_review(self):
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir="/does/not/exist", desktop_reviewed=False))

    def test_boolean_review_flag_cannot_replace_per_criterion_evidence(self):
        result = self.successful_run()
        report_path = Path(result['run_dir']) / 'desktop-review.json'
        report = json.loads(report_path.read_text())
        report['criteria'][0]['status'] = 'unverified'
        engine.save_json(report_path, report)
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=result['run_dir'], desktop_reviewed=True))
        self.assertEqual('original\n', (self.project / 'editable.txt').read_text())

    def test_review_evidence_must_bind_contract_and_stage(self):
        result = self.successful_run()
        report_path = Path(result['run_dir']) / 'desktop-review.json'
        report = json.loads(report_path.read_text())
        report['contract_sha256'] = 'another-contract'
        engine.save_json(report_path, report)
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=result['run_dir'], desktop_reviewed=True))

    def test_saver_apply_refuses_changed_original_without_partial_write(self):
        self.task["writable_files"].append("z-second.txt")
        (self.project / "z-second.txt").write_text("second original\n")
        self.task_file.write_text(json.dumps(self.task))

        def change_both(stage, *unused):
            (stage / "editable.txt").write_text("approved first revision\n")
            (stage / "z-second.txt").write_text("approved second revision\n")
            return {"exit_code": 0, "stop_reason": None, "usage": []}

        result = self.successful_run(worker=change_both)
        (self.project / "z-second.txt").write_text("concurrent change\n")
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=result["run_dir"], desktop_reviewed=True))
        self.assertEqual("original\n", (self.project / "editable.txt").read_text())
        self.assertEqual("concurrent change\n", (self.project / "z-second.txt").read_text())

    def test_saver_apply_refuses_mutated_execution_copy(self):
        result = self.successful_run()
        (Path(result["run_dir"]) / "workspace/editable.txt").write_text("unreviewed later change\n")
        with self.assertRaises(ValueError):
            engine.apply_run(argparse.Namespace(run_dir=result["run_dir"], desktop_reviewed=True))
        self.assertEqual("original\n", (self.project / "editable.txt").read_text())

    def test_saver_success_integrates_and_keeps_original_backup(self):
        result = self.successful_run()
        with mock.patch.object(engine, "run_fixed_checks", return_value=self.passing_checks):
            applied = engine.apply_run(argparse.Namespace(run_dir=result["run_dir"], desktop_reviewed=True))
        self.assertEqual("integrated_pending_desktop_acceptance", applied["status"])
        self.assertTrue(applied["integration_checks_passed"])
        self.assertEqual("approved revision\n", (self.project / "editable.txt").read_text())
        self.assertEqual("original\n", (Path(applied["backup"]) / "editable.txt").read_text())


if __name__ == "__main__":
    unittest.main()
