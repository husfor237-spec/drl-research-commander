"""Behavior tests against the installed gauge, using only synthetic telemetry.

Run with the configured Python executable and -I -B. No telemetry reader is
stubbed: the real sibling context-gauge parses each temporary JSONL fixture.

This module therefore requires the sibling `context-gauge` skill to be installed
next to this skill. When it is absent (for example in a fresh clone of this
repository), every test here is skipped with an explicit reason instead of
failing, so the suite still reports a meaningful result. See README "依赖".
"""

import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "context_policy.py"
GAUGE = ROOT.parent / "context-gauge" / "scripts" / "context_gauge.py"
PYTHON = Path(sys.executable)
SID = "synthetic-commander"
NOW_DT = datetime.datetime(2026, 10, 12, 8, tzinfo=datetime.timezone.utc)
NOW = NOW_DT.timestamp()
INVALID_MODEL_PAYLOADS = ({}, {"model": ""}, {"model": " \t\n"}, {"model": 17})
GAUGE_MISSING_REASON = ("requires the sibling context-gauge skill at " + str(GAUGE) +
                        "; install it next to this skill as the README describes")


def load_policy():
    spec = importlib.util.spec_from_file_location("drl_policy_behavior_tests", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load_policy()


def stamp(seconds=0):
    return (NOW_DT + datetime.timedelta(seconds=seconds)).isoformat()


def event(kind, payload, timestamp=stamp(-10)):
    result = {"type": kind, "payload": payload}
    if timestamp is not None:
        result["timestamp"] = timestamp
    return result


def token_event(used=790_000, window=1_000_000, timestamp=stamp(-5), total=9_999_999):
    info = {"last_token_usage": {"input_tokens": used, "cached_input_tokens": 100},
            "total_token_usage": {"input_tokens": total, "total_tokens": total + 123}}
    if window is not None:
        info["model_context_window"] = window
    return event("event_msg", {"type": "token_count", "info": info}, timestamp)


def compaction(seconds=-4):
    return event("compacted", {"window_number": 2}, stamp(seconds))


@unittest.skipUnless(GAUGE.is_file(), GAUGE_MISSING_REASON)
class ContextPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drl-policy-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.path = self.base / ("rollout-" + SID + ".jsonl")
        self.home = self.base / "codex-home"
        self.home.mkdir()
        self.environment = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.assertTrue(GAUGE.is_absolute())

    def rows(self, used=790_000, window=1_000_000, model="gpt-6.1-sol"):
        return [event("session_meta", {"id": SID, "cwd": str(self.base)}, stamp(-12)),
                event("turn_context", {"model": model}, stamp(-10)),
                token_event(used, window)]

    def write(self, rows, path=None):
        target = path or self.path
        target.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return target

    def read(self, rows=None, session_id=SID, path=None):
        if rows is not None:
            self.write(rows, path)
        return policy.read_policy(session_id, str(path or self.path), gauge_path=str(GAUGE), now=NOW)

    def assert_unknown(self, report):
        self.assertEqual(report["rotation"]["action"], "unknown", report)
        self.assertIsNone(report["fill_percent"], report)

    def cli(self, *args):
        return subprocess.run([str(PYTHON), "-I", "-B", str(SOURCE), "--gauge-path", str(GAUGE), *args],
                              cwd=self.base, env=os.environ.copy(), capture_output=True,
                              text=True, encoding="utf-8", timeout=20)

    def test_79_percent_continues(self):
        report = self.read(self.rows(790_000))
        self.assertTrue(report["binding_verified"])
        self.assertEqual(report["snapshot_status"], "latest_report")
        self.assertEqual(report["fill_percent"], 79)
        self.assertEqual(report["rotation"]["action"], "continue")

    def test_80_percent_prepares(self):
        report = self.read(self.rows(800_000))
        self.assertEqual(report["fill_percent"], 80)
        self.assertEqual(report["rotation"]["action"], "prepare")

    def test_89_percent_prepares(self):
        report = self.read(self.rows(890_000))
        self.assertEqual(report["fill_percent"], 89)
        self.assertEqual(report["rotation"]["action"], "prepare")

    def test_90_percent_requires_handoff(self):
        report = self.read(self.rows(900_000))
        self.assertEqual(report["fill_percent"], 90)
        self.assertEqual(report["rotation"]["action"], "required")

    def test_79_999999_percent_does_not_round_up_to_prepare(self):
        report = self.read(self.rows(79_999_999, 100_000_000))
        self.assertGreater(report["fill_percent"], 79.999)
        self.assertLess(report["fill_percent"], 80)
        self.assertEqual(report["rotation"]["action"], "continue")

    def test_89_999999_percent_does_not_round_up_to_required(self):
        report = self.read(self.rows(89_999_999, 100_000_000))
        self.assertLess(report["fill_percent"], 90)
        self.assertEqual(report["rotation"]["action"], "prepare")

    def test_over_100_percent_preserves_actual_ratio(self):
        report = self.read(self.rows(1_250_000))
        self.assertEqual(report["fill_percent"], 125)
        self.assertEqual(report["used_tokens"], 1_250_000)
        self.assertEqual(report["rotation"]["action"], "required")

    def test_zero_usage_is_a_valid_measured_snapshot(self):
        report = self.read(self.rows(0))
        self.assertEqual(report["snapshot_status"], "latest_report")
        self.assertEqual(report["fill_percent"], 0)
        self.assertEqual(report["rotation"]["action"], "continue")

    def test_first_compaction_requests_revalidation_with_new_low_usage(self):
        report = self.read(self.rows(950_000) + [compaction(), token_event(100_000, timestamp=stamp(-2))])
        self.assertEqual(report["compactions"], 1)
        self.assertTrue(report["revalidate_after_compaction"])
        self.assertEqual(report["snapshot_status"], "latest_report")
        self.assertEqual(report["fill_percent"], 10)
        self.assertEqual(report["rotation"]["action"], "continue")

    def test_second_compaction_requires_handoff_without_usage(self):
        report = self.read(self.rows()[:2] + [compaction(-4), compaction(-2)])
        self.assertTrue(report["binding_verified"])
        self.assertEqual(report["compactions"], 2)
        self.assertTrue(report["revalidate_after_compaction"])
        self.assertEqual(report["rotation"]["action"], "required")
        self.assertIn("second_compaction_requires_handoff", report["rotation"]["reasons"])
        self.assertIsNone(report["fill_percent"])

    def test_pre_compaction_high_usage_is_not_a_new_snapshot(self):
        report = self.read(self.rows(990_000) + [compaction()])
        self.assertEqual(report["compactions"], 1)
        self.assertTrue(report["revalidate_after_compaction"])
        self.assertEqual(report["snapshot_status"], "awaiting_post_compaction_usage")
        self.assertEqual(report["usage_timestamp"], stamp(-5))
        self.assert_unknown(report)

    def test_new_turn_without_usage_cannot_trigger_from_old_high_usage(self):
        # Equal timestamps deliberately require event order, not just timestamp comparison.
        for timestamp in (stamp(-2), stamp(-5), None):
            with self.subTest(timestamp=timestamp):
                report = self.read(self.rows(990_000) + [event("turn_context", {"model": "gpt-6.1-sol"}, timestamp)])
                self.assertEqual(report["snapshot_status"], "awaiting_usage")
                self.assert_unknown(report)

    def test_future_missing_and_invalid_usage_times_are_not_current(self):
        for timestamp in (stamp(60), None, "not-an-iso-timestamp"):
            with self.subTest(timestamp=timestamp):
                rows = self.rows(950_000) + [token_event(990_000, timestamp=timestamp)]
                report = self.read(rows)
                self.assertEqual(report["snapshot_status"], "timestamp_unknown")
                self.assertEqual(report["usage_timestamp"], timestamp)
                self.assert_unknown(report)

    def test_unknown_model_with_reported_window_is_usable(self):
        report = self.read(self.rows(720, 900, model="synthetic-unlisted-model"))
        self.assertEqual(report["model"], "synthetic-unlisted-model")
        self.assertEqual(report["context_window"], 900)
        self.assertEqual(report["window_source"], "codex")
        self.assertEqual(report["fill_percent"], 80)
        self.assertEqual(report["rotation"]["action"], "prepare")

    def test_missing_window_never_uses_fixed_model_fallback(self):
        for model in ("gpt-6.1-sol", "synthetic-unlisted-model"):
            with self.subTest(model=model):
                report = self.read(self.rows(9_000_000, None, model))
                self.assertEqual(report["used_tokens"], 9_000_000)
                self.assertIsNone(report["context_window"])
                self.assertEqual(report["window_source"], "unknown")
                self.assert_unknown(report)

    def test_invalid_windows_cannot_trigger_a_ratio(self):
        for window in (0, -1, True, "1000000", 1_000_000.0):
            with self.subTest(window=window):
                report = self.read(self.rows(9_000_000, window))
                self.assertIsNone(report["context_window"])
                self.assert_unknown(report)

    def test_same_model_new_usage_cannot_borrow_an_older_window(self):
        report = self.read(self.rows(100_000) + [token_event(990_000, None, stamp(-1))])
        self.assertEqual(report["used_tokens"], 990_000)
        self.assertIsNone(report["context_window"])
        self.assert_unknown(report)

    def test_model_switch_clears_old_usage_and_window(self):
        report = self.read(self.rows(990_000) + [event("turn_context", {"model": "gpt-6-astra"}, stamp(-2))])
        self.assertEqual(report["model"], "gpt-6-astra")
        self.assertIsNone(report["context_window"])
        self.assertIsNone(report["used_tokens"])
        self.assertIsNone(report["usage_timestamp"])
        self.assert_unknown(report)

    def test_model_switch_without_new_window_does_not_inherit_old_window(self):
        report = self.read(self.rows(990_000) + [event("turn_context", {"model": "gpt-6-astra"}, stamp(-3)),
                                               token_event(990_000, None, stamp(-2))])
        self.assertEqual(report["model"], "gpt-6-astra")
        self.assertIsNone(report["context_window"])
        self.assert_unknown(report)

    def test_latest_invalid_model_without_new_usage_clears_historical_identity(self):
        for payload in INVALID_MODEL_PAYLOADS:
            with self.subTest(payload=payload):
                report = self.read(self.rows(990_000) + [event("turn_context", payload, stamp(-4))])
                self.assertTrue(report["ok"], report)
                self.assertTrue(report["binding_verified"], report)
                self.assertIsNone(report["model"], report)
                self.assertIsNone(report["used_tokens"], report)
                self.assertIsNone(report["context_window"], report)
                self.assertIsNone(report["usage_timestamp"], report)
                self.assertEqual(report["snapshot_status"], "unavailable", report)
                self.assertEqual(report["latest_turn_timestamp"], stamp(-4), report)
                self.assertIn("model_identity_unknown", report["rotation"]["reasons"])
                self.assert_unknown(report)

    def test_latest_invalid_model_with_new_90_percent_usage_cannot_decide_by_ratio(self):
        for payload in INVALID_MODEL_PAYLOADS:
            with self.subTest(payload=payload):
                report = self.read(self.rows(100_000) + [event("turn_context", payload, stamp(-4)),
                                                       token_event(180_000, 200_000, stamp(-3))])
                self.assertTrue(report["ok"], report)
                self.assertTrue(report["binding_verified"], report)
                self.assertIsNone(report["model"], report)
                self.assertEqual(report["used_tokens"], 180_000, report)
                self.assertEqual(report["context_window"], 200_000, report)
                self.assertEqual(report["window_source"], "codex", report)
                self.assertEqual(report["usage_timestamp"], stamp(-3), report)
                self.assertEqual(report["snapshot_status"], "latest_report", report)
                self.assertIn("model_identity_unknown", report["rotation"]["reasons"])
                self.assertNotIn("context_at_or_above_80_percent", report["rotation"]["reasons"])
                self.assertNotIn("context_at_or_above_90_percent", report["rotation"]["reasons"])
                self.assert_unknown(report)

    def test_valid_model_recovers_ratio_only_with_its_own_new_usage_and_window(self):
        for payload in INVALID_MODEL_PAYLOADS:
            for model in ("gpt-6.1-sol", "synthetic-recovered-model"):
                with self.subTest(payload=payload, model=model):
                    rows = self.rows(990_000) + [event("turn_context", payload, stamp(-4)),
                                               token_event(180_000, 200_000, stamp(-3))]
                    invalid = self.read(rows)
                    self.assertIsNone(invalid["model"], invalid)
                    self.assert_unknown(invalid)
                    rows.append(event("turn_context", {"model": model}, stamp(-2)))
                    waiting = self.read(rows)
                    self.assertEqual(waiting["model"], model, waiting)
                    self.assertIsNone(waiting["used_tokens"], waiting)
                    self.assertIsNone(waiting["context_window"], waiting)
                    self.assert_unknown(waiting)
                    rows.append(token_event(180_000, None, stamp(-1)))
                    no_window = self.read(rows)
                    self.assertEqual(no_window["model"], model, no_window)
                    self.assertEqual(no_window["used_tokens"], 180_000, no_window)
                    self.assertIsNone(no_window["context_window"], no_window)
                    self.assert_unknown(no_window)
                    rows.append(token_event(450_000, 500_000, stamp(0)))
                    recovered = self.read(rows)
                    self.assertEqual(recovered["model"], model, recovered)
                    self.assertEqual(recovered["used_tokens"], 450_000, recovered)
                    self.assertEqual(recovered["context_window"], 500_000, recovered)
                    self.assertEqual(recovered["window_source"], "codex", recovered)
                    self.assertEqual(recovered["snapshot_status"], "latest_report", recovered)
                    self.assertEqual(recovered["fill_percent"], 90, recovered)
                    self.assertEqual(recovered["rotation"]["action"], "required", recovered)
                    self.assertNotIn("model_identity_unknown", recovered["rotation"]["reasons"])

    def test_second_compaction_requires_handoff_even_when_latest_model_is_invalid(self):
        for payload in INVALID_MODEL_PAYLOADS:
            for new_usage in (False, True):
                with self.subTest(payload=payload, new_usage=new_usage):
                    rows = self.rows(100_000) + [event("turn_context", payload, stamp(-4)),
                                               compaction(-3), compaction(-2)]
                    if new_usage:
                        rows.append(token_event(180_000, 200_000, stamp(-1)))
                    report = self.read(rows)
                    self.assertTrue(report["binding_verified"], report)
                    self.assertIsNone(report["model"], report)
                    self.assertIsNone(report["fill_percent"], report)
                    self.assertEqual(report["compactions"], 2, report)
                    self.assertTrue(report["revalidate_after_compaction"], report)
                    self.assertEqual(report["rotation"]["action"], "required", report)
                    self.assertIn("model_identity_unknown", report["rotation"]["reasons"])
                    self.assertIn("second_compaction_requires_handoff", report["rotation"]["reasons"])
                    self.assertNotIn("context_at_or_above_90_percent", report["rotation"]["reasons"])

    def test_new_model_uses_its_new_window_ratio(self):
        for used, action, percent in ((180_000, "required", 90), (150_000, "continue", 75)):
            with self.subTest(used=used):
                report = self.read(self.rows(100_000) + [event("turn_context", {"model": "synthetic-new-model"}, stamp(-3)),
                                                       token_event(used, 200_000, stamp(-2))])
                self.assertEqual(report["context_window"], 200_000)
                self.assertEqual(report["fill_percent"], percent)
                self.assertEqual(report["rotation"]["action"], action)

    def test_current_input_does_not_mix_with_old_or_cumulative_input(self):
        report = self.read(self.rows(990_000) + [token_event(200_000, total=99_000_000, timestamp=stamp(-1))])
        self.assertEqual(report["used_tokens"], 200_000)
        self.assertEqual(report["fill_percent"], 20)
        self.assertEqual(report["rotation"]["action"], "continue")

    def test_cumulative_only_input_is_not_current_usage(self):
        rows = self.rows()[:2] + [event("event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 99_000_000}, "model_context_window": 1_000_000}})]
        report = self.read(rows)
        self.assertIsNone(report["used_tokens"])
        self.assert_unknown(report)

    def test_missing_or_wrong_metadata_cannot_rotate_anyone(self):
        for metadata in (None, {"id": "someone-else"}, {"id": 123}, {}, "malformed"):
            with self.subTest(metadata=metadata):
                rows = self.rows(990_000)[1:] + [compaction(-4), compaction(-2)]
                if metadata is not None:
                    rows.insert(0, event("session_meta", metadata))
                report = self.read(rows)
                self.assertFalse(report["binding_verified"])
                self.assertFalse(report["ok"])
                self.assert_unknown(report)

    def test_conflicting_metadata_fields_do_not_establish_binding(self):
        rows = self.rows(990_000)
        rows[0] = event("session_meta", {"session_id": SID, "id": "someone-else"})
        report = self.read(rows + [compaction(-4), compaction(-2)])
        self.assertFalse(report["binding_verified"], report)
        self.assert_unknown(report)

    def test_bad_metadata_after_valid_metadata_invalidates_binding(self):
        for metadata in ({}, {"id": 123}, {"id": ""}):
            with self.subTest(metadata=metadata):
                report = self.read(self.rows(990_000) + [event("session_meta", metadata), compaction(-4), compaction(-2)])
                self.assertFalse(report["binding_verified"], report)
                self.assert_unknown(report)

    def test_mixed_sessions_cannot_trigger_either_sessions_rotation(self):
        rows = self.rows(990_000) + [event("session_meta", {"id": "someone-else"}), compaction(-4), compaction(-2)]
        self.write(rows)
        for requested in (SID, "someone-else"):
            with self.subTest(requested=requested):
                report = self.read(session_id=requested)
                self.assertFalse(report["binding_verified"])
                self.assert_unknown(report)

    def test_message_strings_cannot_fabricate_compaction_events(self):
        # Values equal to event names survive JSON encoding as literal quoted tags.
        report = self.read(self.rows(100_000) + [event("response_item", {"type": "message", "content": [
            {"type": "input_text", "text": "compacted"}]}),
            event("event_msg", {"type": "user_message", "message": "compacted"})])
        self.assertEqual(report["compactions"], 0)
        self.assertFalse(report["revalidate_after_compaction"])
        self.assertEqual(report["snapshot_status"], "latest_report", report)
        self.assertEqual(report["rotation"]["action"], "continue", report)

    def test_message_shaped_payload_cannot_fabricate_metadata(self):
        rows = self.rows(990_000)[1:] + [compaction(-4), compaction(-2)]
        rows.insert(0, event("response_item", {"id": SID, "text": "session_meta"}))
        report = self.read(rows)
        self.assertFalse(report["binding_verified"])
        self.assert_unknown(report)

    def test_message_shaped_token_count_cannot_replace_structural_usage(self):
        fake = token_event(990_000, timestamp=stamp(-1))
        fake["type"] = "response_item"
        report = self.read(self.rows(100_000) + [fake])
        self.assertEqual(report["used_tokens"], 100_000, report)
        self.assertEqual(report["fill_percent"], 10, report)
        self.assertEqual(report["rotation"]["action"], "continue", report)

    def test_missing_explicit_path_cannot_read_a_recent_session(self):
        sessions = self.home / "sessions"
        sessions.mkdir()
        self.write(self.rows(990_000) + [compaction(-4), compaction(-2)], sessions / ("rollout-" + SID + ".jsonl"))
        report = self.read(path=self.base / "missing.jsonl")
        self.assertFalse(report["ok"])
        self.assertEqual(report["rotation"]["reasons"], ["explicit_session_not_found"])
        self.assert_unknown(report)

    def test_cli_missing_session_id_cannot_fallback_to_recent_session(self):
        sessions = self.home / "sessions"
        sessions.mkdir()
        self.write(self.rows(990_000) + [compaction(-4), compaction(-2)], sessions / ("rollout-" + SID + ".jsonl"))
        result = self.cli("--session-id", "not-found")
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["rotation"]["reasons"], ["explicit_session_not_found"])
        self.assert_unknown(report)

    def test_cli_json_has_only_drl_policy_and_leaves_all_inputs_unchanged(self):
        # The subprocess uses its real clock, whereas direct calls receive NOW.
        rows = self.rows(790_000)
        cli_now = datetime.datetime.now(datetime.timezone.utc)
        for index, row in enumerate(rows):
            row["timestamp"] = (cli_now - datetime.timedelta(seconds=30 - index)).isoformat()
        self.write(rows)
        sentinel = self.home / "unrelated.json"
        sentinel.write_bytes(b'{"keep": "unchanged"}\r\n')
        inputs = {path: path.read_bytes() for path in (SOURCE, GAUGE, self.path, sentinel)}
        tree_before = {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        caches_before = {str(p) for directory in (SOURCE.parent, GAUGE.parent) for p in directory.rglob("*.pyc")}
        result = self.cli("--session-id", SID, "--session", str(self.path))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["policy"], {"prepare_fill_percent": 80, "required_fill_percent": 90,
                                            "compaction_limit": 2, "policy_is_heuristic": True})
        self.assertEqual(report["rotation"]["action"], "continue")
        self.assertEqual(report["bound_by"], "explicit")
        self.assertEqual(report["session_id"], SID)
        serialized = json.dumps(report, ensure_ascii=False).lower()
        for forbidden in ("retention", "degradation", "capability", "能力保留", "60%", "75%"):
            self.assertNotIn(forbidden, serialized)

        def check_thresholds(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if "threshold" in key or "fill_percent" in key and key != "fill_percent":
                        self.assertNotIn(item, (60, 75, 0.6, 0.75))
                    check_thresholds(item)
            elif isinstance(value, list):
                for item in value:
                    check_thresholds(item)

        check_thresholds(report)
        for path, original in inputs.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        self.assertEqual({str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}, tree_before)
        self.assertFalse(list(self.base.rglob("__pycache__")))
        self.assertEqual({str(p) for directory in (SOURCE.parent, GAUGE.parent) for p in directory.rglob("*.pyc")}, caches_before)


class PolicyConfigurationTests(unittest.TestCase):
    """Threshold configuration and reader-compatibility checks.

    These do not call the installed gauge, so they run even in a fresh clone.
    """

    def test_defaults_match_the_documented_80_90_2_policy(self):
        self.assertEqual(policy.normalize_policy(),
                         {"prepare_fill_percent": 80, "required_fill_percent": 90,
                          "compaction_limit": 2, "policy_is_heuristic": True})

    def test_overrides_are_applied_and_stay_marked_heuristic(self):
        merged = policy.normalize_policy({"prepare_fill_percent": 70,
                                          "required_fill_percent": 85,
                                          "compaction_limit": 3})
        self.assertEqual(merged["prepare_fill_percent"], 70)
        self.assertEqual(merged["required_fill_percent"], 85)
        self.assertEqual(merged["compaction_limit"], 3)
        self.assertIs(merged["policy_is_heuristic"], True)

    def test_thresholds_must_be_ordered(self):
        for bad in ({"prepare_fill_percent": 90, "required_fill_percent": 90},
                    {"prepare_fill_percent": 95, "required_fill_percent": 90}):
            with self.assertRaises(ValueError):
                policy.normalize_policy(bad)

    def test_thresholds_and_limit_must_stay_in_range(self):
        for bad in ({"prepare_fill_percent": 0}, {"prepare_fill_percent": 101},
                    {"required_fill_percent": -1}, {"required_fill_percent": "90"},
                    {"compaction_limit": 0}, {"compaction_limit": 1.5}):
            with self.assertRaises(ValueError, msg=str(bad)):
                policy.normalize_policy(bad)

    def test_cli_rejects_invalid_thresholds_before_touching_telemetry(self):
        with tempfile.TemporaryDirectory(prefix="drl-policy-cli-") as base:
            result = subprocess.run([str(PYTHON), "-I", "-B", str(SOURCE),
                                     "--session-id", SID, "--prepare-percent", "95",
                                     "--required-percent", "90"],
                                    cwd=base, capture_output=True, text=True,
                                    encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertEqual(list(Path(base).iterdir()), [])

    def test_reader_missing_a_used_keyword_is_rejected(self):
        class Reader:
            @staticmethod
            def find_session(session_id=None, path=None):  # prefer_recent dropped
                return None

            @staticmethod
            def scan_session(path, max_history=40):
                return {}

            @staticmethod
            def snapshot_status(st, now=None):
                return {}

        with self.assertRaises(ValueError) as caught:
            policy.check_gauge_interface(Reader)
        self.assertIn("prefer_recent", str(caught.exception))

    def test_reader_accepting_kwargs_is_tolerated(self):
        class Reader:
            @staticmethod
            def find_session(**kwargs):
                return None

            @staticmethod
            def scan_session(path, **kwargs):
                return {}

            @staticmethod
            def snapshot_status(st, **kwargs):
                return {}

        policy.check_gauge_interface(Reader)  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
