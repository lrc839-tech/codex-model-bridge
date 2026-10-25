import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.runner.adapters import (codex_plan, dsh_plan, parse_dsh_outcome,
                                        verify_policy_attestation)
from benchmarks.runner.isolation import permission_config, require_probe
from benchmarks.runner.records import canonical_hash, sha256_file


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.binary = self.base / "fake-codex"; self.binary.write_text("offline fake binary")
        self.probe = {"passed": True, "scope": "local_command_sandbox",
                      "codex_sha256": sha256_file(self.binary),
                      "policy_sha256": canonical_hash(permission_config())}
        self.capability = {"binary": str(self.binary), "passed": True,
                           "sha256": sha256_file(self.binary),
                           "feature_names": ["memories", "apps", "plugins", "hooks", "skip_host_skill_discovery",
                                             "browser_use", "computer_use", "multi_agent", "view_image"]}

    def test_codex_plan_explicit_fresh_and_no_host_context(self):
        plan = codex_plan(self.base, "task", "model-for-test", self.probe, self.capability)
        self.assertEqual(plan.context, "cli_control")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--json"):
            self.assertIn(flag, plan.argv)
        self.assertIn("project_doc_max_bytes=0", plan.argv)
        self.assertIn('web_search="disabled"', plan.argv)
        self.assertNotIn("resume", plan.argv)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", plan.argv)
        self.assertFalse(plan.safety["desktop_product_equivalent"])

    def test_failed_probe_blocks_plan(self):
        self.probe["passed"] = False
        with self.assertRaises(RuntimeError):
            codex_plan(self.base, "task", "model", self.probe, self.capability)

    def test_changed_policy_or_binary_blocks_stale_probe(self):
        with self.assertRaises(RuntimeError): require_probe(self.probe, self.binary, [self.base])
        self.binary.write_text("changed")
        with self.assertRaises(RuntimeError): require_probe(self.probe, self.binary)

    def test_parent_api_credentials_not_inherited(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "must-not-inherit", "AWS_SECRET_ACCESS_KEY": "secret"}):
            plan = codex_plan(self.base, "task", "model", self.probe, self.capability)
        self.assertNotIn("DEEPSEEK_API_KEY", plan.environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", plan.environment)
        self.assertNotIn("secret", json.dumps(plan.public_record()))

    def test_hybrid_treatment_is_declared(self):
        plan = codex_plan(self.base, "task", "model", self.probe, self.capability,
                          hybrid_instruction="Explicit trusted Bridge instruction")
        self.assertEqual(plan.adapter, "codex_bridge")
        self.assertIn("Explicit trusted Bridge", plan.prompt)

    def test_dsh_default_workspace_write_not_accepted_as_isolation(self):
        capability = {"binary": str(self.binary), "sha256": sha256_file(self.binary), "headless_supported": True}
        with self.assertRaises(RuntimeError):
            dsh_plan(self.base, "task", self.base / "home", capability)

    def test_dsh_success_requires_final_reason_and_exit_zero(self):
        path = self.base / "events.jsonl"
        path.write_text(json.dumps({"type": "final", "reason": {"kind": "completed"}}))
        self.assertTrue(parse_dsh_outcome(path, 0)["completed"])
        self.assertFalse(parse_dsh_outcome(path, 1)["completed"])
        path.write_text(json.dumps({"type": "final", "reason": {"kind": "error"}}))
        self.assertFalse(parse_dsh_outcome(path, 0)["completed"])

    def test_dsh_missing_final_not_success(self):
        path = self.base / "events.jsonl"; path.write_text('{"type":"tool_result","status":"completed"}\n')
        self.assertFalse(parse_dsh_outcome(path, 0)["completed"])

    def test_dsh_policy_attestation_requires_retained_evidence(self):
        with self.assertRaises(RuntimeError): verify_policy_attestation({}, self.binary)


if __name__ == "__main__": unittest.main()
