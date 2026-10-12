"""Behavior tests for the decisions.jsonl ledger helper.

These use only temporary projects and never touch a real paper project.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "record_decision.py"
PYTHON = Path(sys.executable)


def load_module():
    spec = importlib.util.spec_from_file_location("drl_record_decision_tests", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


record = load_module()


def valid_entry(**overrides):
    entry = {"id": "d-0001", "timestamp": "2026-10-12T06:00:00+00:00", "stage": "G5",
             "spec_version": "spec-v1", "decision": "keep the minimum viable component",
             "reason": "component-level ablation is testable", "evidence": ["C:/p/audit/a.md"],
             "affected_runs": ["run-001"], "revalidation": "re-run G5 after the change"}
    entry.update(overrides)
    return entry


class RecordDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drl-ledger-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.ledger = record.ledger_path(str(self.project))

    def cli(self, *args):
        return subprocess.run([str(PYTHON), "-I", "-B", str(SOURCE), "--project", str(self.project), *args],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_append_creates_one_valid_line(self):
        clean, warnings = record.validate_entry(valid_entry())
        record.append_entry(self.ledger, clean)
        self.assertEqual(warnings, [])
        lines = self.ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0]), clean)

    def test_append_keeps_existing_lines(self):
        for identifier in ("d-a", "d-b"):
            record.append_entry(self.ledger, record.validate_entry(valid_entry(id=identifier))[0])
        self.assertEqual([json.loads(line)["id"] for line in
                          self.ledger.read_text(encoding="utf-8").splitlines()], ["d-a", "d-b"])

    def test_missing_field_is_rejected(self):
        for field in record.FIELDS:
            entry = valid_entry()
            del entry[field]
            with self.assertRaises(record.DecisionError, msg=field):
                record.validate_entry(entry)

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(record.DecisionError):
            record.validate_entry(valid_entry(extra="nope"))

    def test_evidence_must_not_be_empty(self):
        with self.assertRaises(record.DecisionError):
            record.validate_entry(valid_entry(evidence=[]))
        with self.assertRaises(record.DecisionError):
            record.validate_entry(valid_entry(evidence=["  "]))

    def test_affected_runs_may_be_empty(self):
        clean, _ = record.validate_entry(valid_entry(affected_runs=[]))
        self.assertEqual(clean["affected_runs"], [])

    def test_non_stage_value_warns_but_is_kept(self):
        clean, warnings = record.validate_entry(valid_entry(stage="handoff"))
        self.assertEqual(clean["stage"], "handoff")
        self.assertTrue(any("G0-G9" in note for note in warnings))

    def test_duplicate_id_is_rejected_by_cli(self):
        first = self.cli("--id", "d-dup", "--stage", "G5", "--spec-version", "spec-v1",
                         "--decision", "one", "--reason", "because",
                         "--evidence", "C:/p/a.md", "--revalidation", "none needed")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.cli("--id", "d-dup", "--stage", "G5", "--spec-version", "spec-v1",
                          "--decision", "two", "--reason", "because",
                          "--evidence", "C:/p/b.md", "--revalidation", "none needed")
        self.assertEqual(second.returncode, 2, second.stderr)
        self.assertIn("already present", json.loads(second.stdout)["error"])
        self.assertEqual(len(self.ledger.read_text(encoding="utf-8").splitlines()), 1)

    def test_generated_id_and_timestamp_are_used_when_omitted(self):
        result = self.cli("--stage", "G2", "--spec-version", "spec-v1",
                          "--decision", "freeze the main line", "--reason", "evidence is enough",
                          "--evidence", "C:/p/design/d.md", "--revalidation", "reopen G2 if the model changes")
        self.assertEqual(result.returncode, 0, result.stderr)
        appended = json.loads(result.stdout)["appended"]
        self.assertTrue(appended["id"].startswith("d-"))
        self.assertIn("T", appended["timestamp"])

    def test_missing_required_flag_fails_without_writing(self):
        result = self.cli("--stage", "G5", "--spec-version", "spec-v1", "--decision", "x",
                          "--reason", "y", "--evidence", "C:/p/a.md")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertFalse(self.ledger.exists())

    def test_check_only_reports_without_writing(self):
        result = self.cli("--check-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["entries"], 0)
        self.assertFalse(self.ledger.exists())

    def test_corrupt_line_is_located(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text('{"id": "d-a"}\nnot json\n', encoding="utf-8")
        result = self.cli("--check-only")
        self.assertEqual(result.returncode, 2)
        self.assertIn("line 2", json.loads(result.stdout)["error"])

    def test_duplicate_id_in_an_existing_ledger_is_detected(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(valid_entry())
        self.ledger.write_text(line + "\n" + line + "\n", encoding="utf-8")
        result = self.cli("--check-only")
        self.assertEqual(result.returncode, 2)
        self.assertIn("duplicate decision id", json.loads(result.stdout)["error"])

    def test_relative_project_is_rejected(self):
        with self.assertRaises(record.DecisionError):
            record.ledger_path("relative/project")


if __name__ == "__main__":
    unittest.main(verbosity=2)
