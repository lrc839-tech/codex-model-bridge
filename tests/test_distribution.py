"""Installation tests only touch temporary folders; no real Codex settings or keys."""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load_script("install")
builder = load_script("build_release")


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="bridge-distribution-test-")
        self.root = Path(self.scratch.name)
        self.source = self.root / "source-skill"
        for filename in ("SKILL.md", "scripts/bridge.py", "runtime/config.py", "ui/index.html"):
            path = self.source / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test fixture\n", encoding="utf-8")
        self.destination = self.root / "agents" / "skills" / "codex-model-bridge"
        self.codex = self.root / "codex"
        self.codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.codex.chmod(0o755)

    def tearDown(self):
        self.scratch.cleanup()

    def test_fresh_install_copies_full_skill_and_records_interpreters(self):
        result = installer.install(self.destination, self.codex, self.source)
        self.assertTrue((self.destination / "runtime/config.py").is_file())
        self.assertIsNone(result["backup"])
        metadata = json.loads((self.destination / installer.MARKER).read_text())
        self.assertEqual(metadata["codex"], str(self.codex.resolve()))
        self.assertEqual(metadata["package"], "codex-model-bridge")

    def test_upgrade_backs_up_previous_skill_outside_discovery_directory(self):
        installer.install(self.destination, self.codex, self.source)
        (self.destination / "user-added.txt").write_text("preserve me", encoding="utf-8")
        result = installer.install(self.destination, self.codex, self.source)
        backup = Path(result["backup"])
        self.assertFalse(backup.is_relative_to(self.destination.parent))
        self.assertEqual((backup / "user-added.txt").read_text(), "preserve me")
        self.assertFalse((self.destination / "user-added.txt").exists())

    def test_source_symlinks_cannot_escape_skill(self):
        (self.source / "outside").symlink_to(self.root / "codex")
        with self.assertRaises(installer.InstallError):
            installer.install(self.destination, self.codex, self.source)
        self.assertFalse(self.destination.exists())

    def test_target_symlink_is_refused(self):
        self.destination.parent.mkdir(parents=True)
        self.destination.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(installer.InstallError):
            installer.install(self.destination, self.codex, self.source)

    def test_uninstall_requires_marker_and_leaves_other_data(self):
        installer.install(self.destination, self.codex, self.source)
        state = self.root / "state.json"
        state.write_text("preserve", encoding="utf-8")
        installer.uninstall(self.destination)
        self.assertFalse(self.destination.exists())
        self.assertEqual(state.read_text(), "preserve")
        self.destination.mkdir()
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.destination)

    def test_configure_uses_installed_entry_and_saved_codex_path(self):
        installer.install(self.destination, self.codex, self.source)
        with patch.object(installer.subprocess, "call", return_value=0) as call:
            self.assertEqual(installer.configure(self.destination), 0)
        args, kwargs = call.call_args
        self.assertEqual(args[0][1:], [str(self.destination / "scripts/bridge.py"), "configure"])
        self.assertEqual(kwargs["env"]["CODEX_MODEL_BRIDGE_CODEX"], str(self.codex.resolve()))

    def test_missing_source_does_not_move_existing_skill(self):
        self.destination.mkdir(parents=True)
        sentinel = self.destination / "sentinel.txt"
        sentinel.write_text("keep", encoding="utf-8")
        (self.source / "SKILL.md").unlink()
        with self.assertRaises(installer.InstallError):
            installer.install(self.destination, self.codex, self.source)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_release_rejects_long_api_keys_and_personal_paths(self):
        fixture = self.root / "fixture.py"
        for text in ("api_key = 'sk-' + 'not-a-real-key'", "prefix=" + "'sk-" + "a" * 30 + "'", "private=" + "'/Users/" + "alice/project'"):
            fixture.write_text(text, encoding="utf-8")
            if "not-a-real-key" in text:
                builder.validate_contents(fixture, fixture.read_bytes(), self.root)
            else:
                with self.assertRaises(builder.ReleaseError):
                    builder.validate_contents(fixture, fixture.read_bytes(), self.root)

    def test_release_whitelist_excludes_installed_metadata_and_keys(self):
        package = self.root / "package"
        for name in (
            "README.md", "LICENSE", "install.command", "configure.command", "uninstall.command",
            "scripts/install.py", "scripts/mac_launcher.sh", "skills/codex-model-bridge/SKILL.md",
            "skills/codex-model-bridge/scripts/bridge.py", "skills/codex-model-bridge/runtime/config.py",
            "skills/codex-model-bridge/ui/index.html",
        ):
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("release fixture\n", encoding="utf-8")
        for name in ("credentials.json", "skills/codex-model-bridge/.bridge-install.json", "skills/codex-model-bridge/.bridge-python", "runs/private.txt", ".env"):
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE SENTINEL", encoding="utf-8")
        result = builder.build_release(package, self.root / "output")
        archive_path = Path(result["zip"])
        first_bytes = archive_path.read_bytes()
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), result["files"] + 1)
            for name in names:
                self.assertNotIn(b"PRIVATE SENTINEL", archive.read(name))
            install_name = next(name for name in names if name.endswith("/install.command"))
            self.assertEqual((archive.getinfo(install_name).external_attr >> 16) & 0o777, 0o755)
            manifest_name = next(name for name in names if name.endswith("/RELEASE-MANIFEST.json"))
            manifest = json.loads(archive.read(manifest_name))
            prefix = manifest_name.rsplit("/", 1)[0]
            for entry in manifest["files"]:
                self.assertEqual(entry["sha256"], hashlib.sha256(archive.read(prefix + "/" + entry["path"])).hexdigest())
        builder.build_release(package, self.root / "output")
        self.assertEqual(archive_path.read_bytes(), first_bytes)
        self.assertIn(hashlib.sha256(first_bytes).hexdigest(), Path(result["checksum"]).read_text())


if __name__ == "__main__":
    unittest.main()
