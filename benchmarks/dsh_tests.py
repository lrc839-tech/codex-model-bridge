"""Security regression checks; all model replies are local fakes."""
from pathlib import Path
import os
import sys
import tempfile
import unittest

from benchmarks import dsh_adapter


class DshIsolationTests(unittest.TestCase):
    def test_only_explicit_ipv4_loopback_broker_is_accepted(self):
        for url in ["https://127.0.0.1:9000", "http://example.com:9000", "http://localhost:9000",
                    "http://user:secret@127.0.0.1:9000", "http://127.0.0.1:9000/?key=value"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                dsh_adapter._port(url)

    @unittest.skipUnless(sys.platform == "darwin" and dsh_adapter.SDK_PYTHON.is_file(), "macOS pinned DSH runtime required")
    def test_control_state_cannot_be_inside_the_candidate_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            with self.assertRaises(ValueError):
                dsh_adapter.build_command(workspace, workspace / "state", "http://127.0.0.1:9000")
            self.assertEqual({}, dsh_adapter._TOOL_SERVERS)

    @unittest.skipUnless(sys.platform == "darwin" and dsh_adapter.SDK_PYTHON.is_file(), "macOS pinned DSH runtime required")
    def test_real_official_sdk_native_tools_relay_and_kernel_isolation(self):
        original_home = os.environ.get("HOME")
        original_codex_home = os.environ.get("CODEX_HOME")
        result = dsh_adapter.preflight()
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["real_api_broker_roundtrip"])
        self.assertEqual(0, result["paid_requests"])
        self.assertEqual(2, result["mock_requests"])
        self.assertEqual(12, result["mock_usage"]["total_tokens"])
        self.assertEqual(original_home, os.environ.get("HOME"))
        self.assertEqual(original_codex_home, os.environ.get("CODEX_HOME"))
        self.assertEqual({}, dsh_adapter._TOOL_SERVERS)


if __name__ == "__main__":
    unittest.main()
