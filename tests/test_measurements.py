"""Synthetic-session tests. No personal Codex sessions or credentials are read."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from runtime import measurements as m


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def timestamp(second):
    return (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=second)).isoformat()


def record(kind, payload, second):
    return {"timestamp": timestamp(second), "type": kind, "payload": payload}


def context(turn="task-turn", second=3, model="fixture-model"):
    return record("turn_context", {"turn_id": turn, "model": model}, second)


def tokens(inputs=100, outputs=20, cached=60, reasoning=5, second=2):
    usage = {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs}
    if cached is not None:
        usage["cached_input_tokens"] = cached
    if reasoning is not None:
        usage["reasoning_output_tokens"] = reasoning
    return record("event_msg", {"type": "token_count", "info": {"total_token_usage": usage}}, second)


def fixture():
    return [record("session_meta", {"id": SESSION}, 0), context("previous-turn", 1), tokens(),
            record("event_msg", {"type": "task_started", "turn_id": "task-turn", "started_at": timestamp(3)}, 3),
            context(second=4), tokens(140, 30, 90, 7, 5)]


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-measure-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / ("rollout-fixture-" + SESSION + ".jsonl")

    def write(self, events=None, *, path=None, suffix=b""):
        target = path or self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"".join(json.dumps(item).encode() + b"\n" for item in (events if events is not None else fixture())) + suffix)
        return target

    def snapshot(self, events=None):
        self.write(events)
        return m.snapshot_session(self.path, SESSION)

    def test_snapshot_reads_verified_cumulative_and_exposes_no_conversation(self):
        events = fixture()
        events.insert(3, record("response_item", {"role": "user", "content": "PRIVATE_CONVERSATION_FIXTURE"}, 2))
        events.insert(4, record("event_msg", {"type": "item_completed", "content": "PRIVATE_BODY_FIXTURE"}, 2))
        result = self.snapshot(events)
        self.assertTrue(result["source_verified"])
        self.assertEqual((140, 90, 30, 7, 170), tuple(result[key] for key in m.COUNTERS))
        self.assertEqual("task-turn", result["turn_id"])
        self.assertEqual("fixture-model", result["model"])
        self.assertFalse(result["billing_usage"])
        self.assertFalse(result["weekly_quota_usage"])
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_before_turn_baseline_captures_planning_before_marker(self):
        start = m.mark_start("candidate-1", snapshot=self.snapshot())
        self.assertEqual(100, start["input_tokens"])
        self.assertEqual("previous-turn", start["turn_id"])
        self.assertEqual("task-turn", start["target_turn_id"])
        self.assertTrue(start["pre_marker_planning_included"])
        events = fixture() + [tokens(180, 50, 120, 12, 7),
                              record("event_msg", {"type": "task_complete", "turn_id": "task-turn", "completed_at": timestamp(8),
                                                   "last_agent_message": "PRIVATE_FINAL_CONTENT"}, 8)]
        end = m.mark_end(start, snapshot=self.snapshot(events))
        self.assertTrue(end["usage_complete"])
        self.assertEqual((80, 60, 30, 7, 110), tuple(end[key] for key in m.COUNTERS))
        self.assertTrue(end["candidate_turn_complete"])
        self.assertEqual(5, end["task_wall_seconds"])
        self.assertEqual("completed_candidate_turn", end["coverage"])
        self.assertNotIn("PRIVATE_FINAL_CONTENT", json.dumps(end))

    def test_explicit_now_boundary_excludes_prior_planning_and_says_so(self):
        snapshot = self.snapshot()
        start = m.mark_start(boundary="now", snapshot=snapshot)
        self.assertEqual(140, start["input_tokens"])
        self.assertFalse(start["pre_marker_planning_included"])
        self.assertEqual("explicit_snapshot_now", start["baseline_source"])

    def test_repeated_turn_context_does_not_reset_baseline_after_compaction(self):
        events = fixture() + [context(second=6), tokens(180, 50, 120, 12, 7)]
        result = self.snapshot(events)
        self.assertEqual(100, m.mark_start(snapshot=result)["input_tokens"])
        self.assertEqual(timestamp(3), result["current_turn_started_at"])

    def test_no_verified_prior_usage_does_not_assume_zero(self):
        events = [record("session_meta", {"id": SESSION}, 0), context(second=1), tokens(second=2)]
        result = m.mark_start(snapshot=self.snapshot(events))
        self.assertFalse(result["source_verified"])
        self.assertIsNone(result["total_tokens"])
        self.assertEqual("missing_verified_pre_turn_baseline", result["reason"])

    def test_observed_initial_zero_is_a_verified_fresh_baseline(self):
        events = [record("session_meta", {"id": SESSION}, 0), context(second=1),
                  tokens(0, 0, 0, 0, 2), tokens(20, 4, 0, 1, 3)]
        result = m.mark_start(snapshot=self.snapshot(events))
        self.assertTrue(result["source_verified"])
        self.assertEqual(0, result["total_tokens"])
        self.assertEqual("observed_initial_zero", result["baseline_source"])

    def test_duplicate_events_use_latest_counter_not_sum(self):
        events = fixture()
        events += [copy.deepcopy(events[-1]), copy.deepcopy(events[-1])]
        result = self.snapshot(events)
        self.assertEqual(170, result["total_tokens"])
        self.assertEqual(2, result["duplicate_token_events"])
        self.assertEqual(50, m.delta(m.mark_start(snapshot=result), result)["total_tokens"])

    def test_cache_and_reasoning_are_not_added_to_total(self):
        result = self.snapshot()
        changes = m.delta(m.mark_start(snapshot=result), result)
        self.assertEqual(50, changes["total_tokens"])
        self.assertEqual(changes["input_tokens"] + changes["output_tokens"], changes["total_tokens"])
        self.assertEqual(30, changes["cached_input_tokens"])
        self.assertEqual(2, changes["reasoning_output_tokens"])

    def test_optional_subcounts_absent_are_unknown_not_zero(self):
        events = [record("session_meta", {"id": SESSION}, 0), context("previous-turn", 1), tokens(cached=None, reasoning=None),
                  context(second=3), tokens(140, 30, None, None, 4)]
        result = self.snapshot(events)
        changes = m.delta(m.mark_start(snapshot=result), result)
        self.assertTrue(changes["usage_complete"])
        self.assertEqual(50, changes["total_tokens"])
        self.assertIsNone(changes["cached_input_tokens"])
        self.assertIsNone(changes["reasoning_output_tokens"])

    def test_different_session_snapshots_cannot_be_subtracted(self):
        result = self.snapshot()
        start = m.mark_start(snapshot=result)
        changed = {**result, "session_id": OTHER}
        changes = m.delta(start, changed)
        self.assertEqual("different_sessions", changes["reason"])
        self.assertIsNone(changes["total_tokens"])

    def test_negative_delta_is_unknown_even_with_same_epoch(self):
        result = self.snapshot()
        start = m.mark_start(snapshot=result)
        changed = {**result, "input_tokens": 80, "output_tokens": 10, "cached_input_tokens": 50,
                   "reasoning_output_tokens": 1, "total_tokens": 90}
        changes = m.delta(start, changed)
        self.assertFalse(changes["usage_complete"])
        self.assertEqual("counter_reset_or_negative_delta", changes["reason"])

    def test_internal_counter_reset_is_detected_even_if_end_later_exceeds_start(self):
        first = self.snapshot()
        start = m.mark_start(snapshot=first)
        events = fixture() + [tokens(0, 0, 0, 0, 6), tokens(300, 50, 200, 10, 7)]
        result = self.snapshot(events)
        self.assertEqual(1, result["counter_epoch"])
        changes = m.delta(start, result)
        self.assertEqual("counter_reset", changes["reason"])
        self.assertIsNone(changes["total_tokens"])

    def test_missing_usage_and_invalid_total_are_unknown(self):
        events = fixture()
        events[-1]["payload"]["info"]["total_token_usage"]["total_tokens"] = 999
        result = self.snapshot(events)
        self.assertFalse(result["usage_complete"])
        self.assertIsNone(result["total_tokens"])
        self.assertEqual("invalid_or_missing_token_usage", result["reason"])
        result = self.snapshot([record("session_meta", {"id": SESSION}, 0), context()])
        self.assertEqual("missing_token_usage", result["reason"])

    def test_rate_limit_only_event_does_not_invalidate_verified_usage(self):
        result = self.snapshot(fixture() + [record("event_msg", {"type": "token_count", "info": None}, 6)])
        self.assertTrue(result["usage_complete"])
        self.assertEqual(170, result["total_tokens"])

    def test_partial_and_malformed_sources_are_unknown_without_echoing_content(self):
        self.write(suffix=b'{"PRIVATE_PARTIAL_FIXTURE":')
        result = m.snapshot_session(self.path, SESSION)
        self.assertEqual("truncated_source", result["reason"])
        self.assertTrue(result["trailing_partial_line"])
        self.assertNotIn("PRIVATE_PARTIAL_FIXTURE", json.dumps(result))
        self.write(suffix=b'{"PRIVATE_MALFORMED_FIXTURE":\n')
        result = m.snapshot_session(self.path, SESSION)
        self.assertEqual("malformed_source", result["reason"])
        self.assertNotIn("PRIVATE_MALFORMED_FIXTURE", json.dumps(result))

    def test_complete_json_without_newline_is_not_assumed_committed(self):
        events = fixture()
        self.write(events[:-1], suffix=json.dumps(events[-1]).encode())
        self.assertEqual("truncated_source", m.snapshot_session(self.path, SESSION)["reason"])

    def test_wrong_filename_is_rejected_without_opening_another_session(self):
        self.write()
        with mock.patch.object(Path, "open", side_effect=AssertionError("must not read other session")):
            result = m.snapshot_session(self.path, OTHER)
        self.assertEqual("session_filename_mismatch", result["reason"])

    def test_session_metadata_identity_is_verified(self):
        events = fixture()
        events[0]["payload"]["id"] = OTHER
        self.assertEqual("session_metadata_mismatch", self.snapshot(events)["reason"])
        self.assertEqual("missing_session_metadata", self.snapshot(fixture()[1:])["reason"])

    def test_session_lookup_uses_only_explicit_matching_filename(self):
        own = self.root / "2026" / "01" / "01" / self.path.name
        self.write(path=own)
        foreign = own.parent / ("rollout-fixture-" + OTHER + ".jsonl")
        foreign.write_text("PRIVATE_OTHER_SESSION_INVALID_JSON")
        original_open = Path.open
        opened = []

        def guarded_open(path, *args, **kwargs):
            if path == foreign:
                raise AssertionError("other session contents must not be read")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            result = m.snapshot_current(session_id=SESSION, session_root=self.root)
        self.assertTrue(result["source_verified"])
        self.assertEqual([own], opened)

    def test_missing_current_id_does_not_fall_back_to_latest_chat(self):
        self.write()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = m.snapshot_current(session_root=self.root)
        self.assertEqual("missing_or_invalid_current_session_id", result["reason"])
        self.assertIsNone(result["total_tokens"])

    def test_environment_id_mismatch_is_unknown(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": SESSION, "CODEX_SESSION_ID": OTHER}):
            result = m.snapshot_current(session_root=self.root)
        self.assertEqual("session_environment_mismatch", result["reason"])

    def test_current_env_id_and_unambiguous_file_are_required(self):
        self.write()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": SESSION, "CODEX_SESSION_ID": SESSION}):
            self.assertTrue(m.snapshot_current(session_root=self.root)["source_verified"])
        self.write(path=self.root / "copy" / self.path.name)
        self.assertEqual("ambiguous_session_source", m.snapshot_current(session_id=SESSION, session_root=self.root)["reason"])
        self.assertEqual("session_source_not_found", m.snapshot_current(session_id=OTHER, session_root=self.root)["reason"])

    def test_symlink_source_is_rejected(self):
        self.write()
        source = self.root / "linked" / self.path.name
        source.parent.mkdir()
        source.symlink_to(self.path)
        self.assertEqual("session_file_is_symlink", m.snapshot_session(source, SESSION)["reason"])

    def test_mark_end_inside_running_turn_is_explicitly_partial(self):
        snapshot = self.snapshot()
        result = m.mark_end(m.mark_start(snapshot=snapshot), snapshot=snapshot)
        self.assertTrue(result["usage_complete"])
        self.assertFalse(result["candidate_turn_complete"])
        self.assertIsNone(result["task_wall_seconds"])
        self.assertEqual("through_last_recorded_token_event_only", result["coverage"])

    def test_new_turn_cannot_be_folded_into_candidate_task(self):
        snapshot = self.snapshot()
        start = m.mark_start(snapshot=snapshot)
        result = self.snapshot(fixture() + [context("unrelated-turn", 7), tokens(200, 60, 130, 20, 8)])
        end = m.mark_end(start, snapshot=result)
        self.assertFalse(end["usage_complete"])
        self.assertEqual("candidate_turn_changed", end["reason"])
        self.assertIsNone(end["total_tokens"])

    def test_unverified_or_replaced_source_is_not_a_valid_delta(self):
        snapshot = self.snapshot()
        start = m.mark_start(snapshot=snapshot)
        for changed, reason in [({**snapshot, "source_identity": {"device": 1, "inode": 2}}, "different_session_sources"),
                                ({**snapshot, "source_record": 1}, "source_moved_backwards"),
                                ({**snapshot, "source_verified": False, "reason": "missing_data"}, "end_unverified:missing_data")]:
            with self.subTest(reason=reason):
                end = m.delta(start, changed)
                self.assertEqual(reason, end["reason"])
                self.assertIsNone(end["total_tokens"])

    def test_numeric_bool_negative_or_impossible_subcounts_are_invalid(self):
        for key, value in [("input_tokens", True), ("output_tokens", -1), ("cached_input_tokens", 999),
                           ("reasoning_output_tokens", 999)]:
            events = fixture()
            events[-1]["payload"]["info"]["total_token_usage"][key] = value
            with self.subTest(key=key):
                self.assertFalse(self.snapshot(events)["usage_complete"])

    def test_invalid_marker_label_or_boundary_is_rejected(self):
        for kwargs in [{"label": "PRIVATE PROMPT CONTENT"}, {"boundary": "assume-zero"}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                m.mark_start(snapshot={}, **kwargs)


if __name__ == "__main__":
    unittest.main()
