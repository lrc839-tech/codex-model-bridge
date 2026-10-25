"""Readiness and evidence CLI. This entrypoint never initiates paid inference."""
import argparse
import json
from pathlib import Path

from .coordinator import checkpoint
from .isolation import capabilities, probe_isolation
from .records import atomic_json, import_codex_jsonl, import_request_ledger, summarize_usage


def main():
    parser = argparse.ArgumentParser(description="离线评测底座：探针、状态与用量导入；不会启动模型")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="真实无付费命令沙箱探针")
    probe.add_argument("--output", required=True)
    status = commands.add_parser("status", help="读取checkpoint；中断状态转uncertain，不重发")
    status.add_argument("run_dir")
    usage = commands.add_parser("usage", help="导入已存在的usage证据")
    usage.add_argument("path")
    usage.add_argument("--source", choices=("codex", "external"), required=True)
    args = parser.parse_args()
    if args.command == "probe":
        result = {"capabilities": capabilities(), "isolation": probe_isolation()}
        atomic_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["isolation"]["passed"] else 1
    if args.command == "status":
        result = checkpoint(args.run_dir)
    else:
        result = import_codex_jsonl(args.path) if args.source == "codex" else import_request_ledger(args.path)
        result["summary"] = summarize_usage(result["records"])
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
