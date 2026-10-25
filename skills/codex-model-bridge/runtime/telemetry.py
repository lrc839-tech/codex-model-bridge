"""Durable, content-free accounting for actual external model requests.

A pending request is deliberately unknown after a restart. This module never
retries requests and never records prompts, keys, endpoints, or response bodies.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid


_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,199}\Z")
_ID = re.compile(r"[a-f0-9]{32}\Z")
_TOP_TOKENS = frozenset({"prompt_tokens", "input_tokens", "completion_tokens", "output_tokens",
                         "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                         "reasoning_tokens"})
_DETAIL_TOKENS = {"prompt_tokens_details": frozenset({"cached_tokens"}),
                  "input_tokens_details": frozenset({"cached_tokens"}),
                  "completion_tokens_details": frozenset({"reasoning_tokens"}),
                  "output_tokens_details": frozenset({"reasoning_tokens"})}
_TOTALS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")


def _label(value, field, model=False):
    pattern = _MODEL if model else _LABEL
    if (not isinstance(value, str) or not pattern.fullmatch(value)
            or value.lower().startswith(("http:", "https:", "bearer:"))):
        raise ValueError("Telemetry " + field + " must be a short identifier, not content or a URL.")
    return value


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1


def _safe_usage(usage):
    """Keep only recognised numeric token fields; do not preserve vendor text."""
    if not isinstance(usage, dict):
        return {}, False
    clean, valid = {}, True
    for name in _TOP_TOKENS:
        if name in usage:
            if _integer(usage[name]):
                clean[name] = usage[name]
            else:
                valid = False
    for parent, children in _DETAIL_TOKENS.items():
        if parent not in usage:
            continue
        value = usage[parent]
        if not isinstance(value, dict):
            valid = False
            continue
        nested = {}
        for name in children:
            if name in value:
                if _integer(value[name]):
                    nested[name] = value[name]
                else:
                    valid = False
        if nested:
            clean[parent] = nested
    return clean, valid


def _normalise(usage):
    clean, valid = _safe_usage(usage)

    def choose(*values):
        nonlocal valid
        present = [value for value in values if value is not None]
        if not present:
            return None
        if len(set(present)) != 1:
            valid = False
            return None
        return present[0]

    inputs = choose(clean.get("prompt_tokens"), clean.get("input_tokens"))
    outputs = choose(clean.get("completion_tokens"), clean.get("output_tokens"))
    cached = choose(clean.get("prompt_cache_hit_tokens"),
                    clean.get("prompt_tokens_details", {}).get("cached_tokens"),
                    clean.get("input_tokens_details", {}).get("cached_tokens"))
    reasoning = choose(clean.get("reasoning_tokens"),
                       clean.get("completion_tokens_details", {}).get("reasoning_tokens"),
                       clean.get("output_tokens_details", {}).get("reasoning_tokens"))
    total = inputs + outputs if inputs is not None and outputs is not None else None
    if clean.get("total_tokens") is not None and clean["total_tokens"] != total:
        valid = False
    if cached is not None and (inputs is None or cached > inputs):
        valid = False
    if reasoning is not None and (outputs is None or reasoning > outputs):
        valid = False
    miss = clean.get("prompt_cache_miss_tokens")
    if miss is not None and cached is not None and (inputs is None or cached + miss != inputs):
        valid = False
    return {"input_tokens": inputs, "cached_input_tokens": cached, "output_tokens": outputs,
            "reasoning_output_tokens": reasoning, "total_tokens": total}, bool(valid and total is not None)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


class UsageLedger:
    """Append-only JSONL ledger, locked and fsynced for each state transition."""

    def __init__(self, path):
        requested = Path(path).expanduser().absolute()
        requested.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Resolve the containing directory, but never follow the ledger itself.
        self.path = requested.parent.resolve() / requested.name
        os.chmod(self.path.parent, 0o700)
        with self._locked() as stream:
            self._load(stream)

    @contextmanager
    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(self.path), flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_uid != os.getuid()):
                raise ValueError("Telemetry path must be a private, owner-controlled regular file.")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "a+", encoding="utf-8") as stream:
                descriptor = None
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield stream
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _load(stream):
        stream.seek(0)
        started, finished = {}, {}
        for number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or not _ID.fullmatch(event.get("request_id", "")):
                    raise ValueError()
                request_id = event["request_id"]
                if event.get("event") == "start":
                    if set(event) != {"event", "request_id", "provider", "model", "stage", "kind", "time"} or request_id in started:
                        raise ValueError()
                    for key in ("provider", "stage", "kind"):
                        _label(event[key], key)
                    _label(event["model"], "model", model=True)
                    started[request_id] = event
                elif event.get("event") == "finish":
                    if (set(event) != {"event", "request_id", "status", "usage", "usage_complete", "finish_reason", "error_code", "time"}
                            or request_id not in started or request_id in finished
                            or event["status"] not in {"ok", "error"} or not isinstance(event["usage_complete"], bool)):
                        raise ValueError()
                    clean, valid = _safe_usage(event["usage"])
                    if clean != event["usage"] or not valid:
                        raise ValueError()
                    for key in ("finish_reason", "error_code"):
                        if event[key] is not None:
                            _label(event[key], key)
                    finished[request_id] = event
                else:
                    raise ValueError()
                dt.datetime.fromisoformat(event["time"])
            except (ValueError, TypeError, KeyError):
                raise ValueError("Telemetry ledger has an invalid record at line " + str(number) + ".") from None
        return started, finished

    @staticmethod
    def _append(stream, record):
        stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    def start(self, provider: str, model: str, stage: str, kind="chat") -> str:
        labels = {"provider": _label(provider, "provider"), "model": _label(model, "model", model=True),
                  "stage": _label(stage, "stage"), "kind": _label(kind, "kind")}
        with self._locked() as stream:
            started, _ = self._load(stream)
            request_id = uuid.uuid4().hex
            while request_id in started:
                request_id = uuid.uuid4().hex
            self._append(stream, {"event": "start", "request_id": request_id, **labels, "time": _now()})
        return request_id

    def finish(self, request_id, status, usage, finish_reason=None, error_code=None) -> None:
        if status not in {"ok", "error"}:
            raise ValueError("Telemetry status must be ok or error.")
        for field, value in (("finish_reason", finish_reason), ("error_code", error_code)):
            if value is not None:
                _label(value, field)
        clean, safe = _safe_usage(usage)
        _, complete = _normalise(clean)
        with self._locked() as stream:
            started, finished = self._load(stream)
            if request_id not in started:
                raise ValueError("Cannot finish an unknown telemetry request.")
            if request_id in finished:
                raise ValueError("Cannot finish a telemetry request twice.")
            self._append(stream, {"event": "finish", "request_id": request_id, "status": status,
                                  "usage": clean, "usage_complete": bool(safe and complete),
                                  "finish_reason": finish_reason, "error_code": error_code, "time": _now()})

    def summary(self) -> dict:
        with self._locked() as stream:
            started, finished = self._load(stream)
        known = dict.fromkeys(_TOTALS, 0)
        complete_count = 0
        cache_complete = reasoning_complete = True
        for event in finished.values():
            totals, complete = _normalise(event["usage"])
            if not event["usage_complete"] or not complete:
                continue
            complete_count += 1
            for key in _TOTALS:
                if totals[key] is not None:
                    known[key] += totals[key]
            cache_complete = cache_complete and totals["cached_input_tokens"] is not None
            reasoning_complete = reasoning_complete and totals["reasoning_output_tokens"] is not None
        unfinished = len(started) - len(finished)
        unknown = len(started) - complete_count
        complete = unknown == 0
        result = {"requests_started": len(started), "requests_finished": len(finished),
                  "requests_failed": sum(event["status"] == "error" for event in finished.values()),
                  "unfinished_requests": unfinished, "unknown_requests": unknown,
                  "usage_complete": complete, "usage_status": "complete" if complete else "unknown",
                  "known_totals": known,
                  "cache_detail_complete": complete and cache_complete,
                  "reasoning_detail_complete": complete and reasoning_complete}
        result.update({key: known[key] if complete else None for key in _TOTALS})
        if not result["cache_detail_complete"]:
            result["cached_input_tokens"] = None
        if not result["reasoning_detail_complete"]:
            result["reasoning_output_tokens"] = None
        result["unknown_reasons"] = (["unfinished_requests"] if unfinished else []) + (
            ["missing_or_invalid_usage"] if len(finished) > complete_count else [])
        return result
