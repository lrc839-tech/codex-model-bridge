#!/usr/bin/env python3
"""Fetch pinned public tasks and check the official grader; never call a model API.

The reference implementations and all official tests stay in evaluators/.
Only fixtures/ may be copied into an agent's sandbox.
"""
from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parent
COMMIT = "6105f6d9d3ef66ce3d5c8f529d2882af3d76b706"
SEED = "codex-model-bridge-pilot-2026-10-25-v1"
RAW = f"https://raw.githubusercontent.com/exercism/python/{COMMIT}/"
GROUPS = {
    "parsing": ["forth", "sgf-parsing"],
    "state-machine": ["bowling", "bank-account"],
    "reactivity-io": ["react", "paasio"],
    "constraint-search": ["word-search", "dominoes", "alphametics"],
    "tree-state": ["zipper", "satellite"],
    "optimization": ["book-store", "knapsack", "two-bucket"],
}
CONTRACTS = {
    "sgf-parsing": """Implement parse(input_string), returning an SgfTree. A node has properties mapping uppercase property names to lists of string values, and children containing its child nodes. Preserve the starter's equality behavior. Follow the SGF subset, escaping and whitespace requirements in the official instructions. The appended instructions specify the required ValueError messages.\n""",
    "bank-account": """Implement BankAccount.open(), close(), get_balance(), deposit(amount), and withdraw(amount). A fresh account is closed. Opening starts at zero, closing discards the balance, and reopening starts again at zero. Follow the appended ValueError messages exactly. This pinned exercise evaluates sequential operations only; it does not evaluate concurrency.\n""",
    "paasio": """Implement MeteredFile using subclassing and MeteredSocket using delegation, preserving the starter's method/property names and signatures. Counts start at zero. Count actual bytes transferred, not requested bytes, and count each read/write or recv/send operation, including zero-byte and partial transfers. Preserve wrapped context-manager behavior, exception propagation/suppression, flags, and file iteration/next behavior. The file wrapper's read/write byte and operation counters, and socket wrapper's recv/send counters, remain observable after operations. This is a wrapper task; no real network connection is needed.\n""",
    "word-search": """Implement Point(x, y) and WordSearch(puzzle). The puzzle is a list of equal-width row strings. Coordinates are zero-based: x is the column, y the row. search(word) returns a tuple (start_point, end_point), including both endpoint letters, or None if absent. Search horizontally, vertically and diagonally in both directions. Preserve Point equality by x/y.\n""",
    "zipper": """Implement Zipper.from_tree(tree), value(), left(), right(), up(), set_value(value), set_left(tree), set_right(tree), and to_tree(). Trees are nested dictionaries with a 'value' and optional 'left'/'right' subtrees. from_tree focuses the root. Navigation returns a zipper with retained context, or None when impossible. Setters return a zipper focused at the current node with the requested change. to_tree returns the complete tree from the root, including changes below the root. Use supplied values/subtrees as data; the starter's setter stubs omit arguments and may be corrected.\n""",
    "book-store": """Implement total(basket), where basket contains integer book identifiers 1 through 5. Return the cheapest total price as an integer in cents, using the official group discount rules (one book costs 800 cents). Empty basket costs zero. Select the grouping with the lowest total, rather than assuming the largest available group is always best.\n""",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(path: str, optional: bool = False) -> bytes | None:
    for attempt in range(3):
        try:
            request = urllib.request.Request(RAW + path, headers={"User-Agent": "codex-model-bridge-benchmark"})
            return urllib.request.urlopen(request, timeout=30).read()
        except urllib.error.HTTPError as error:
            if error.code == 404 and optional:
                return None
            if attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(path)


def immutable_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Refusing to replace changed fixture: {path}")
    else:
        path.write_bytes(data)
    path.chmod(0o444)


def tree_manifest(folder: Path) -> dict[str, str]:
    return {str(path.relative_to(folder)): sha256(path.read_bytes())
            for path in sorted(folder.rglob("*")) if path.is_file()}


def manifest_hash(manifest: dict[str, str]) -> str:
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())


def check_grader(fixture: Path, evaluator: Path, solution_files: list[str], reference: bool) -> dict:
    # The checked upstream code is not model-generated. No API credentials are
    # inherited; all checks are executed through Codex's network-denied sandbox.
    sys.path.insert(0, str(ROOT.parent / "skills" / "codex-model-bridge"))
    from runtime.engine import child_environment, sandbox_command
    with tempfile.TemporaryDirectory(prefix="bridge-grader-gate-") as temporary:
        stage = Path(temporary)
        for name in solution_files:
            source = evaluator / "reference" / name if reference else fixture / name
            shutil.copyfile(source, stage / name)
        for source in evaluator.iterdir():
            if source.is_file() and source.suffix == ".py":
                shutil.copyfile(source, stage / source.name)
        argv = [str(Path(sys.executable).resolve()), "-B", "-m", "unittest", "discover", "-s", ".", "-p", "*_test.py", "-v"]
        command = sandbox_command(stage, argv, access="read")
        result = subprocess.run(command, cwd=stage, env=child_environment(),
                                capture_output=True, text=True, timeout=60)
        output = result.stdout + result.stderr
        count_match = re.search(r"Ran (\d+) tests? in", output)
        counts = re.search(r"FAILED \(([^)]+)\)", output)
        return {"exit_code": result.returncode,
                "tests_run": int(count_match.group(1)) if count_match else None,
                "passed": result.returncode == 0 and "\nOK" in output,
                "failure_summary": counts.group(1) if counts else None,
                "output_sha256": sha256(output.encode()),
                "sandbox": "codex-permissions-read-only-network-disabled",
                "python": sys.version.split()[0]}


def prepare_one(item: tuple[str, list[str]], license_data: bytes, difficulties: dict[str, int]) -> dict:
    category, pool = item
    ranked = sorted(pool, key=lambda slug: sha256(f"{SEED}:{category}:{slug}".encode()))
    slug = ranked[0]
    source_dir = f"exercises/practice/{slug}/"
    metadata = json.loads(fetch(source_dir + ".meta/config.json"))
    files = metadata["files"]
    solution_files = files["solution"]
    if len(solution_files) != 1 or len(files["example"]) != 1:
        raise ValueError("This pilot supports exactly one official solution file per task")
    fixture = ROOT / "fixtures" / "exercism-python" / slug
    evaluator = ROOT / "evaluators" / "exercism-python" / slug
    sources = {}
    for name in solution_files:
        data = fetch(source_dir + name)
        immutable_write(fixture / name, data)
        sources[name] = sha256(data)
    instruction_parts = []
    for name in [".docs/instructions.md", ".docs/instructions.append.md"]:
        data = fetch(source_dir + name, optional=name.endswith(".append.md"))
        if data is not None:
            sources[name] = sha256(data)
            instruction_parts.append(data.decode())
    instructions = ("\n\n".join(instruction_parts) + "\n").encode()
    immutable_write(fixture / "INSTRUCTIONS.md", instructions)
    immutable_write(fixture / "INTERFACE.md", ("# Public interface clarification\n\n" + CONTRACTS[slug]).encode())
    immutable_write(fixture / "LICENSE.exercism-python", license_data)
    test_count = 0
    test_names = []
    for name in files["test"]:
        data = fetch(source_dir + name)
        immutable_write(evaluator / name, data)
        sources[name] = sha256(data)
        for node in ast.walk(ast.parse(data)):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                test_count += 1
                test_names.append(node.name)
    example_name = files["example"][0]
    data = fetch(source_dir + example_name)
    sources[example_name] = sha256(data)
    immutable_write(evaluator / "reference" / solution_files[0], data)
    immutable_write(evaluator / "LICENSE.exercism-python", license_data)
    source_manifest = json.dumps({"repository": "exercism/python", "commit": COMMIT,
                                  "source_dir": source_dir, "files": sources},
                                 ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    immutable_write(evaluator / "SOURCE.json", source_manifest)
    reference_gate = check_grader(fixture, evaluator, solution_files, reference=True)
    starter_gate = check_grader(fixture, evaluator, solution_files, reference=False)
    if not reference_gate["passed"] or starter_gate["passed"]:
        raise ValueError(f"Invalid grader gate {slug}: reference={reference_gate}, starter={starter_gate}")
    if reference_gate["tests_run"] != test_count or starter_gate["tests_run"] != test_count:
        raise ValueError(f"Missing/discovery error in grader for {slug}")
    fixture_files = tree_manifest(fixture)
    evaluator_files = tree_manifest(evaluator)
    return {
        "id": "exercism-python-" + slug,
        "slug": slug,
        "category": category,
        "upstream_difficulty": difficulties[slug],
        "fixture_dir": str(fixture.relative_to(ROOT.parent)),
        "evaluator_dir": str(evaluator.relative_to(ROOT.parent)),
        "solution_files": solution_files,
        "source_url": f"https://github.com/exercism/python/tree/{COMMIT}/{source_dir.rstrip('/')}",
        "source_commit": COMMIT,
        "selection_pool": pool,
        "selection_rank": ranked,
        "instructions_file": "INSTRUCTIONS.md",
        "interface_file": "INTERFACE.md",
        "fixture_files": fixture_files,
        "fixture_sha256": manifest_hash(fixture_files),
        "evaluator_files": evaluator_files,
        "grader_sha256": manifest_hash(evaluator_files),
        "task_prompt_sha256": sha256(instructions + b"\n" + (fixture / "INTERFACE.md").read_bytes()),
        "grading": {"argv": ["python3", "-B", "-m", "unittest", "discover", "-s", ".", "-p", "*_test.py", "-v"],
                    "pattern": "*_test.py", "total_tests": test_count, "timeout_seconds": 60,
                    "copy_files": files["test"], "reference_solution_map": {solution_files[0]: "reference/" + solution_files[0]},
                    "pass_rule": "exit_code == 0 AND expected test count AND no skipped tests",
                    "test_names": test_names},
        "public_checks": [{"name": "syntax", "argv": ["python3", "-B", "-c", "from pathlib import Path; " + "; ".join(f"compile(Path({name!r}).read_text(), {name!r}, 'exec')" for name in solution_files)]}],
        "validation": {"reference": reference_gate, "starter": starter_gate},
        "license": "MIT (preserve Exercism license notices)",
        "limitations": ["Public exercise and public tests may appear in model training data.",
                        "Withholding local tests is adapted evaluation, not the normal Exercism learning workflow.",
                        "This task evaluates bounded Python implementation, not general intelligence or visual quality."],
    }


def main() -> None:
    if not (3, 10) <= sys.version_info[:2] <= (3, 13):
        print("Note: upstream declares Python 3.10–3.13 support; this local gate is outside that range.", file=sys.stderr)
    license_data = fetch("LICENSE")
    track = json.loads(fetch("config.json"))
    difficulties = {item["slug"]: item["difficulty"] for item in track["exercises"]["practice"]}
    # Fetch independent tasks concurrently; each validates in its own temp sandbox.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda item: prepare_one(item, license_data, difficulties), GROUPS.items()))
    generated = {
        "schema_version": 1,
        "prepared_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "source downloaded; official reference/starter gates run; no model benchmark run",
        "selection": {"seed": SEED, "method": "one per preregistered category, minimum sha256(seed + ':' + category + ':' + slug)",
                      "pool": GROUPS, "task_count": 6, "arms": ["codex", "hybrid", "deepseek-harness"],
                      "repeats": 1, "scheduled_runs": 18, "no_selection_based_on_model_results": True},
        "source": {"repository": "https://github.com/exercism/python", "commit": COMMIT, "license": "MIT",
                   "supported_python": "3.10 through 3.13 per pinned upstream README",
                   "practice_exercise_count": len(track["exercises"]["practice"])},
        "tasks": results,
    }
    target = ROOT / "task-candidates.json"
    # Candidate metadata is generated; unlike upstream fixture files it changes
    # with gate runtime/version. Keep it writable for explicit refreshes.
    target.write_text(json.dumps(generated, ensure_ascii=False, indent=2) + "\n")
    for task in results:
        print(task["id"], task["grading"]["total_tests"], "reference=PASS", "starter=FAIL")
    print("Manifest:", target)


if __name__ == "__main__":
    main()
