"""Read-only, content-free pilot analysis. Does not open agent prompts or answers."""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
from pathlib import Path
import re

from benchmarks.runner.records import import_request_ledger, normalize_usage

ARMS = ("codex_pure", "codex_bridge", "deepseek_harness")
LABELS = {"codex_pure": "纯 Codex", "codex_bridge": "Codex＋外接模型", "deepseek_harness": "DeepSeek Harness"}
METRICS = ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens",
           "reasoning_output_tokens", "total_tokens")
STATES = {"completed": "运行结束", "failed": "失败", "timeout": "超时", "output_limit": "输出超限",
          "uncertain": "不确定", "launch_failed": "启动失败", "not_started": "未开始", "unknown": "未知"}


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def read_json(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def empty_usage(status="missing"):
    return {"status": status, "complete": False, "records": None, "request_count": None,
            **{key: None for key in METRICS}}


def unstarted_usage(granularity):
    return {"status": "not_started", "complete": True, "records": 0, "granularity": granularity,
            "request_count": 0 if granularity == "request" else None,
            **{key: 0 for key in METRICS}, "known_lower_sums": {key: 0 for key in METRICS},
            "reason": "no_model_process_started"}


def combine_usage(records, *, granularity, request_count=None):
    """Keep each missing subset null; malformed totals never become complete."""
    if records is None:
        return empty_usage()
    if not records and granularity != "request":
        return empty_usage()
    normalized = []
    for record in records:
        if not isinstance(record, dict):
            normalized.append(normalize_usage(None, "report", granularity)); continue
        row = normalize_usage(record, "report", granularity)
        if record.get("status") in {"partial", "unknown"} or record.get("validation_errors"):
            row["status"] = "partial"
            row["total_tokens"] = None
        normalized.append(row)
    def total(key):
        values = [row.get(key) for row in normalized]
        return sum(values) if all(number(value) is not None for value in values) else None
    complete = all(row.get("status") == "reported" for row in normalized)
    result = {"status": "complete" if complete else "partial", "complete": complete,
              "records": len(normalized), "granularity": granularity, "request_count": request_count,
              **{key: total(key) for key in METRICS if key != "uncached_input_tokens"}}
    incoming, cached = result["input_tokens"], result["cached_input_tokens"]
    result["uncached_input_tokens"] = incoming - cached if incoming is not None and cached is not None and cached <= incoming else None
    known = {}
    for key in METRICS:
        if key == "uncached_input_tokens":
            values = [row["input_tokens"] - row["cached_input_tokens"] for row in normalized
                      if row.get("input_tokens") is not None and row.get("cached_input_tokens") is not None]
        else:
            values = [row[key] for row in normalized if number(row.get(key)) is not None]
        known[key] = sum(values) if values else (0 if not normalized else None)
    result["known_lower_sums"] = known
    return result


def codex_usage(result, arm):
    if arm == "deepseek_harness":
        return empty_usage("not_applicable")
    document = (result or {}).get("usage")
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        return empty_usage()
    return combine_usage(document["records"], granularity="turn")


def external_usage(path, arm):
    if arm == "codex_pure":
        return {**empty_usage("not_applicable"), "request_count": 0,
                "request_count_status": "proven_no_external_model_route"}
    if not Path(path).is_file() or Path(path).is_symlink():
        return empty_usage()
    try:
        imported = import_request_ledger(path, source="trusted_api_broker")
    except (OSError, ValueError, TypeError):
        return empty_usage("invalid_ledger")
    result = combine_usage(imported["records"], granularity="request", request_count=imported["request_count"])
    result["unknown_usage_requests"] = imported["unknown_usage_requests"]
    return result


def valid_grade(grade):
    if not isinstance(grade, dict) or grade.get("source") != "independent_grader":
        return False
    total, passed = number(grade.get("tests_total")), number(grade.get("tests_passed"))
    return (grade.get("passed") is True and total is not None and total > 0 and passed == total and
            grade.get("file_snapshot_unchanged") is True and grade.get("original_grader_unchanged") is True and
            not grade.get("counters"))


def api_estimate(usage, pricing):
    """Declared peak-price estimate. Missing cache detail uses an explicit upper bound."""
    if not usage.get("complete") or usage.get("status") == "not_applicable":
        return None
    prices = pricing.get("usd_per_million_peak", {}) if isinstance(pricing, dict) else {}
    rates = [number(prices.get(key)) for key in ("input_cached", "input_uncached", "output")]
    conversion = number(pricing.get("usd_to_cny_budget_ceiling")) if isinstance(pricing, dict) else None
    incoming, outgoing = number(usage.get("input_tokens")), number(usage.get("output_tokens"))
    if None in rates or conversion is None or incoming is None or outgoing is None:
        return None
    cached = number(usage.get("cached_input_tokens"))
    input_cost = ((incoming - cached) * rates[1] + cached * rates[0]
                  if cached is not None and cached <= incoming else incoming * max(rates[0], rates[1]))
    return (input_cost + outgoing * rates[2]) / 1_000_000 * conversion


def aggregate_usage(rows, key, arm):
    if (key == "codex_usage" and arm == "deepseek_harness") or (key == "external_usage" and arm == "codex_pure"):
        value = empty_usage("not_applicable")
        if key == "external_usage" and arm == "codex_pure":
            value.update(request_count=0, request_count_status="proven_no_external_model_route")
        return value
    if not rows:
        return empty_usage()
    usages = [row[key] for row in rows]
    complete = bool(usages) and all(usage.get("complete") for usage in usages)
    values = {}
    known = {}
    for metric in METRICS:
        numbers = [number(usage.get(metric)) for usage in usages]
        values[metric] = sum(numbers) if all(value is not None for value in numbers) else None
        known_numbers = []
        for usage in usages:
            if usage.get("status") == "not_started": continue
            value = (usage.get("known_lower_sums") or {}).get(metric, usage.get(metric))
            if number(value) is not None: known_numbers.append(value)
        known[metric] = sum(known_numbers) if known_numbers else (0 if all(usage.get("status") == "not_started" for usage in usages) else None)
    requests = [number(usage.get("request_count")) for usage in usages]
    return {"status": "complete" if complete else "partial", "complete": complete,
            **values, "known_lower_sums": known,
            "request_count": sum(requests) if all(value is not None for value in requests) else None}


def build_report(control_root):
    """Read numeric records and checkpoint state; never read model prompts/answers."""
    control = Path(control_root).resolve()
    prereg = read_json(control / "preregistration.json")
    if not prereg or not isinstance(prereg.get("schedule"), list):
        raise ValueError("A frozen preregistration schedule is required")
    summary = read_json(control / "summary.json") or {}
    saved_rows = {(row.get("task_id"), row.get("arm"), row.get("repeat", 1)): row
                  for row in summary.get("results", []) if isinstance(row, dict)}
    pricing = (summary.get("budget") or {}).get("pricing", {})
    rows = []
    for assignment in prereg["schedule"]:
        task, arm, repeat = assignment.get("task_id"), assignment.get("arm"), assignment.get("repeat", 1)
        if (not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task) or arm not in ARMS or
                not isinstance(repeat, int) or isinstance(repeat, bool) or not 1 <= repeat <= 100):
            raise ValueError("Invalid preregistered task/arm/repeat")
        run = control / "runs" / task / arm / f"r{repeat:02d}"
        result = read_json(run / "result.json")
        checkpoint = read_json(run / "checkpoint.json") or {}
        saved = saved_rows.get((task, arm, repeat), {})
        grade_record = read_json(run / "grade.json") or {}
        grade = grade_record.get("grade") or saved.get("grade") or {}
        checkpoint_state = checkpoint.get("state")
        fallback_state = "uncertain" if checkpoint_state and checkpoint_state != "prepared" else "not_started"
        state = (result or {}).get("state", saved.get("state", fallback_state))
        if state not in STATES: state = "unknown"
        c_usage, e_usage = codex_usage(result, arm), external_usage(run / "external-usage.jsonl", arm)
        if state == "not_started" and e_usage.get("status") not in {"missing", "not_applicable"} and e_usage.get("request_count") != 0:
            state = "uncertain"
        if state == "not_started":
            if arm != "deepseek_harness": c_usage = unstarted_usage("turn")
            if arm != "codex_pure": e_usage = unstarted_usage("request")
        successful = state == "completed" and valid_grade(grade)
        row = {"task_id": task, "arm": arm, "repeat": repeat, "state": state,
               "successful": successful, "grader_passed": grade.get("passed") if isinstance(grade.get("passed"), bool) else None,
               "tests_passed": number(grade.get("tests_passed")), "tests_total": number(grade.get("tests_total")),
               "wall_seconds": number((result or {}).get("wall_seconds", saved.get("wall_seconds"))),
               "grader_wall_seconds": number(grade.get("wall_seconds")),
               "codex_usage": c_usage, "external_usage": e_usage,
               "api_estimated_cny_upper_bound": 0 if state == "not_started" or arm == "codex_pure" else api_estimate(e_usage, pricing),
               "metering_status": {"codex": c_usage["status"], "external": e_usage["status"]},
               "not_started_model_consumption_is_zero": state == "not_started",
               "protocol_violation": bool((result or {}).get("changed_immutable_inputs") or
                                           (result or {}).get("protocol_violations") or
                                           (result or {}).get("artifact_error") or
                                           grade.get("file_snapshot_unchanged") is False or
                                           grade.get("original_grader_unchanged") is False)}
        known_external = e_usage.get("known_lower_sums") or {}
        row["api_known_requests_estimated_cny_upper_bound"] = api_estimate(
            {**known_external, "complete": known_external.get("total_tokens") is not None,
             "status": "known_part_only"}, pricing)
        if arm == "codex_pure": row["api_known_requests_estimated_cny_upper_bound"] = 0
        rows.append(row)
    groups = {}
    for arm in ARMS:
        selected = [row for row in rows if row["arm"] == arm]
        attempted = [row for row in selected if row["state"] != "not_started"]
        successful = [row for row in selected if row["successful"]]
        walls = [row["wall_seconds"] for row in attempted]
        all_wall = sum(walls) if attempted and all(value is not None for value in walls) else None
        success_wall = sum(row["wall_seconds"] for row in successful) if successful and all(row["wall_seconds"] is not None for row in successful) else None
        c_total, e_total = aggregate_usage(selected, "codex_usage", arm), aggregate_usage(selected, "external_usage", arm)
        api_costs = [row["api_estimated_cny_upper_bound"] for row in attempted]
        api_total = sum(api_costs) if attempted and all(value is not None for value in api_costs) else None
        if arm == "codex_pure": api_total = 0
        known_api_costs = [row["api_known_requests_estimated_cny_upper_bound"] for row in attempted
                           if row.get("api_known_requests_estimated_cny_upper_bound") is not None]
        successes = len(successful)
        groups[arm] = {"scheduled": len(selected), "attempted": len(attempted), "successes": successes,
            "completed": sum(row["state"] == "completed" for row in selected),
            "timeouts": sum(row["state"] == "timeout" for row in selected),
            "not_started": sum(row["state"] == "not_started" for row in selected),
            "unknown_or_uncertain": sum(row["state"] in {"unknown", "uncertain"} for row in selected),
            "protocol_violations": sum(row["protocol_violation"] for row in selected),
            "codex_usage": c_total, "external_usage": e_total,
            "all_attempts_wall_seconds": all_wall,
            "known_attempts_wall_seconds": sum(value for value in walls if value is not None),
            "successful_tasks_wall_seconds": success_wall,
            "seconds_per_success_including_failures": all_wall / successes if all_wall is not None and successes else None,
            "codex_tokens_per_success_including_failures": c_total["total_tokens"] / successes if c_total["total_tokens"] is not None and successes else None,
            "external_tokens_per_success_including_failures": e_total["total_tokens"] / successes if e_total["total_tokens"] is not None and successes else None,
            "api_estimated_cny_upper_bound": api_total,
            "api_known_requests_estimated_cny_upper_bound": sum(known_api_costs) if known_api_costs else None,
            "api_cny_per_success_including_failures": api_total / successes if api_total is not None and successes else None}
    a, b = groups["codex_pure"], groups["codex_bridge"]
    reasons = []
    if a["scheduled"] != 6 or b["scheduled"] != 6 or a["completed"] != 6 or b["completed"] != 6:
        reasons.append("六题纯组与混合组尚未全部运行结束")
    if not a["codex_usage"]["complete"] or not b["codex_usage"]["complete"]:
        reasons.append("Codex 主用量存在缺失或异常，不能将未知当零")
    if a["successes"] != 6 or b["successes"] != 6:
        reasons.append("尚未满足两组各六题全部通过同一独立验收的质量条件")
    if not a["codex_usage"]["total_tokens"]:
        reasons.append("纯组基线 Token 不足以计算比例")
    quality_ready = a["scheduled"] == b["scheduled"] == a["completed"] == b["completed"] == a["successes"] == b["successes"] == 6
    savings = (1 - b["codex_usage"]["total_tokens"] / a["codex_usage"]["total_tokens"]) if not reasons else None
    time_ready = quality_ready and a["all_attempts_wall_seconds"] is not None and b["all_attempts_wall_seconds"] is not None and a["all_attempts_wall_seconds"] > 0
    comparison = {"codex_token_savings_ratio": savings, "eligible": not reasons, "ineligible_reasons": reasons,
                  "quality_condition": "Both A and B pass all six fixed independent graders; all attempted runs/costs retained.",
                  "wall_time_reduction_ratio": (1 - b["all_attempts_wall_seconds"] / a["all_attempts_wall_seconds"]) if time_ready else None,
                  "time_comparison_quality_eligible": time_ready,
                  "cost_units_separate": True, "subscription_quota_or_money_savings_proven": False}
    task_ids = list(dict.fromkeys(row["task_id"] for row in rows))
    attempted_rows = [row for row in rows if row["state"] != "not_started"]
    actual_task_count = len(set(row["task_id"] for row in attempted_rows))
    not_started_count = sum(row["state"] == "not_started" for row in rows)
    paired = [{"task_id": task, "arms": {arm: [row for row in rows if row["task_id"] == task and row["arm"] == arm]
                                            for arm in ARMS}} for task in task_ids]
    posthoc_source = read_json(control / "control/posthoc-bridge-stage-grades.json") or {}
    posthoc = None
    diagnostic_rows = posthoc_source.get("records", [])
    if (posthoc_source.get("kind") == "post_hoc_unsubmitted_stage_diagnostic" and
            isinstance(diagnostic_rows, list) and diagnostic_rows and
            any(row["arm"] == "codex_bridge" and row["state"] != "not_started" and not row["successful"] for row in rows) and
            all(isinstance(row, dict) and row.get("original_B_score_changed") is False for row in diagnostic_rows)):
        posthoc = {"kind": "post_hoc_unsubmitted_stage_diagnostic", "formal_score_unchanged": True,
                   "api_requests": number(posthoc_source.get("api_requests")),
                   "stages": [{key: row.get(key) for key in ("delegate", "tests_passed", "tests_total", "stage_unchanged", "official_grader_unchanged")}
                              for row in diagnostic_rows],
                   "diagnosis": "正式交付未通过；事后隔离评分的未提交候选通过，故需修复初审格式/交付流程，不将事后成绩计入正式分数。"}
    return {"schema_version": 1, "measurement_context": "cli_control", "desktop_product_equivalent": False,
            "task_count": len(task_ids), "repeats": sorted(set(row["repeat"] for row in rows)),
            "planned_task_count": len(task_ids), "actual_task_count": actual_task_count,
            "planned_run_count": len(rows), "actual_run_count": len(attempted_rows), "not_started_run_count": not_started_count,
            "post_hoc_diagnostic": posthoc,
            "codex_model": prereg.get("codex_model"), "reasoning_effort": prereg.get("reasoning_effort"),
            "external_model": prereg.get("external_model"), "groups": groups, "paired": paired,
            "implementation_sources": prereg.get("implementation_sources"),
            "rows": rows, "comparison": comparison,
            "campaign_api_estimated_cny": number(summary.get("api_estimated_cny")),
            "budget_limits": (summary.get("budget") or {}).get("limits"),
            "limitations": ["这是桌面总控下的受控 CLI 三组试验，不是桌面 GUI 操作开销或性能实测。",
                            f"原计划 {len(task_ids)} 个公开 Python 任务、{len(rows)} 次运行；实际 {actual_task_count} 题、{len(attempted_rows)} 次，{not_started_count} 次未开始，不能据此发布普遍智能排名。",
                            "公开练习可能进入模型训练语料，不能排除训练污染；三组均看不到官方答案与隐藏 tests。",
                            "Codex 与外部模型 Token 分开报告，不能跨模型直接相加后当费用或订阅额度。",
                            "缓存输入包含在输入中，推理输出包含在输出中；缺项为 null。",
                            "失败、超时与未知用量保留；未知未填零，不使用成功子集夸大节省。"]}


def display(value, digits=2):
    if value is None: return "未知"
    return str(value) if isinstance(value, int) else f"{value:.{digits}f}"


def markdown(report):
    comparison = report["comparison"]
    lines = ["# 本机三组评测报告", "", "**受控 CLI 试验，由桌面 Codex 总控；不是桌面 GUI 性能实测。**", "",
             f"**计划 {report['planned_task_count']} 题、{report['planned_run_count']} 次；实际 {report['actual_task_count']} 题、{report['actual_run_count']} 次运行，{report['not_started_run_count']} 次未开始。当前结果不能作为普遍智能排名。**", "",
             f"Codex：`{report.get('codex_model')}`，思考强度 `{report.get('reasoning_effort')}`；外部模型：`{report.get('external_model')}`。", ""]
    if comparison["eligible"]:
        lines += [f"在两组六题均通过同一独立验收、主用量完整的条件下，混合组 Codex Token 变化对应节省率 **{comparison['codex_token_savings_ratio']:.1%}**。这不是 Pro 周额度或人民币费用节省率。", ""]
    else:
        lines += ["**当前不能得出同等质量下的 Codex Token 节省率。** " + "；".join(comparison["ineligible_reasons"]) + "。", ""]
    lines += ["## 完成效果与时间", "", "| 组别 | 成功/计划 | 运行结束 | 超时 | 未开始 | 全部已尝试耗时秒 | 成功任务耗时秒 | 含失败成本的每成功耗时秒 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for arm in ARMS:
        group = report["groups"][arm]
        lines.append(f"| {LABELS[arm]} | {group['successes']}/{group['scheduled']} | {group['completed']} | {group['timeouts']} | {group['not_started']} | {display(group['all_attempts_wall_seconds'])} | {display(group['successful_tasks_wall_seconds'])} | {display(group['seconds_per_success_including_failures'])} |")
    lines += ["", "## 已发生主用量（未开始消耗为零，已尝试但漏计保持未知）", "", "| 组别/模型 | 输入 | 缓存输入 | 非缓存输入 | 输出 | 推理输出 | 总 Token | 外部请求 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for arm in ARMS:
        for key, label in (("codex_usage", "Codex"), ("external_usage", "外部模型")):
            usage = report["groups"][arm][key]
            if usage["status"] == "not_applicable": continue
            lines.append("| " + LABELS[arm] + "/" + label + " | " + " | ".join(display(usage.get(metric)) for metric in METRICS) + " | " + display(usage.get("request_count")) + " |")
    lines += ["", "缓存与推理都是子集，不重复计入总数；CLI turn 用量不能当作逐 HTTP 请求数。API 费用采用声明的峰值价格及换算上限估算，不是账单。", "",
              "## 已尝试任务的计量与失败成本", "", "| 任务/组别 | 状态 | Codex Token | 外部 Token | 外部已知部分 Token | 已知请求费用估算上限元 | 全部费用估算上限元 |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in report["rows"]:
        if row["state"] == "not_started": continue
        c, e = row["codex_usage"], row["external_usage"]
        known = (e.get("known_lower_sums") or {}).get("total_tokens")
        codex_tokens = "未使用" if c["status"] == "not_applicable" else display(c.get("total_tokens"))
        external_tokens = "未使用" if e["status"] == "not_applicable" else display(e.get("total_tokens"))
        known_tokens = "不适用" if e["status"] == "not_applicable" else display(known)
        lines.append("| " + row["task_id"] + "/" + LABELS[row["arm"]] + " | " + STATES[row["state"]] + "; Codex=" + c["status"] + "; 外部=" + e["status"] + " | " +
                     " | ".join([codex_tokens, external_tokens, known_tokens,
                        display(row.get("api_known_requests_estimated_cny_upper_bound"), 4), display(row.get("api_estimated_cny_upper_bound"), 4)]) + " |")
    lines += ["", "**已知部分不是全部消耗。** 存在 pending、缺失或异常 usage 时，全部 Token/费用仍为未知；失败与超时的已知消耗也保留。未开始任务没有模型执行消耗，评分仍为未评测。", "",
              "## 逐任务配对", "", "| 任务 | 纯 Codex | 混合 | DSH | 纯/混合/DSH 耗时秒 |", "|---|---|---|---|---|"]
    for pair in report["paired"]:
        cells, times = [], []
        for arm in ARMS:
            rows = pair["arms"][arm]
            if not rows:
                cells.append("未开始"); times.append("未知"); continue
            row = rows[0]
            cells.append("通过" if row["successful"] else STATES[row["state"]] + ("/验收未过" if row["grader_passed"] is False else ""))
            times.append(display(row["wall_seconds"]))
        lines.append("| " + pair["task_id"] + " | " + " | ".join(cells) + " | " + "/".join(times) + " |")
    posthoc = report.get("post_hoc_diagnostic")
    if posthoc:
        stages = "、".join(str(row.get("delegate")) + "：" + display(row.get("tests_passed")) + "/" + display(row.get("tests_total")) for row in posthoc["stages"])
        lines += ["", "### 事后诊断（不计正式成绩）", "",
                  "混合组正式交付未通过。之后对未提交候选做隔离评分：" + stages + "。这次事后诊断没有新增模型 API 请求，候选和官方评分文件保持不变。", "",
                  "失败环节指向初审格式与交付流程；候选的事后通过不能替换正式提交。原混合组分数与失败成本全部保留。"]
    lines += ["", "## 解释边界", ""] + ["- " + note for note in report["limitations"]]
    return "\n".join(lines) + "\n"


def csv_text(report):
    base_fields = ["task_id", "arm", "repeat", "state", "successful", "grader_passed", "tests_passed", "tests_total", "wall_seconds", "grader_wall_seconds", "protocol_violation", "api_estimated_cny_upper_bound", "api_known_requests_estimated_cny_upper_bound"]
    usage_fields = [prefix + "_" + key for prefix in ("codex", "external") for key in (*METRICS, "request_count", "status")]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=base_fields + usage_fields)
    writer.writeheader()
    for row in report["rows"]:
        flat = {key: row.get(key) for key in base_fields}
        for prefix in ("codex", "external"):
            flat.update({prefix + "_" + key: row[prefix + "_usage"].get(key) for key in (*METRICS, "request_count", "status")})
        writer.writerow(flat)
    return stream.getvalue()


def write_reports(control_root, output_dir=None):
    report = build_report(control_root)
    output = Path(output_dir or Path(control_root) / "report")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    (output / "评测报告.md").write_text(markdown(report), encoding="utf-8")
    (output / "逐任务数据.csv").write_text(csv_text(report), encoding="utf-8-sig")
    (output / "评测汇总.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return output


def main():
    parser = argparse.ArgumentParser(description="只读用量和分数，生成脱敏中文评测报告")
    parser.add_argument("control_root")
    parser.add_argument("--output-dir")
    args = parser.parse_args()
    output = write_reports(args.control_root, args.output_dir)
    print(str(output))


if __name__ == "__main__": main()
