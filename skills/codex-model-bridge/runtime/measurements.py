"""Read recorded Codex session counters without exporting conversation content.

These counters are telemetry, not billing tokens or subscription quota. A task
must run in its own candidate chat: this reader cannot separate a coordinator's
planning/development from candidate work performed in the same measured turn.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import uuid


COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
_CORE = ("input_tokens", "output_tokens", "total_tokens")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,199}\Z")
_NOTES = ["recorded_usage_is_not_billing_or_weekly_quota",
          "covers_only_token_events_already_written_to_this_session",
          "use_a_fresh_isolated_candidate_chat_to_exclude_coordinator_development"]


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _safe_label(value):
    return value if isinstance(value, str) and _LABEL.fullmatch(value) and not value.startswith(("https:", "http:")) else None


def _id(value):
    if not isinstance(value, str):
        return None
    try:
        canonical = str(uuid.UUID(value))
        return canonical if value.lower() == canonical else None
    except ValueError:
        return None


def _number(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1


def _usage(raw):
    if not isinstance(raw, dict):
        return None
    if any(not _number(raw.get(key)) for key in _CORE):
        return None
    result = {key: raw.get(key) if _number(raw.get(key)) else None for key in COUNTERS}
    if result["total_tokens"] != result["input_tokens"] + result["output_tokens"]:
        return None
    for child, parent in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if child in raw and (result[child] is None or result[child] > result[parent]):
            return None
    return result


def _unknown(reason, **metadata):
    result = {"scope": "session_recorded_usage", "source": "codex_session_jsonl",
              "usage_complete": False, "source_verified": False, "usage_status": "unknown",
              "reason": reason, "captured_at": _now(), "billing_usage": False,
              "weekly_quota_usage": False, "limitations": list(_NOTES)}
    result.update(dict.fromkeys(COUNTERS))
    result.update(metadata)
    return result


def _sample(counters, *, session_id, path, line_number, timestamp, turn_id, model, epoch, identity):
    return {"scope": "session_recorded_usage", "source": "codex_session_jsonl",
            "usage_complete": True, "source_verified": True, "usage_status": "complete", "reason": None,
            "session_id": session_id, "source_path": str(path), "source_record": line_number,
            "timestamp": timestamp, "turn_id": turn_id, "model": model, "counter_epoch": epoch,
            "source_identity": identity, "billing_usage": False, "weekly_quota_usage": False,
            "limitations": list(_NOTES), **counters}


def snapshot_session(path, expected_thread_id) -> dict:
    """Extract only metadata/counters from an explicitly identified session file."""
    session_id = _id(expected_thread_id)
    if session_id is None:
        return _unknown("invalid_session_id")
    path = Path(path).expanduser()
    if not path.name.endswith("-" + session_id + ".jsonl"):
        return _unknown("session_filename_mismatch", session_id=session_id)
    if path.is_symlink():
        return _unknown("session_file_is_symlink", session_id=session_id)
    latest = baseline = initial_zero = None
    current_turn = current_model = turn_started_at = None
    current_models, completed_turns, boundaries = set(), {}, {}
    epoch = duplicates = 0
    metadata_verified = False
    malformed = partial = False
    invalid_usage = None
    bytes_read = 0
    try:
        with path.open("rb") as stream:
            initial_stat = os.fstat(stream.fileno())
            identity = {"device": initial_stat.st_dev, "inode": initial_stat.st_ino}
            remaining = initial_stat.st_size
            line_number = 0
            while remaining:
                line = stream.readline(remaining)
                if not line:
                    malformed = True
                    break
                remaining -= len(line)
                bytes_read += len(line)
                line_number += 1
                if not line.strip():
                    continue
                if not line.endswith(b"\n"):
                    partial = True
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    malformed = True
                    break
                if not isinstance(event, dict):
                    malformed = True
                    break
                typ, payload = event.get("type"), event.get("payload")
                if not isinstance(payload, dict):
                    continue
                timestamp = event.get("timestamp")
                if typ == "session_meta":
                    if payload.get("id") != session_id or metadata_verified:
                        return _unknown("session_metadata_mismatch", session_id=session_id)
                    metadata_verified = True
                elif typ == "turn_context" or (typ == "event_msg" and payload.get("type") == "task_started"):
                    turn_id = _safe_label(payload.get("turn_id"))
                    model = _safe_label(payload.get("model"))
                    if turn_id is None:
                        continue
                    if turn_id not in boundaries:
                        boundaries[turn_id] = {"baseline": dict(latest) if latest is not None else None,
                                               "initial_zero": None,
                                               "started_at": payload.get("started_at") if _date(payload.get("started_at")) else timestamp}
                    if turn_id != current_turn:
                        current_models = set()
                        current_model = None
                    current_turn = turn_id
                    if model is not None:
                        current_model = model
                        current_models.add(model)
                    baseline = boundaries[turn_id]["baseline"]
                    initial_zero = boundaries[turn_id]["initial_zero"]
                    turn_started_at = boundaries[turn_id]["started_at"]
                elif typ == "event_msg" and payload.get("type") == "task_complete":
                    turn_id = _safe_label(payload.get("turn_id"))
                    completed_at = payload.get("completed_at") if _date(payload.get("completed_at")) else timestamp
                    if turn_id is not None and _date(completed_at):
                        completed_turns[turn_id] = completed_at
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    # Rate-limit-only notifications carry no token info.
                    if info is None:
                        continue
                    counters = _usage(info.get("total_token_usage") if isinstance(info, dict) else None)
                    if counters is None or _date(timestamp) is None:
                        invalid_usage = "invalid_or_missing_token_usage"
                        continue
                    invalid_usage = None
                    if latest is not None:
                        comparable = [key for key in COUNTERS if latest.get(key) is not None and counters.get(key) is not None]
                        if any(counters[key] < latest[key] for key in comparable):
                            epoch += 1
                        if all(counters.get(key) == latest.get(key) for key in COUNTERS):
                            duplicates += 1
                    latest = _sample(counters, session_id=session_id, path=path, line_number=line_number,
                                     timestamp=timestamp, turn_id=current_turn, model=current_model, epoch=epoch, identity=identity)
                    if current_turn is not None and baseline is None and all(counters[key] == 0 for key in _CORE):
                        # Zero is a valid baseline only when actually present in the source.
                        if boundaries[current_turn]["initial_zero"] is None:
                            boundaries[current_turn]["initial_zero"] = dict(latest)
                        initial_zero = boundaries[current_turn]["initial_zero"]
            final_stat = os.fstat(stream.fileno())
        current_stat = path.stat()
        if (final_stat.st_size < initial_stat.st_size or current_stat.st_ino != initial_stat.st_ino
                or current_stat.st_dev != initial_stat.st_dev):
            return _unknown("session_source_replaced_or_truncated", session_id=session_id)
    except OSError:
        return _unknown("session_source_unavailable", session_id=session_id)
    common = {"session_id": session_id, "source_path": str(path), "source_size": bytes_read,
              "current_turn_id": current_turn, "current_turn_model": current_model,
              "current_turn_started_at": turn_started_at if _date(turn_started_at) else None,
              "current_turn_complete": current_turn in completed_turns if current_turn else False,
              "current_turn_completed_at": completed_turns.get(current_turn),
              "models_observed": sorted(current_models), "duplicate_token_events": duplicates,
              "trailing_partial_line": partial, "captured_at": _now()}
    if not metadata_verified:
        return _unknown("missing_session_metadata", **common)
    if malformed or partial:
        return _unknown("truncated_source" if partial else "malformed_source", **common)
    if invalid_usage is not None:
        return _unknown(invalid_usage, **common)
    if latest is None:
        return _unknown("missing_token_usage", **common)
    result = {**latest, **common}
    result["before_current_turn"] = baseline or initial_zero
    result["before_current_turn_source"] = ("previous_recorded_cumulative" if baseline else
                                             "observed_initial_zero" if initial_zero else None)
    return result


def snapshot_current(*, session_id=None, session_root=None) -> dict:
    """Find only filenames matching the explicit/current session identifier."""
    if session_id is None:
        thread = os.environ.get("CODEX_THREAD_ID")
        session = os.environ.get("CODEX_SESSION_ID")
        if thread and session and thread != session:
            return _unknown("session_environment_mismatch")
        session_id = thread or session
    session_id = _id(session_id)
    if session_id is None:
        return _unknown("missing_or_invalid_current_session_id")
    root = Path(session_root).expanduser() if session_root is not None else Path.home() / ".codex" / "sessions"
    try:
        # Enumerate names, never inspect other chats to guess the latest session.
        matches = list(root.glob("**/rollout-*-" + session_id + ".jsonl"))
    except OSError:
        return _unknown("session_source_unavailable", session_id=session_id)
    if len(matches) != 1:
        return _unknown("session_source_not_found" if not matches else "ambiguous_session_source", session_id=session_id)
    return snapshot_session(matches[0], session_id)


def delta(start, end) -> dict:
    """Subtract verified cumulative counters; never infer missing data as zero."""
    base = {"scope": "same_session_recorded_usage_delta", "source": "codex_session_jsonl",
            "billing_usage": False, "weekly_quota_usage": False, "limitations": list(_NOTES),
            "captured_at": _now()}

    def unknown(reason):
        return {**base, **dict.fromkeys(COUNTERS), "usage_complete": False, "usage_status": "unknown", "reason": reason}

    if not isinstance(start, dict) or not isinstance(end, dict):
        return unknown("missing_snapshot")
    if not start.get("source_verified") or not start.get("usage_complete"):
        return unknown("start_unverified:" + str(start.get("reason") or "missing_data"))
    if not end.get("source_verified") or not end.get("usage_complete"):
        return unknown("end_unverified:" + str(end.get("reason") or "missing_data"))
    if _id(start.get("session_id")) is None or start.get("session_id") != end.get("session_id"):
        return unknown("different_sessions")
    if start.get("source_identity") != end.get("source_identity"):
        return unknown("different_session_sources")
    if start.get("counter_epoch") != end.get("counter_epoch"):
        return unknown("counter_reset")
    before = _usage({key: start.get(key) for key in COUNTERS if start.get(key) is not None})
    after = _usage({key: end.get(key) for key in COUNTERS if end.get(key) is not None})
    if before is None or after is None:
        return unknown("invalid_counters")
    if (not _number(start.get("source_record")) or not _number(end.get("source_record"))
            or end["source_record"] < start["source_record"]):
        return unknown("source_moved_backwards")
    values = {key: after[key] - before[key] if before[key] is not None and after[key] is not None else None for key in COUNTERS}
    if any(value is not None and value < 0 for value in values.values()):
        return unknown("counter_reset_or_negative_delta")
    if _usage({key: value for key, value in values.items() if value is not None}) is None:
        return unknown("inconsistent_counter_delta")
    begin_time, end_time = _date(start.get("timestamp")), _date(end.get("timestamp"))
    if begin_time is None or end_time is None or end_time < begin_time:
        return unknown("invalid_snapshot_timestamps")
    return {**base, **values, "usage_complete": True, "usage_status": "complete", "reason": None,
            "session_id": start["session_id"], "start_turn_id": start.get("turn_id"), "end_turn_id": end.get("turn_id"),
            "model": end.get("model"), "models_observed": end.get("models_observed", []),
            "start_timestamp": start.get("timestamp"), "end_timestamp": end.get("timestamp"),
            "recorded_interval_seconds": round((end_time - begin_time).total_seconds(), 6),
            "cache_detail_complete": values["cached_input_tokens"] is not None,
            "reasoning_detail_complete": values["reasoning_output_tokens"] is not None}


def mark_start(label=None, *, boundary="before_current_turn", snapshot=None, session_id=None, session_root=None) -> dict:
    """Create a serialisable baseline; before-current-turn includes early planning."""
    if boundary not in {"before_current_turn", "now"}:
        raise ValueError("Measurement boundary must be before_current_turn or now.")
    if label is not None and _safe_label(label) is None:
        raise ValueError("Measurement label must be a short identifier.")
    current = snapshot if snapshot is not None else snapshot_current(session_id=session_id, session_root=session_root)
    if not current.get("source_verified"):
        baseline = _unknown("start_source_unverified", session_id=current.get("session_id"))
    elif boundary == "now":
        baseline = {key: value for key, value in current.items() if key != "before_current_turn"}
    elif not current.get("current_turn_id"):
        baseline = _unknown("missing_current_turn", session_id=current.get("session_id"))
    elif not current.get("before_current_turn"):
        baseline = _unknown("missing_verified_pre_turn_baseline", session_id=current.get("session_id"))
    else:
        baseline = dict(current["before_current_turn"])
    return {**baseline, "marker": "start", "label": label, "boundary": boundary, "marked_at": _now(),
            "target_turn_id": current.get("current_turn_id"), "task_started_at": current.get("current_turn_started_at"),
            "pre_marker_planning_included": boundary == "before_current_turn" and baseline.get("source_verified", False),
            "baseline_source": current.get("before_current_turn_source") if boundary == "before_current_turn" else "explicit_snapshot_now"}


def mark_end(start, *, snapshot=None, session_id=None, session_root=None) -> dict:
    """Finish a window. For a complete task, call from outside its finished turn."""
    current = snapshot if snapshot is not None else snapshot_current(session_id=session_id, session_root=session_root)
    result = delta(start, current)
    if start.get("target_turn_id") is not None and start["target_turn_id"] != current.get("current_turn_id"):
        result.update(dict.fromkeys(COUNTERS))
        result.update(usage_complete=False, usage_status="unknown", reason="candidate_turn_changed")
    complete = (current.get("current_turn_complete", False)
                and start.get("target_turn_id") == current.get("current_turn_id"))
    began, ended = _date(start.get("task_started_at")), _date(current.get("current_turn_completed_at"))
    return {**result, "marker": "end", "label": start.get("label"), "marked_at": _now(),
            "target_turn_id": start.get("target_turn_id"), "candidate_turn_complete": complete,
            "task_wall_seconds": round((ended - began).total_seconds(), 6) if complete and began and ended and ended >= began else None,
            "coverage": "completed_candidate_turn" if complete else "through_last_recorded_token_event_only",
            "start_snapshot": start, "end_snapshot": current}
