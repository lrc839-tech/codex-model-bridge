"""Command construction and capability checks; no inference occurs on import/probe."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess

from .isolation import PROFILE, clean_environment, discover_codex, permission_config, require_probe
from .records import canonical_hash, sha256_file


@dataclass
class CommandPlan:
    argv: list
    environment: dict
    prompt: str
    context: str
    adapter: str
    safety: dict = field(default_factory=dict)

    def public_record(self):
        # Prompts are separately hashed; credentials must never be command arguments.
        return {"argv": self.argv, "environment_keys": sorted(self.environment),
                "prompt_sha256": canonical_hash(self.prompt), "measurement_context": self.context,
                "adapter": self.adapter, "safety": self.safety}


def probe_codex_capabilities(binary=None):
    binary = binary or discover_codex()
    help_result = subprocess.run([binary, "exec", "--help"], capture_output=True, text=True, timeout=20)
    feature_result = subprocess.run([binary, "features", "list"], capture_output=True, text=True, timeout=20)
    required = ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules")
    feature_names = {line.split()[0] for line in feature_result.stdout.splitlines() if line.split()}
    necessary_features = {"memories", "skip_host_skill_discovery", "plugins", "apps", "hooks"}
    missing = [flag for flag in required if flag not in help_result.stdout]
    missing += sorted(necessary_features - feature_names)
    return {"binary": str(Path(binary).resolve()), "sha256": sha256_file(binary),
            "passed": help_result.returncode == feature_result.returncode == 0 and not missing,
            "missing": missing, "feature_names": sorted(feature_names),
            "source": "installed_cli_help_and_features_no_model_requests"}


def codex_plan(workspace, prompt, model, isolation_probe, capability_probe,
               *, read_roots=(), hybrid_instruction=None, reasoning_effort="medium"):
    """Fresh CLI control run, authenticated via existing Pro login but no user config."""
    binary = capability_probe["binary"]
    require_probe(isolation_probe, binary, read_roots)
    if not capability_probe.get("passed") or capability_probe.get("sha256") != sha256_file(binary):
        raise RuntimeError("Installed Codex CLI lacks audited contamination controls")
    available = set(capability_probe.get("feature_names", ()))
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Explicit supported reasoning effort required")
    disabled = {"memories", "external_agent_memory_import", "apps", "plugins", "remote_plugin", "hooks",
                "multi_agent", "multi_agent_v2", "skill_search", "skill_mcp_dependency_install",
                "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
                "computer_use", "image_generation", "workspace_dependencies", "shell_snapshot",
                "login_shell_package_path", "network_proxy", "recommended_plugins", "remote_control",
                "standalone_web_search", "web_search_cached", "web_search_request", "sleep_tool",
                "view_image", "remote_plugin", "in_app_local_automation"}
    argv = [binary, "--no-daemon", "-a", "never", "exec", "--json", "--ephemeral",
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--color", "never",
            "-C", str(Path(workspace).resolve()), "-m", model,
            "-c", 'default_permissions="' + PROFILE + '"', "-c", permission_config(read_roots),
            "-c", "project_doc_max_bytes=0", "-c", "project_doc_fallback_filenames=[]",
            "-c", 'model_reasoning_effort=' + json.dumps(reasoning_effort),
            "-c", 'web_search="disabled"', "-c", 'shell_environment_policy.inherit="none"',
            "-c", 'shell_environment_policy.set=' + _toml_inline(clean_environment_for_tools()),
            "--enable", "skip_host_skill_discovery"]
    for feature in sorted(disabled & available):
        argv += ["--disable", feature]
    # Never use resume/fork, session IDs, danger-full-access, or add-dir.
    argv += ["-"]
    if hybrid_instruction:
        prompt += "\n\n" + hybrid_instruction
    return CommandPlan(argv, clean_environment(), prompt, "cli_control",
                       "codex_bridge" if hybrid_instruction else "codex_pure",
                       {"fresh_process": True, "fresh_session": True, "user_config": "ignored",
                        "host_skills": "discovery_skipped", "memory": "disabled",
                        "web": "disabled", "plugins": "disabled",
                        "read_roots": sorted(map(str, read_roots)),
                        "local_command_probe_sha256": canonical_hash(isolation_probe),
                        "desktop_product_equivalent": False})


def clean_environment_for_tools():
    env = clean_environment()
    # No HOME/USER or inference/API credentials in model-created tool subprocesses.
    return {key: value for key, value in env.items() if key in {"PATH", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE"}}


def _toml_inline(mapping):
    return "{" + ",".join(json.dumps(key) + "=" + json.dumps(value) for key, value in mapping.items()) + "}"


def probe_dsh_capabilities(binary, environment=None):
    """Run --help only. Installation is managed separately, never implicit npx downloads."""
    argv = [str(Path(binary).resolve()), "--profile", "headless", "--help"]
    process = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                             env=environment or clean_environment())
    needed = ("--json", "--permission-mode", "--approval-policy")
    missing = [flag for flag in needed if flag not in process.stdout + process.stderr]
    return {"binary": str(Path(binary).resolve()), "sha256": sha256_file(binary),
            "headless_supported": process.returncode == 0 and not missing,
            "missing": missing, "source": "installed_dsh_headless_help_no_model_requests",
            "isolation_verified": False}


def dsh_plan(workspace, prompt, dsh_home, capability_probe, *, verified_policy=None,
             model=None, base_url=None, extra_environment=None):
    """Require an independently verified DSH policy, not the permissive default preset.

    Root may provide a tested patch and broker. Attestation binds exact files/binary;
    this API intentionally does not turn a Codex command probe into a DSH guarantee.
    """
    binary = capability_probe["binary"]
    if not capability_probe.get("headless_supported") or capability_probe.get("sha256") != sha256_file(binary):
        raise RuntimeError("DSH headless capability not established")
    verify_policy_attestation(verified_policy, binary)
    home = Path(dsh_home).resolve()
    if home.exists() and any(home.iterdir()):
        raise RuntimeError("Each independent DSH run requires an empty, fresh DSH_HOME")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = [binary, "--profile", "headless", "--permission-mode", "workspace-write",
            "--approval-policy", "never", "--json"]
    for patch in verified_policy.get("patches", []):
        argv += ["--patch", patch["path"]]
    argv += verified_policy.get("extra_argv", [])
    argv += ["-"]
    env = clean_environment()
    env["DSH_HOME"] = str(home)
    # Generic endpoint routing is explicit. API keys must be brokered or passed by
    # caller only in env, never in argv/manifests; broader inheritance is forbidden.
    if model and verified_policy.get("selected_model") != model:
        raise RuntimeError("DSH model selection must be verified in the exact policy/patch; do not guess env names")
    if base_url:
        env["DEEPSEEK_BASE_URL"] = base_url
    allowed_env = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "SSL_CERT_FILE"}
    if extra_environment:
        if set(extra_environment) - allowed_env:
            raise ValueError("Unexpected DSH environment variable")
        env.update(extra_environment)
    return CommandPlan(argv, env, prompt, "cli_control", "deepseek_harness",
                       {"fresh_process": True, "fresh_session": True, "fresh_dsh_home": True,
                        "policy_attestation": verified_policy,
                        "desktop_product_equivalent": False})


def verify_policy_attestation(attestation, binary):
    required = {"outside_read_denied", "outside_write_denied", "symlink_read_denied",
                "sibling_read_denied", "network_search_denied", "memory_disabled",
                "skills_disabled", "plugins_allowlisted", "fresh_session_verified"}
    if not isinstance(attestation, dict) or set(attestation.get("passed_checks", [])) != required:
        raise RuntimeError("DSH full agent/tool isolation has not been verified; launch refused")
    if attestation.get("binary_sha256") != sha256_file(binary):
        raise RuntimeError("Stale DSH policy attestation")
    for patch in attestation.get("patches", []):
        if sha256_file(patch["path"]) != patch["sha256"]:
            raise RuntimeError("DSH policy changed after verification")
    # A raw copied boolean is not sufficient: retained verification evidence is required.
    evidence = attestation.get("evidence_path")
    if not evidence or sha256_file(evidence) != attestation.get("evidence_sha256"):
        raise RuntimeError("DSH isolation evidence absent or altered")
    if any(token in {"auto", "full-access", "--session-id", "--add-dir"}
           for token in attestation.get("extra_argv", [])):
        raise RuntimeError("Unsafe or session-reusing DSH arguments")


def parse_dsh_outcome(path, exit_code):
    final = None
    invalid = 0
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                invalid += bool(line.strip())
                continue
            if isinstance(event, dict) and event.get("type") == "final":
                final = event
    reason = (final or {}).get("reason") or {}
    return {"completed": exit_code == 0 and reason.get("kind") == "completed",
            "finish_reason": reason.get("kind"), "invalid_lines": invalid,
            "usage_status": "requires_persisted_request_ledger_not_headless_text"}
