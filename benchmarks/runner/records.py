"""Durable run records and loss-aware usage importers (standard library only)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import time


def clock_record():
    return {"utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(), "pid": os.getpid()}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_jsonl(path, value):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(usage, source, granularity):
    """Cached input and reasoning output are subsets, never additive totals."""
    if not isinstance(usage, dict):
        return {"source": source, "granularity": granularity, "status": "unknown",
                "input_tokens": None, "cached_input_tokens": None,
                "output_tokens": None, "reasoning_output_tokens": None,
                "total_tokens": None}
    issues = []
    details = {}
    for key in ("prompt_tokens_details", "input_tokens_details", "completion_tokens_details", "output_tokens_details"):
        value = usage.get(key)
        if value is None:
            details[key] = {}
        elif not isinstance(value, dict):
            details[key] = {}
            issues.append("invalid_" + key)
        else:
            details[key] = value

    def field(name, candidates):
        supplied = [value for value in candidates if value is not None]
        if not supplied:
            return None
        if any(_integer(value) is None for value in supplied):
            issues.append("invalid_" + name)
            return None
        if len(set(supplied)) != 1:
            issues.append("conflicting_" + name)
            return None
        return supplied[0]

    incoming = field("input_tokens", [usage.get("input_tokens"), usage.get("prompt_tokens")])
    outgoing = field("output_tokens", [usage.get("output_tokens"), usage.get("completion_tokens")])
    cached = field("cached_input_tokens", [usage.get("cached_input_tokens"), usage.get("prompt_cache_hit_tokens"),
        details["prompt_tokens_details"].get("cached_tokens"), details["input_tokens_details"].get("cached_tokens")])
    reasoning = field("reasoning_output_tokens", [usage.get("reasoning_output_tokens"), usage.get("reasoning_tokens"),
        details["completion_tokens_details"].get("reasoning_tokens"), details["output_tokens_details"].get("reasoning_tokens")])
    supplied_total = field("total_tokens", [usage.get("total_tokens")])
    if cached is not None and incoming is not None and cached > incoming:
        cached = None; issues.append("cached_exceeds_input")
    if reasoning is not None and outgoing is not None and reasoning > outgoing:
        reasoning = None; issues.append("reasoning_exceeds_output")
    cache_miss = field("cache_miss_tokens", [usage.get("prompt_cache_miss_tokens")])
    if cache_miss is not None and incoming is not None and cached is not None and cache_miss + cached != incoming:
        issues.append("cache_parts_conflict_with_input")
    total = incoming + outgoing if incoming is not None and outgoing is not None else None
    if total is not None and supplied_total is not None and supplied_total != total:
        issues.append("total_conflicts_with_input_output")
    if issues:
        total = None
    result = {"source": source, "granularity": granularity,
            "status": "reported" if incoming is not None and outgoing is not None and not issues else "partial",
            "input_tokens": incoming, "cached_input_tokens": cached,
            "output_tokens": outgoing, "reasoning_output_tokens": reasoning,
            "total_tokens": total}
    if issues:
        result["validation_errors"] = sorted(set(issues))
    return result


def import_codex_jsonl(path):
    """Import CLI turn totals, without pretending they expose individual HTTP requests."""
    records, errors, thread_id = [], [], None
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                errors.append({"line": index, "reason": "invalid_json"})
                continue
            if not isinstance(event, dict):
                errors.append({"line": index, "reason": "non_object"})
                continue
            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
            if event.get("type") == "turn.completed":
                row = normalize_usage(event.get("usage"), "codex_cli_json", "turn")
                row.update({"event_line": index, "thread_id": thread_id,
                            "measurement_context": "cli_control"})
                records.append(row)
            elif event.get("type") in {"error", "turn.failed"}:
                errors.append({"line": index, "reason": event["type"]})
    if not records:
        records.append({**normalize_usage(None, "codex_cli_json", "turn"),
                        "measurement_context": "cli_control", "thread_id": thread_id,
                        "reason": "no_completed_turn_usage"})
    return {"records": records, "errors": errors,
            "raw_sha256": sha256_file(path), "request_count": None,
            "request_count_status": "not_exposed_by_cli_turn_events"}


def import_request_ledger(path, source="external_provider"):
    """Import existing request ledger. Unknown/failed attempts stay in the denominator."""
    records, seen, starts, finishes = [], set(), {}, {}
    with Path(path).open(encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                records.append({"source": source, "granularity": "request", "status": "unknown",
                                "event_line": index, "reason": "invalid_json"})
                continue
            if not isinstance(event, dict):
                records.append({"source": source, "granularity": "request", "status": "unknown",
                                "event_line": index, "reason": "non_object"})
                continue
            request_id = event.get("request_id")
            if event.get("event") in {"start", "finish"}:
                bucket = starts if event["event"] == "start" else finishes
                if not isinstance(request_id, str) or not request_id or request_id in bucket or request_id in seen:
                    raise ValueError("Invalid or duplicate request ledger transition")
                bucket[request_id] = (index, event)
                continue
            if request_id and request_id in seen:
                raise ValueError("Duplicate request_id; do not sum the same request twice")
            if request_id:
                seen.add(request_id)
            row = normalize_usage(event.get("usage"), source, "request")
            # Deliberately do not copy request bodies, headers, credentials, or model reasoning.
            for key in ("request_id", "stage", "model", "status_code", "outcome", "elapsed_seconds"):
                if key in event:
                    row[key] = event[key]
            row["event_line"] = index
            records.append(row)
    if finishes.keys() - starts.keys():
        raise ValueError("Request ledger finish without matching start")
    for identity, (index, started) in starts.items():
        finished = finishes.get(identity, (None, {}))[1]
        row = normalize_usage(finished.get("usage"), source, "request")
        if finished.get("usage_complete") is False:
            row["status"] = "partial"
            row["total_tokens"] = None
            row["validation_errors"] = sorted(set(row.get("validation_errors", []) + ["usage_marked_incomplete"]))
        row.update({"request_id": identity, "event_line": index,
                    "stage": started.get("stage"), "model": started.get("model"),
                    "outcome": finished.get("status", "unfinished"),
                    "finish_reason": finished.get("finish_reason")})
        records.append(row)
    return {"records": records, "raw_sha256": sha256_file(path),
            "request_count": len(records),
            "unknown_usage_requests": sum(row.get("status") != "reported" for row in records)}


def summarize_usage(records):
    """A partial sum stays partial; absent numbers are never silently treated as zero."""
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    return {"known_sums": {key: sum(row[key] for row in records if _integer(row.get(key)) is not None)
                            for key in keys},
            "unknown_counts": {key: sum(_integer(row.get(key)) is None for row in records) for key in keys},
            "complete": bool(records) and all(row.get("status") == "reported" for row in records),
            "record_count": len(records)}
