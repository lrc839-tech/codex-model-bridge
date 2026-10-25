"""Trusted local API relay with a durable campaign budget. Never run in a candidate.

The real credential stays in this process. Agents receive per-run random relay
credentials. Only Chat Completions is exposed; search, fetch and redirects fail.
Streaming is forwarded while numeric usage is collected from its final chunk.
"""
from __future__ import annotations
import datetime as dt
import http.server
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from urllib import request, error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/codex-model-bridge"))
from runtime.telemetry import UsageLedger
from runtime.providers import ProviderClient, _NoRedirect

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SSE_LINE_BYTES = 1024 * 1024
MAX_REQUEST_SECONDS = 120
_ALLOWED_FIELDS = {"messages", "model", "tools", "tool_choice", "stream", "stream_options",
                   "thinking", "reasoning_effort", "max_tokens", "max_completion_tokens", "n",
                   "temperature", "top_p", "frequency_penalty", "presence_penalty", "stop",
                   "response_format", "seed", "parallel_tool_calls", "user"}
_NATIVE_FIELDS = {"messages", "model", "system", "tools", "tool_choice", "stream", "thinking", "max_tokens",
                  "temperature", "top_p", "top_k", "stop_sequences", "metadata",
                  "dsh_session_log", "dsh_plugin_packages"}
NATIVE_MESSAGES_URL = "https://api.deepseek.com/anthropic/v1/messages"


class BudgetStopped(Exception):
    pass


def _text_content(content, *, native=False):
    if content is None or isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        typ = block.get("type")
        if typ == "text" and isinstance(block.get("text"), str):
            continue
        if native and typ in {"tool_use", "thinking", "redacted_thinking"}:
            continue
        if native and typ == "tool_result" and _text_content(block.get("content"), native=True):
            continue
        return False
    return True


def _candidate_payload(payload, *, native=False):
    """Accept common client fields, reject paid/server tools and hidden options."""
    if (not isinstance(payload, dict) or set(payload) - (_NATIVE_FIELDS if native else _ALLOWED_FIELDS)
            or not isinstance(payload.get("messages"), list) or not payload["messages"]
            or not all(isinstance(message, dict) and _text_content(message.get("content"), native=native)
                       for message in payload["messages"])):
        raise ValueError("invalid_request")
    if native:
        if "system" in payload and not _text_content(payload["system"], native=True):
            raise ValueError("nontext_content_denied")
        tools = payload.get("tools", [])
        if (not isinstance(tools, list) or any(not isinstance(tool, dict)
                or set(tool) - {"name", "description", "parameters", "input_schema", "strict", "cache_control", "type"}
                or tool.get("type") not in {None, "function", "custom"}
                or not isinstance(tool.get("name"), str) for tool in tools)):
            raise ValueError("server_tools_denied")
        choice = payload.get("tool_choice")
        if choice is not None and (not isinstance(choice, dict)
                or set(choice) - {"type", "name", "disable_parallel_tool_use"}
                or choice.get("type") not in {"auto", "none", "any", "tool"}):
            raise ValueError("server_tools_denied")
        value = payload.get("max_tokens", 8192)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("invalid_request")
        if "stream" in payload and not isinstance(payload["stream"], bool):
            raise ValueError("invalid_request")
        result = {**payload, "model": "deepseek-flash", "thinking": {"type": "disabled"}, "max_tokens": min(value, 8192)}
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        raise ValueError("invalid_tools")
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) - {"type", "function"}
                or tool.get("type") != "function" or not isinstance(tool.get("function"), dict)):
            raise ValueError("server_tools_denied")
    choice = payload.get("tool_choice")
    if choice is not None and choice not in ("auto", "none", "required"):
        if (not isinstance(choice, dict) or set(choice) != {"type", "function"}
                or choice.get("type") != "function" or not isinstance(choice.get("function"), dict)
                or set(choice["function"]) != {"name"} or not isinstance(choice["function"]["name"], str)):
            raise ValueError("server_tools_denied")
    if "stream" in payload and not isinstance(payload["stream"], bool):
        raise ValueError("invalid_request")
    if "n" in payload and (isinstance(payload["n"], bool) or payload["n"] != 1):
        raise ValueError("multiple_completions_denied")
    limits = []
    for field in ("max_tokens", "max_completion_tokens"):
        if field in payload:
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("invalid_request")
            limits.append(value)
    result = dict(payload)
    result.update(model="deepseek-flash", thinking={"type": "disabled"}, n=1,
                  max_tokens=min([8192] + limits))
    result.pop("reasoning_effort", None)
    result.pop("max_completion_tokens", None)
    result.pop("stream_options", None)
    if result.get("stream"):
        result["stream_options"] = {"include_usage": True}
    # NaN/Infinity must fail locally, not create a billable/unknown request.
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


def _merge_native_usage(merged, event):
    usage = event.get("usage")
    if event.get("type") == "message_start" and isinstance(event.get("message"), dict):
        usage = event["message"].get("usage")
    if not isinstance(usage, dict):
        return
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        if field in usage:
            merged[field] = usage[field]


def _native_usage(merged):
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in merged.values()):
        return None
    # Native Messages excludes cache reads/creation from its input_tokens.
    # Require the explicit input and output; optional cache fields default to 0
    # according to the native schema, rather than adding them to CC counters.
    if "input_tokens" not in merged or "output_tokens" not in merged:
        return None
    cached = merged.get("cache_read_input_tokens", 0)
    inputs = merged["input_tokens"] + cached + merged.get("cache_creation_input_tokens", 0)
    outputs = merged["output_tokens"]
    return {"prompt_tokens": inputs, "completion_tokens": outputs, "total_tokens": inputs + outputs,
            "prompt_cache_hit_tokens": cached, "prompt_cache_miss_tokens": inputs - cached}


def _read_timeout(response, seconds):
    """urllib's socket timeout is per read; tighten it to the wall-clock remainder."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(max(0.001, seconds))


def _bounded_chunks(response, deadline):
    """read1 returns available bytes, avoiding a trickle-resetting read(n) loop."""
    reader = getattr(response, "read1", response.read)
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        _read_timeout(response, remaining)
        chunk = reader(min(65536, MAX_RESPONSE_BYTES - total + 1))
        if time.monotonic() > deadline:
            raise TimeoutError()
        if not chunk:
            return
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError("response_limit")
        yield chunk


def _bounded_lines(response, deadline):
    pending = b""
    for chunk in _bounded_chunks(response, deadline):
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            if len(line) + 1 > MAX_SSE_LINE_BYTES:
                raise ValueError("response_line_limit")
            yield line + b"\n"
        if len(pending) > MAX_SSE_LINE_BYTES:
            raise ValueError("response_line_limit")
    if pending:
        yield pending


class CampaignBudget:
    """Single trusted coordinator; conservative peak prices and CNY conversion.

    Conversion is a declared ceiling of 8 CNY/USD, not an exchange-rate quote.
    Unknown/in-flight usage stops further submissions instead of assuming zero.
    """
    def __init__(self, root, *, max_runs=18, max_seconds=7200, max_cny=10):
        if (isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) or value <= 0 for value in (max_seconds, max_cny))):
            raise ValueError("Campaign limits must be finite positive values")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ledger = UsageLedger(self.root / "api-usage.jsonl")
        self.path = self.root / "budget.json"
        self.lock = threading.RLock()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "started_epoch": None, "runs_started": 0, "stop_reason": None,
            "limits": {"runs": max_runs, "seconds": max_seconds, "estimated_api_cny": max_cny},
            "pricing": {"source": "https://api-docs.deepseek.com/quick_start/pricing/",
                        "retrieved_date": "2026-10-25", "usd_to_cny_budget_ceiling": 8,
                        "usd_per_million_peak": {"input_cached": .006, "input_uncached": .3, "output": 1.2},
                        "kind": "conservative_estimate_not_invoice"}}
        # A restart can tighten the campaign limits; it cannot silently relax them.
        limits = self.state["limits"]
        self.max_runs = min(max_runs, limits["runs"])
        self.max_seconds = min(max_seconds, limits["seconds"])
        self.max_cny = min(max_cny, limits["estimated_api_cny"])
        self.state["limits"] = {"runs": self.max_runs, "seconds": self.max_seconds, "estimated_api_cny": self.max_cny}
        self._save()

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    def estimate(self):
        summary = self.ledger.summary()
        if not summary["usage_complete"]:
            return None
        inputs, outputs = summary["input_tokens"], summary["output_tokens"]
        cached = summary["cached_input_tokens"] or 0
        return ((inputs-cached)*.3 + cached*.006 + outputs*1.2) / 1_000_000 * 8

    def remaining_seconds(self):
        started = self.state["started_epoch"]
        return self.max_seconds if started is None else max(0, self.max_seconds - max(0, time.time() - started))

    def check(self, *, reserve_cny=0, new_run=False):
        if (isinstance(reserve_cny, bool) or not isinstance(reserve_cny, (int, float))
                or not math.isfinite(reserve_cny) or reserve_cny < 0):
            raise ValueError("Budget reservation must be a finite nonnegative value")
        with self.lock:
            cost = self.estimate()
            reason = self.state.get("stop_reason")
            if self.state["started_epoch"] is not None and self.remaining_seconds() <= 0:
                reason = "wall_time_limit"
            elif cost is None:
                reason = "unknown_or_pending_api_usage"
            elif cost + reserve_cny >= self.max_cny:
                reason = "estimated_api_budget_limit"
            elif new_run and self.state["runs_started"] >= self.max_runs:
                reason = "task_run_limit"
            if reason:
                self.state["stop_reason"] = reason
                self._save()
                raise BudgetStopped(reason)
            return {"allowed": True, "estimated_api_cny": cost, "runs_started": self.state["runs_started"]}

    def start_run(self):
        with self.lock:
            result = self.check(new_run=True)
            if self.state["started_epoch"] is None:
                self.state["started_epoch"] = time.time()
                self.state["started_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            self.state["runs_started"] += 1
            self._save()
            return {**result, "runs_started": self.state["runs_started"]}


class APIBroker:
    def __init__(self, profile, budget):
        self.client = ProviderClient(profile)
        if self.client.provider != "deepseek" or self.client.model != "deepseek-flash":
            raise ValueError("Pilot requires the selected official deepseek-flash profile")
        self.budget = budget
        self.routes = {}
        self.serial = threading.RLock()
        self._activity = threading.Condition(threading.RLock())
        self._active = {}
        self._closing = False
        self._closed = False
        self._started = False
        self.upstream_opener = request.build_opener(_NoRedirect())
        broker = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reject(self, status, code):
                body = json.dumps({"error": {"message": code, "type": "pilot_broker", "code": code}}).encode()
                try:
                    self.send_response(status); self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                except OSError:
                    pass

            def do_GET(self):
                self.reject(403, "route_denied")

            def do_POST(self):
                native = self.path in {"/v1/messages", "/messages"}
                authorization = self.headers.get("Authorization", "")
                token = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else None
                if native and self.headers.get("x-api-key"):
                    token = self.headers["x-api-key"]
                route = broker.routes.get(token)
                if route is None or self.path not in {"/v1/chat/completions", "/chat/completions", "/v1/messages", "/messages"}:
                    self.reject(403, "route_denied"); return
                if native != (route["arm"] == "deepseek_harness"):
                    self.reject(403, "arm_protocol_mismatch"); return
                self.connection.settimeout(10)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 2*1024*1024:
                        raise ValueError()
                    payload = _candidate_payload(json.loads(self.rfile.read(length)), native=native)
                    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
                    upstream_headers = {"Authorization": "Bearer " + broker.client._api_key, "Content-Type": "application/json"}
                    if native:
                        upstream_headers.pop("Authorization")
                        upstream_headers["x-api-key"] = broker.client._api_key
                        upstream_headers["anthropic-version"] = "2023-06-01"
                        for header in ("anthropic-beta", "x-deepseek-harness-user-id", "x-deepseek-harness-session-id"):
                            value = self.headers.get(header)
                            if value is not None:
                                if len(value) > 512 or any(ord(char) < 32 or ord(char) > 126 for char in value):
                                    raise ValueError()
                                upstream_headers[header] = value
                except (ValueError, TypeError, OverflowError, OSError):
                    self.reject(400, "invalid_request"); return
                with broker.serial:
                    if broker._closing:
                        self.reject(503, "broker_closing"); return
                    if broker.budget.state["started_epoch"] is None:
                        self.reject(409, "campaign_not_started"); return
                    try:
                        # UTF-8 bytes bound token inputs conservatively; no cache assumed for reserve.
                        broker.budget.check(reserve_cny=(len(body)*.3+payload["max_tokens"]*1.2)/1e6*8)
                    except BudgetStopped:
                        self.reject(429, "pilot_budget_stopped"); return
                    ledger = route["ledger"]
                    kind = "messages" if native else "chat"
                    try:
                        active = broker._begin_request(route, kind)
                    except BudgetStopped:
                        self.reject(503, "broker_closing"); return
                    usage = finish = failure = None
                    native_totals, native_final_usage = {}, False
                    headers_sent = disconnected = False
                    deadline = active["deadline"]
                    try:
                        upstream = request.Request(NATIVE_MESSAGES_URL if native else broker.client.base_url + "/chat/completions",
                                                   data=body, headers=upstream_headers, method="POST")
                        response = broker.upstream_opener.open(upstream, timeout=max(0.001, deadline-time.monotonic()))
                        broker._attach_response(active, response)
                        with response:
                            if payload.get("stream"):
                                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                                self.send_header("Connection", "close"); self.end_headers(); headers_sent = True
                                done = False
                                for line in _bounded_lines(response, deadline):
                                    forwarded = line
                                    if line.startswith(b"data:"):
                                        chunk = line[5:].strip()
                                        if chunk == b"[DONE]":
                                            done = True
                                        else:
                                            try:
                                                event = json.loads(chunk)
                                                if isinstance(event, dict):
                                                    if native:
                                                        _merge_native_usage(native_totals, event)
                                                        if event.get("type") == "message_delta":
                                                            native_final_usage = isinstance(event.get("usage"), dict) and "output_tokens" in event["usage"]
                                                            if isinstance(event.get("delta"), dict):
                                                                finish = event["delta"].get("stop_reason") or finish
                                                        if event.get("type") == "message_stop":
                                                            done = True
                                                            if native_final_usage:
                                                                broker._publish_usage(active, _native_usage(native_totals))
                                                    else:
                                                        if event.get("usage") is not None:
                                                            usage = event["usage"]
                                                            broker._publish_usage(active, usage)
                                                        if event.get("choices") and isinstance(event["choices"][0], dict):
                                                            finish = event["choices"][0].get("finish_reason") or finish
                                                    if "error" in event:
                                                        failure = "upstream_api_error"
                                                        forwarded = b'data: {"error":{"type":"pilot_broker","code":"upstream_api_error"}}\n\n'
                                            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                                                pass
                                    if not disconnected:
                                        try:
                                            self.wfile.write(forwarded); self.wfile.flush()
                                        except OSError:
                                            disconnected = True
                                    # Drain through the usage tail even when the client disconnected.
                                    if done and not line.strip():
                                        break
                                if native:
                                    usage = _native_usage(native_totals) if done and native_final_usage else None
                                if usage is not None:
                                    broker._publish_usage(active, usage)
                                if not done:
                                    failure = failure or "incomplete_stream"
                                if disconnected:
                                    failure = failure or "client_disconnected"
                            else:
                                data = b"".join(_bounded_chunks(response, deadline))
                                event = json.loads(data)
                                if not isinstance(event, dict):
                                    raise ValueError("invalid_response")
                                if native:
                                    _merge_native_usage(native_totals, event)
                                    usage = _native_usage(native_totals)
                                    finish = event.get("stop_reason")
                                else:
                                    usage = event.get("usage")
                                    if event.get("choices") and isinstance(event["choices"][0], dict):
                                        finish = event["choices"][0].get("finish_reason")
                                if "error" in event:
                                    if usage is not None:
                                        broker._publish_usage(active, usage)
                                    failure = "upstream_api_error"
                                    self.reject(502, failure)
                                else:
                                    if usage is not None:
                                        broker._publish_usage(active, usage)
                                    self.send_response(200); self.send_header("Content-Type", "application/json")
                                    self.send_header("Connection", "close"); self.end_headers(); headers_sent = True
                                    self.wfile.write(data); self.wfile.flush()
                    except Exception as exc:
                        if isinstance(exc, error.HTTPError):
                            exc.close()
                        failure = failure or "upstream_or_client_failure"
                        if not headers_sent:
                            self.reject(502, failure)
                    finally:
                        if finish not in {None, "stop", "length", "tool_calls", "content_filter", "end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal"}:
                            finish = "other"
                        broker._finish_request(active, usage=usage, finish_reason=finish, failure=failure)
                        self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def route(self, arm, run_root):
        if arm not in {"codex_bridge", "deepseek_harness"}:
            raise ValueError("Invalid relay arm")
        token = secrets.token_hex(24)
        self.routes[token] = {"arm": arm, "ledger": UsageLedger(Path(run_root)/"external-usage.jsonl")}
        return {"base_url": self.base_url, "api_key": token, "model": "deepseek-flash"}

    def revoke(self, token):
        self.routes.pop(token, None)

    def _begin_request(self, route, kind):
        with self._activity:
            if self._closing:
                raise BudgetStopped("broker_closing")
            global_id = self.budget.ledger.start("deepseek", "deepseek-flash", route["arm"], kind=kind)
            try:
                local_id = route["ledger"].start("deepseek", "deepseek-flash", route["arm"], kind=kind)
            except Exception:
                self.budget.ledger.finish(global_id, "error", None, error_code="local_ledger_start_failed")
                raise
            active = {"global_id": global_id, "local_id": local_id, "ledger": route["ledger"],
                      "deadline": time.monotonic() + min(MAX_REQUEST_SECONDS, self.budget.remaining_seconds()),
                      "response": None, "known_usage": None, "finished": False, "cancelled": False,
                      "finished_targets": set()}
            self._active[global_id] = active
            self._activity.notify_all()
            return active

    def _attach_response(self, active, response):
        with self._activity:
            active["response"] = response
            cancelled = active["cancelled"] or active["finished"]
        if cancelled:
            self._interrupt_response(active)
            raise OSError("broker_closed")

    def _publish_usage(self, active, usage):
        # Only complete response/tail usage is published, never native message_start.
        with self._activity:
            if not active["finished"]:
                active["known_usage"] = usage

    def _finish_request(self, active, *, usage=None, finish_reason=None, failure=None):
        with self._activity:
            if active["finished"]:
                return
            final_usage = usage if usage is not None else active["known_usage"]
            for name, ledger, request_id in (("global", self.budget.ledger, active["global_id"]),
                                             ("local", active["ledger"], active["local_id"])):
                if name not in active["finished_targets"]:
                    ledger.finish(request_id, "error" if failure else "ok", final_usage,
                                  finish_reason=finish_reason, error_code=failure)
                    active["finished_targets"].add(name)
            active["finished"] = True
            self._active.pop(active["global_id"], None)
            self._activity.notify_all()

    @staticmethod
    def _interrupt_response(active):
        response = active.get("response")
        if response is None:
            return
        sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        else:
            # In-memory/fake responses have no socket; close is nonblocking there.
            try:
                response.close()
            except (OSError, ValueError):
                pass

    @property
    def pending_requests(self):
        with self._activity:
            return len(self._active)

    def settle(self, timeout_seconds=8):
        """Wait for existing requests only. Never submit/retry or stop the budget."""
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds < 0):
            raise ValueError("Settle timeout must be a finite nonnegative value")
        deadline = time.monotonic() + timeout_seconds
        with self._activity:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._activity.wait(remaining)
            active = len(self._active)
        summary = self.budget.ledger.summary()
        return {"settled": active == 0 and summary["unfinished_requests"] == 0,
                "pending_requests": summary["unfinished_requests"], "active_requests": active,
                "requests_started": summary["requests_started"], "requests_finished": summary["requests_finished"],
                "usage_complete": summary["usage_complete"]}

    def start(self):
        self._started = True
        self.thread.start()
        return self

    def close(self, timeout_seconds=MAX_REQUEST_SECONDS):
        """Bounded drain, then durable unknown/error pairs before main can exit."""
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds < 0):
            raise ValueError("Close timeout must be a finite nonnegative value")
        with self._activity:
            if self._closed:
                return self.settle(0)
            self._closing = True
        deadline = time.monotonic() + min(timeout_seconds, self.budget.remaining_seconds())
        if self._started:
            self.server.shutdown()
        self.server.server_close()
        self.settle(max(0, deadline-time.monotonic()))
        with self._activity:
            remaining = list(self._active.values())
            for active in remaining:
                active["cancelled"] = True
        for active in remaining:
            self._interrupt_response(active)
            self._finish_request(active, failure="broker_shutdown_incomplete")
        if self._started:
            self.thread.join(timeout=1)
        with self._activity:
            self._closed = True
        return self.settle(0)
