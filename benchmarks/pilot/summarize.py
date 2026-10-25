#!/usr/bin/env python3
"""Summarize measured paired runs; never infer missing usage or call a model."""

import argparse
import csv
import json
import math
from pathlib import Path
import sys


ARMS = ("codex_only", "codex_ds_bridge", "dsh_ds", "ds_only_same_bridge")
STATUSES = ("pass", "fail", "timeout", "blocked")
TOKEN_FIELDS = (
    "codex_input_tokens", "codex_cached_input_tokens", "codex_output_tokens",
    "codex_reasoning_output_tokens", "ds_input_tokens", "ds_cached_input_tokens",
    "ds_output_tokens",
)
FIELDS = (
    "task_id", "repeat", "arm", "status", "hidden_pass", *TOKEN_FIELDS,
    "usd_cost", "wall_seconds", "human_interventions", "usage_complete",
)
TOTAL_FIELDS = ("codex_input_tokens", "codex_output_tokens", "ds_input_tokens", "ds_output_tokens")


def _boolean(value, field, line):
    value = (value or "").strip().lower()
    if not value:
        return None
    if value not in ("true", "false"):
        raise ValueError("第 %s 行 %s 应为 true/false 或空" % (line, field))
    return value == "true"


def _number(value, field, line, integer=False):
    value = (value or "").strip()
    if not value:
        return None
    try:
        result = int(value) if integer else float(value)
    except ValueError:
        raise ValueError("第 %s 行 %s 不是有效数字" % (line, field))
    if result < 0 or not math.isfinite(result):
        raise ValueError("第 %s 行 %s 必须是非负有限数字" % (line, field))
    return result


def read_runs(path):
    rows, seen = [], set()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV 缺少表头或有重复列名")
        missing = set(FIELDS) - set(reader.fieldnames)
        if missing:
            raise ValueError("CSV 缺少列: " + ", ".join(sorted(missing)))
        for line, raw in enumerate(reader, 2):
            if None in raw:
                raise ValueError("第 %s 行列数超过表头" % line)
            if not any(str(value or "").strip() for value in raw.values()):
                continue
            row = {field: (raw.get(field) or "").strip() for field in FIELDS}
            if not row["task_id"] or row["arm"] not in ARMS or row["status"] not in STATUSES:
                raise ValueError("第 %s 行 task_id、arm 或 status 无效" % line)
            row["repeat"] = _number(row["repeat"], "repeat", line, integer=True)
            if not row["repeat"]:
                raise ValueError("第 %s 行 repeat 必须是正整数" % line)
            for field in (*TOKEN_FIELDS, "human_interventions"):
                row[field] = _number(row[field], field, line, integer=True)
            for field in ("usd_cost", "wall_seconds"):
                row[field] = _number(row[field], field, line)
            for field in ("hidden_pass", "usage_complete"):
                row[field] = _boolean(row[field], field, line)
            for subset, total in (
                ("codex_cached_input_tokens", "codex_input_tokens"),
                ("codex_reasoning_output_tokens", "codex_output_tokens"),
                ("ds_cached_input_tokens", "ds_input_tokens"),
            ):
                if row[subset] is not None and row[total] is not None and row[subset] > row[total]:
                    raise ValueError("第 %s 行 %s 不能大于 %s" % (line, subset, total))
            if row["usage_complete"] and any(row[field] is None for field in TOTAL_FIELDS):
                raise ValueError("第 %s 行标记 usage_complete=true，但总输入/输出缺测" % line)
            key = (row["task_id"], row["repeat"], row["arm"])
            if key in seen:
                raise ValueError("第 %s 行重复 run 键: %r" % (line, key))
            seen.add(key)
            rows.append(row)
    return rows


def _success(row):
    if row["status"] != "pass" or row["hidden_pass"] is False:
        return False
    if row["human_interventions"] is not None and row["human_interventions"] > 0:
        return False
    if row["hidden_pass"] is None or row["human_interventions"] is None:
        return None
    return True


def _complete_sum(rows, fields, require_usage=False):
    if not rows or (require_usage and not all(row["usage_complete"] is True for row in rows)):
        return None
    if any(row[field] is None for row in rows for field in fields):
        return None
    return sum(row[field] for row in rows for field in fields)


def _arm_summary(rows):
    outcomes = [_success(row) for row in rows]
    successes = outcomes.count(True)
    quality_complete = None not in outcomes
    codex_tokens = _complete_sum(rows, ("codex_input_tokens", "codex_output_tokens"), True)
    ds_tokens = _complete_sum(rows, ("ds_input_tokens", "ds_output_tokens"), True)
    all_tokens = None if codex_tokens is None or ds_tokens is None else codex_tokens + ds_tokens
    usd = _complete_sum(rows, ("usd_cost",))

    def per_success(value):
        return value / successes if value is not None and successes and quality_complete else None

    return {
        "attempts": len(rows),
        "independent_tasks": len({row["task_id"] for row in rows}),
        "status_counts": {status: sum(row["status"] == status for row in rows) for status in STATUSES},
        "verified_successes": successes,
        "unknown_quality_runs": outcomes.count(None),
        "quality_complete": quality_complete,
        "success_rate": successes / len(rows) if rows and quality_complete else None,
        "usage_complete": bool(rows) and all(row["usage_complete"] is True for row in rows),
        "codex_total_tokens": codex_tokens,
        "ds_total_tokens": ds_tokens,
        "all_model_total_tokens": all_tokens,
        "codex_tokens_per_verified_success": per_success(codex_tokens),
        "ds_tokens_per_verified_success": per_success(ds_tokens),
        "all_model_tokens_per_verified_success": per_success(all_tokens),
        "usd_cost_total": usd,
        "usd_cost_per_verified_success": per_success(usd),
        "wall_seconds_total": _complete_sum(rows, ("wall_seconds",)),
        "human_interventions_total": _complete_sum(rows, ("human_interventions",)),
    }


def _comparison(rows, baseline, candidate):
    by_arm = {
        arm: {(row["task_id"], row["repeat"]): row for row in rows if row["arm"] == arm}
        for arm in (baseline, candidate)
    }
    a, b = by_arm[baseline], by_arm[candidate]
    union, common = set(a) | set(b), set(a) & set(b)
    a_rows, b_rows = [a[key] for key in sorted(common)], [b[key] for key in sorted(common)]
    result = {
        "baseline": baseline, "candidate": candidate,
        "paired_runs": len(common),
        "independent_paired_tasks": len({key[0] for key in common}),
        "missing_in_baseline": [list(key) for key in sorted(set(b) - set(a))],
        "missing_in_candidate": [list(key) for key in sorted(set(a) - set(b))],
        "paired_baseline": _arm_summary(a_rows),
        "paired_candidate": _arm_summary(b_rows),
        "codex_token_savings_fraction": None,
        "codex_token_savings_status": "不可判断：没有完整配对任务",
        "task_pairs": [
            {"task_id": key[0], "repeat": key[1],
             "baseline_success": _success(a[key]), "candidate_success": _success(b[key])}
            for key in sorted(common)
        ],
    }
    if not common or common != union:
        return result
    a_total = result["paired_baseline"]["codex_total_tokens"]
    b_total = result["paired_candidate"]["codex_total_tokens"]
    if a_total is None or b_total is None:
        result["codex_token_savings_status"] = "不可判断：usage 不完整或总输入/输出缺测"
    elif a_total == 0:
        result["codex_token_savings_status"] = "不可判断：基线 Codex Token 总量为 0"
    else:
        result["codex_token_savings_fraction"] = 1 - b_total / a_total
        result["codex_token_savings_status"] = "已测得该批任务的 Codex 原始 Token 差异；需同时核对质量"
    return result


def summarize(rows):
    present = [arm for arm in ARMS if any(row["arm"] == arm for row in rows)]
    return {
        "schema_version": 1, "recorded_attempts": len(rows),
        "independent_tasks": len({row["task_id"] for row in rows}),
        "arms": {arm: _arm_summary([row for row in rows if row["arm"] == arm]) for arm in present},
        "comparisons": [
            _comparison(rows, baseline, candidate)
            for baseline, candidate in (
                ("codex_only", "codex_ds_bridge"),
                ("dsh_ds", "codex_ds_bridge"),
                ("ds_only_same_bridge", "codex_ds_bridge"),
            ) if baseline in present or candidate in present
        ],
        "notes": [
            "没有实测数据时不能宣称省 Token 或质量更高；缺测值为 null，绝不补 0。",
            "input 已含 cached，output 已含 reasoning；子项不重复相加。",
            "成功要求 status=pass、隐藏要求全部通过且无人工返工；失败、超时、阻塞均计入分母。",
            "每成功任务用量包含该组失败尝试与回退消耗；只在质量和用量完整时计算。",
            "跨模型原始 Token 总和仅作工作量记录，不等同于费用或订阅额度。",
            "重复轮次不是独立任务；没有置信区间或智能程度优劣结论。",
        ],
    }


def to_markdown(report):
    def fmt(value):
        if value is None:
            return "缺测 / 不可判断"
        if isinstance(value, float):
            return "%.4g" % value
        return str(value)

    lines = ["# 测试记录汇总", "", "记录 %s 次完整尝试，涉及 %s 个独立任务。" % (
        report["recorded_attempts"], report["independent_tasks"]), "",
        "| 组 | 尝试 | 已验证成功 | 成功率 | Codex Token | DS Token | 每成功任务 Codex Token | 总美元费用 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for arm, metrics in report["arms"].items():
        rate = metrics["success_rate"]
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            arm, metrics["attempts"], metrics["verified_successes"],
            "不可判断" if rate is None else "%.1f%%" % (100 * rate),
            fmt(metrics["codex_total_tokens"]), fmt(metrics["ds_total_tokens"]),
            fmt(metrics["codex_tokens_per_verified_success"]), fmt(metrics["usd_cost_total"])))
    for comparison in report["comparisons"]:
        savings = comparison["codex_token_savings_fraction"]
        lines.extend(["", "- %s → %s：%s 个配对尝试；Codex Token 节省率 %s。%s。" % (
            comparison["baseline"], comparison["candidate"], comparison["paired_runs"],
            "不可判断" if savings is None else "%.1f%%" % (100 * savings),
            comparison["codex_token_savings_status"])])
    lines.extend(["", *["- " + note for note in report["notes"]]])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = summarize(read_runs(args.csv_path))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n" if args.format == "json" else to_markdown(report)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)


if __name__ == "__main__":
    main()
