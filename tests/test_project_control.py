"""Ownership, handoff and transaction tests; all project data is temporary.

Run records are inert fixtures. No command stored in a run record is executed.
Concurrent callers are isolated Python processes, never threads changing cwd.
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "project_control.py"
PYTHON = Path(sys.executable)
OWNER = "synthetic-outgoing"
SUCCESSOR = "synthetic-incoming"


def load_control():
    spec = importlib.util.spec_from_file_location("drl_control_behavior_tests", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load_control()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProjectControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="drl-control-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "paper-project"
        self.project.mkdir()
        self.directory = self.project / ".codex-run" / "drl-research"
        self.directory.mkdir(parents=True)
        self.control_path = self.directory / "control.json"
        self.lock_path = self.directory / ".control.lock"
        self.state = self.directory / "state.md"
        self.state.write_text("# Canonical state\nContinue evaluation of the existing job.\n", encoding="utf-8")
        self.authorization = self.base / "human-authorization.txt"
        self.authorization.write_text("Synthetic user request: research this paper; schedule the agreed experiments.\n", encoding="utf-8")
        self.evidence = self.base / "evaluation-evidence.json"
        self.evidence.write_text('{"synthetic": true, "observed_status": "running"}\n', encoding="utf-8")
        self.manifest_path = self.base / "handoff-manifest.json"
        self.manifest = {"schema_version": 1, "project_root": str(self.project),
                         "outgoing_session_id": OWNER, "owner_epoch": 1,
                         "state_file": str(self.state), "authorization_evidence_file": str(self.authorization),
                         "next_steps": ["Inspect the existing job log and checkpoint before scheduling."],
                         "open_tasks": ["Evaluate the existing run"], "active_runs": [], "unknowns": [],
                         "evidence_files": [{"path": str(p), "sha256": digest(p)}
                                            for p in (self.state, self.authorization, self.evidence)]}
        self.config = self.project / "synthetic-config.json"
        self.config.write_text('{"seed": 7, "training_is_disabled": true}\n', encoding="utf-8")
        self.record_path = self.base / "run-record.json"
        self.record = {"run_id": "unique-run", "config_path": str(self.config),
                       "working_directory": str(self.project),
                       "command": ["synthetic-trainer-must-never-execute", "--fixture-only"],
                       "log_path": str(self.project / "existing-job.log")}
        self.write_json(self.record_path, self.record)
        self.call("init", owner=OWNER)

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def call(self, action, **options):
        return control.perform(str(self.project), action, **options)

    def stored(self):
        return json.loads(self.control_path.read_text(encoding="utf-8"))

    def snapshot_files(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob("*") if p.is_file()}

    def reject(self, action, message=None, **options):
        before = self.snapshot_files()
        if message is None:
            with self.assertRaises(control.ControlError):
                self.call(action, **options)
        else:
            with self.assertRaisesRegex(control.ControlError, message):
                self.call(action, **options)
        self.assertEqual(self.snapshot_files(), before, "a rejected operation must not partially commit")
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(list(self.directory.glob(".control-*.tmp")))

    def prepare(self, manifest=None):
        self.write_json(self.manifest_path, manifest if manifest is not None else self.manifest)
        result = self.call("prepare-handoff", owner=OWNER, manifest=str(self.manifest_path))
        return result["control"]["handoff"]["token"]

    def register(self):
        token = self.prepare()
        self.call("register-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        return token

    def review_manifest(self, path=None):
        """Review fixture meaning and retain the hash of those exact bytes.

        Call before any simulated drift or delayed acknowledgement. Never obtain
        the reviewed hash from control.json when sending the acknowledgement.
        """
        path = path or self.manifest_path
        reviewed_bytes = path.read_bytes()
        manifest = json.loads(reviewed_bytes)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["project_root"], str(self.project))
        self.assertEqual(manifest["outgoing_session_id"], OWNER)
        self.assertEqual(manifest["owner_epoch"], 1)
        self.assertEqual(manifest["state_file"], str(self.state))
        self.assertEqual(manifest["authorization_evidence_file"], str(self.authorization))
        expected_steps = list(self.manifest["next_steps"])
        if path != self.manifest_path:
            expected_steps.append("Review the repaired evidence package: " + path.name)
        self.assertEqual(manifest["next_steps"], expected_steps)
        self.assertEqual(manifest["open_tasks"], self.manifest["open_tasks"])
        self.assertEqual(manifest["unknowns"], self.manifest["unknowns"])
        runs = self.stored()["runs"]
        self.assertEqual(len(manifest["active_runs"]), len(set(manifest["active_runs"])))
        self.assertTrue(set(manifest["active_runs"]).issubset(runs))
        self.assertTrue({run_id for run_id, run in runs.items()
                         if run["status"] in ("claimed", "running", "unknown")}
                        .issubset(manifest["active_runs"]))
        self.assertEqual({item["path"] for item in manifest["evidence_files"]},
                         {str(self.state), str(self.authorization), str(self.evidence)})
        for item in manifest["evidence_files"]:
            self.assertEqual(item["sha256"], digest(Path(item["path"])))
        self.assertTrue(self.state.read_text(encoding="utf-8").startswith("# Canonical state\n"))
        self.assertEqual(self.authorization.read_text(encoding="utf-8"),
                         "Synthetic user request: research this paper; schedule the agreed experiments.\n")
        evidence = json.loads(self.evidence.read_text(encoding="utf-8"))
        self.assertIs(evidence["synthetic"], True)
        self.assertEqual(evidence["observed_status"], "running")
        return hashlib.sha256(reviewed_bytes).hexdigest()

    def acknowledge(self):
        token = self.register()
        reviewed = self.review_manifest()
        self.call("acknowledge-handoff", successor=SUCCESSOR, token=token, manifest_sha256=reviewed)
        self.assertEqual(self.stored()["handoff"]["acknowledged_manifest_sha256"], reviewed)
        return token

    def activate(self):
        token = self.acknowledge()
        self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        return token

    def claim(self, owner=OWNER):
        return self.call("claim-run", owner=owner, record=str(self.record_path))

    def cli(self, *args):
        return subprocess.run([str(PYTHON), "-I", "-B", str(SOURCE), "--project", str(self.project), *args],
                              cwd=self.base, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def revised_manifest(self, name="revised-handoff.json", **changes):
        manifest = copy.deepcopy(self.manifest)
        for item in manifest["evidence_files"]:
            item["sha256"] = digest(Path(item["path"]))
        manifest["next_steps"].append("Review the repaired evidence package: " + name)
        manifest.update(changes)
        path = self.base / name
        self.write_json(path, manifest)
        return path

    def test_refresh_recovers_ack_failure_after_evidence_change(self):
        self.claim()
        self.call("update-run", owner=OWNER, run_id="unique-run", status="running", pid=54321)
        self.manifest["active_runs"] = ["unique-run"]
        token = self.register()
        reviewed = self.review_manifest()
        previous = copy.deepcopy(self.stored())
        self.evidence.write_text('{"synthetic": true, "observed_status": "running", "new_observation": 1}\n', encoding="utf-8")
        self.reject("acknowledge-handoff", "checksum mismatch", successor=SUCCESSOR, token=token,
                    manifest_sha256=reviewed)
        revised = self.revised_manifest()
        note = "Repair evidence checksums after the successor rejected the package."
        inputs = {p: p.read_bytes() for p in (self.manifest_path, revised, self.state, self.authorization, self.evidence)}
        result = self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(revised), note=note)
        stored = self.stored()
        self.assertEqual(result["control"], stored)
        self.assertEqual(stored["owner_session_id"], OWNER)
        self.assertEqual(stored["epoch"], previous["epoch"])
        self.assertFalse(stored["dispatch_enabled"])
        self.assertEqual(stored["runs"], previous["runs"])
        handoff = stored["handoff"]
        self.assertEqual(handoff["token"], token)
        self.assertEqual(handoff["successor_session_id"], SUCCESSOR)
        self.assertEqual(handoff["phase"], "registered")
        self.assertEqual(handoff["prepared_at"], previous["handoff"]["prepared_at"])
        self.assertEqual(handoff["manifest_path"], str(revised))
        self.assertEqual(handoff["manifest_sha256"], digest(revised))
        self.assertNotEqual(handoff["manifest_sha256"], previous["handoff"]["manifest_sha256"])
        self.assertEqual(len(handoff["revisions"]), 1)
        revision = handoff["revisions"][0]
        self.assertEqual(revision["manifest_path"], str(self.manifest_path))
        self.assertEqual(revision["manifest_sha256"], previous["handoff"]["manifest_sha256"])
        self.assertEqual(revision["reason"], note)
        self.assertTrue(revision["replaced_at"])
        for path, original in inputs.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        self.reject("activate-successor", "not verified and acknowledged", owner=OWNER, successor=SUCCESSOR, token=token)
        reviewed = self.review_manifest(revised)
        self.call("acknowledge-handoff", successor=SUCCESSOR, token=token, manifest_sha256=reviewed)
        self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertEqual(self.stored()["owner_session_id"], SUCCESSOR)
        self.assertEqual(self.stored()["runs"], previous["runs"])
        self.reject("claim-run", "already claimed", owner=SUCCESSOR, record=str(self.record_path))

    def test_refresh_invalidates_prior_ack_and_requires_successor_to_confirm_again(self):
        token = self.acknowledge()
        previous = copy.deepcopy(self.stored()["handoff"])
        self.assertIn("acknowledged_at", previous)
        self.assertEqual(previous["acknowledged_manifest_sha256"], digest(self.manifest_path))
        self.state.write_text("# Canonical state\nRevised next step: inspect the new evaluation evidence.\n", encoding="utf-8")
        self.reject("activate-successor", "checksum mismatch", owner=OWNER, successor=SUCCESSOR, token=token)
        revised = self.revised_manifest()
        self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(revised),
                  note="Invalidate the old acknowledgement because canonical state changed.")
        handoff = self.stored()["handoff"]
        self.assertEqual(handoff["phase"], "registered")
        self.assertNotIn("acknowledged_at", handoff)
        self.assertNotIn("acknowledged_manifest_sha256", handoff)
        self.assertEqual(handoff["successor_session_id"], SUCCESSOR)
        self.assertEqual(handoff["token"], token)
        self.assertFalse(self.stored()["dispatch_enabled"])
        self.reject("activate-successor", "not verified and acknowledged", owner=OWNER, successor=SUCCESSOR, token=token)
        # Re-registering the same successor also must not recreate its old ack.
        self.call("register-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.reject("activate-successor", "not verified and acknowledged", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertNotIn("acknowledged_manifest_sha256", self.stored()["handoff"])
        reviewed = self.review_manifest(revised)
        self.call("acknowledge-handoff", successor=SUCCESSOR, token=token, manifest_sha256=reviewed)
        self.assertEqual(self.stored()["handoff"]["phase"], "acknowledged")
        self.assertIn("acknowledged_at", self.stored()["handoff"])
        self.assertEqual(self.stored()["handoff"]["acknowledged_manifest_sha256"], reviewed)
        self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertEqual(self.stored()["epoch"], 2)
        self.assertTrue(self.stored()["dispatch_enabled"])

    def test_refresh_preserves_registered_successor_and_cannot_reenable_dispatch(self):
        token = self.register()
        revised = self.revised_manifest()
        self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(revised), note="Clarify next steps.")
        stored = self.stored()
        self.assertEqual(stored["handoff"]["successor_session_id"], SUCCESSOR)
        self.assertEqual(stored["owner_session_id"], OWNER)
        self.assertEqual(stored["epoch"], 1)
        self.assertEqual(stored["handoff"]["phase"], "registered")
        self.assertFalse(stored["dispatch_enabled"])
        self.reject("register-successor", "different successor", owner=OWNER, successor="third-session", token=token)
        reviewed = self.review_manifest(revised)
        self.reject("acknowledge-handoff", "registered successor", successor="third-session", token=token,
                    manifest_sha256=reviewed)
        self.reject("assert-owner", "frozen", owner=OWNER)
        self.reject("claim-run", "frozen", owner=OWNER, record=str(self.record_path))
        self.reject("claim-run", "not the current commander", owner=SUCCESSOR, record=str(self.record_path))

    def test_refresh_requires_outgoing_owner_and_the_existing_token(self):
        token = self.register()
        revised = self.revised_manifest()
        for caller, candidate in ((SUCCESSOR, token), ("third-session", token), (OWNER, "wrong-token"), (OWNER, None)):
            with self.subTest(caller=caller, token=candidate):
                self.reject("refresh-handoff", owner=caller, token=candidate, manifest=str(revised), note="Repair the package.")

    def test_refresh_requires_explicit_nonblank_note_and_existing_manifest(self):
        token = self.register()
        revised = self.revised_manifest()
        for note in (None, "", " \t\n", 17):
            with self.subTest(note=note):
                self.reject("refresh-handoff", "reason", owner=OWNER, token=token, manifest=str(revised), note=note)
        for manifest in (None, "relative.json", str(self.base / "missing-manifest.json")):
            with self.subTest(manifest=manifest):
                self.reject("refresh-handoff", owner=OWNER, token=token, manifest=manifest, note="Repair the package.")

    def test_refresh_requires_pending_handoff_and_cannot_run_after_activation(self):
        revised = self.revised_manifest()
        self.reject("refresh-handoff", owner=OWNER, token="unissued-token", manifest=str(revised), note="No pending handoff.")
        token = self.activate()
        for caller in (OWNER, SUCCESSOR):
            with self.subTest(caller=caller):
                self.reject("refresh-handoff", owner=caller, token=token, manifest=str(revised), note="Too late to refresh.")
        self.assertEqual(self.stored()["owner_session_id"], SUCCESSOR)
        self.assertEqual(self.stored()["epoch"], 2)
        self.assertTrue(self.stored()["dispatch_enabled"])
        self.assertEqual(self.stored()["handoff"]["phase"], "activated")

    def test_refresh_validates_exact_live_inventory_without_partial_revision(self):
        self.claim()
        self.call("update-run", owner=OWNER, run_id="unique-run", status="unknown")
        self.manifest["active_runs"] = ["unique-run"]
        token = self.register()
        for active in ([], ["unique-run", "unclaimed-job"], ["unique-run", "unique-run"]):
            with self.subTest(active=active):
                revised = self.revised_manifest(active_runs=active)
                self.reject("refresh-handoff", owner=OWNER, token=token, manifest=str(revised), note="Reconcile the live jobs.")
                self.assertNotIn("revisions", self.stored()["handoff"])
        # Completion during the freeze changes the exact live inventory. A
        # repaired manifest must now omit the terminal job but preserve its record.
        self.call("update-run", owner=OWNER, run_id="unique-run", status="completed")
        stale = self.revised_manifest("stale-inventory.json")
        self.reject("refresh-handoff", "active run registry", owner=OWNER, token=token, manifest=str(stale), note="Reconcile jobs.")
        repaired = self.revised_manifest("completed-inventory.json", active_runs=[])
        self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(repaired), note="Existing job completed during handoff.")
        self.assertEqual(self.stored()["runs"]["unique-run"]["status"], "completed")
        self.assertEqual(len(self.stored()["handoff"]["revisions"]), 1)
        self.assertFalse(self.stored()["dispatch_enabled"])

    def test_refresh_revalidates_evidence_owner_epoch_and_next_steps(self):
        token = self.acknowledge()
        revised = self.revised_manifest()
        valid = json.loads(revised.read_text(encoding="utf-8"))
        variants = []
        bad_checksum = copy.deepcopy(valid)
        bad_checksum["evidence_files"][0]["sha256"] = "0" * 64
        variants.append(bad_checksum)
        unsealed_state = copy.deepcopy(valid)
        unsealed_state["evidence_files"] = unsealed_state["evidence_files"][1:]
        variants.append(unsealed_state)
        for changes in ({"owner_epoch": 2}, {"outgoing_session_id": SUCCESSOR}, {"next_steps": []}):
            variants.append(dict(valid, **changes))
        for index, invalid in enumerate(variants):
            with self.subTest(index=index):
                self.write_json(revised, invalid)
                self.reject("refresh-handoff", owner=OWNER, token=token, manifest=str(revised), note="Repair evidence.")
                self.assertEqual(self.stored()["handoff"]["phase"], "acknowledged")
                self.assertIn("acknowledged_at", self.stored()["handoff"])
                self.assertEqual(self.stored()["handoff"]["acknowledged_manifest_sha256"], digest(self.manifest_path))
                self.assertNotIn("revisions", self.stored()["handoff"])

    def test_refresh_of_prepared_handoff_preserves_token_and_ordered_manifest_history(self):
        token = self.prepare()
        original = copy.deepcopy(self.stored()["handoff"])
        first = self.revised_manifest("revision-one.json")
        second = self.revised_manifest("revision-two.json")
        for path, note in ((first, "First correction to next steps."), (second, "Second correction after review.")):
            self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(path), note=note)
            handoff = self.stored()["handoff"]
            self.assertEqual(handoff["phase"], "prepared")
            self.assertIsNone(handoff["successor_session_id"])
            self.assertEqual(handoff["token"], token)
            self.assertEqual(handoff["outgoing_session_id"], OWNER)
            self.assertEqual(handoff["outgoing_epoch"], 1)
            self.assertNotIn("acknowledged_at", handoff)
            self.assertNotIn("acknowledged_manifest_sha256", handoff)
            self.assertFalse(self.stored()["dispatch_enabled"])
        handoff = self.stored()["handoff"]
        self.assertEqual(handoff["manifest_path"], str(second))
        self.assertEqual(handoff["manifest_sha256"], digest(second))
        self.assertEqual([(item["manifest_path"], item["manifest_sha256"], item["reason"]) for item in handoff["revisions"]],
                         [(str(self.manifest_path), original["manifest_sha256"], "First correction to next steps."),
                          (str(first), digest(first), "Second correction after review.")])
        reviewed = self.review_manifest(second)
        self.reject("acknowledge-handoff", "registered successor", successor=SUCCESSOR, token=token,
                    manifest_sha256=reviewed)
        self.call("register-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.call("acknowledge-handoff", successor=SUCCESSOR, token=token, manifest_sha256=reviewed)
        self.assertEqual(self.stored()["handoff"]["phase"], "acknowledged")

    def test_refresh_write_failure_preserves_old_manifest_ack_and_history(self):
        token = self.acknowledge()
        revised = self.revised_manifest()
        before = self.snapshot_files()
        with patch.object(control.os, "replace", side_effect=OSError("synthetic refresh replacement failure")):
            with self.assertRaisesRegex(OSError, "synthetic refresh replacement failure"):
                self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(revised), note="Revise next steps.")
        self.assertEqual(self.snapshot_files(), before)
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(list(self.directory.glob(".control-*.tmp")))
        self.assertEqual(self.stored()["handoff"]["phase"], "acknowledged")
        self.assertIn("acknowledged_at", self.stored()["handoff"])
        self.assertEqual(self.stored()["handoff"]["acknowledged_manifest_sha256"], digest(self.manifest_path))
        self.assertNotIn("revisions", self.stored()["handoff"])
        self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(revised), note="Retry the interrupted repair.")
        self.assertEqual(self.stored()["handoff"]["phase"], "registered")
        self.assertNotIn("acknowledged_at", self.stored()["handoff"])
        self.assertNotIn("acknowledged_manifest_sha256", self.stored()["handoff"])
        self.assertEqual(len(self.stored()["handoff"]["revisions"]), 1)

    def test_cli_refresh_records_reason_and_invalidates_ack_without_unfreezing(self):
        token = self.acknowledge()
        revised = self.revised_manifest()
        note = "CLI repair: clarify the next observation before takeover."
        inputs = {path: path.read_bytes() for path in (self.manifest_path, revised, self.state, self.authorization, self.evidence)}
        result = self.cli("refresh-handoff", "--owner", OWNER, "--token", token, "--manifest", str(revised), "--note", note)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["control"], self.stored())
        handoff = report["control"]["handoff"]
        self.assertEqual(handoff["token"], token)
        self.assertEqual(handoff["phase"], "registered")
        self.assertEqual(handoff["successor_session_id"], SUCCESSOR)
        self.assertNotIn("acknowledged_at", handoff)
        self.assertNotIn("acknowledged_manifest_sha256", handoff)
        self.assertEqual(handoff["revisions"][0]["reason"], note)
        self.assertEqual(handoff["manifest_sha256"], digest(revised))
        self.assertFalse(report["control"]["dispatch_enabled"])
        rejected = self.cli("activate-successor", "--owner", OWNER, "--successor", SUCCESSOR, "--token", token)
        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertFalse(json.loads(rejected.stdout)["ok"])
        self.assertIn("not verified and acknowledged", json.loads(rejected.stdout)["error"])
        self.assertEqual(self.stored(), report["control"])
        for path, original in inputs.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(list(self.base.rglob("__pycache__")))

    def test_init_is_idempotent_for_owner_and_cannot_overwrite_other_owner(self):
        before = self.control_path.read_bytes()
        result = self.call("init", owner=OWNER)
        self.assertTrue(result["unchanged"])
        self.assertEqual(self.control_path.read_bytes(), before)
        self.reject("init", "not the current commander", owner=SUCCESSOR)
        self.assertEqual(self.stored()["owner_session_id"], OWNER)
        self.assertEqual(self.stored()["epoch"], 1)

    def test_nonowner_cannot_assert_dispatch_or_claim_a_run(self):
        self.reject("assert-owner", "not the current commander", owner=SUCCESSOR)
        self.reject("claim-run", "not the current commander", owner=SUCCESSOR, record=str(self.record_path))
        self.assertEqual(self.stored()["runs"], {})

    def test_prepare_freezes_dispatch_and_retry_preserves_token(self):
        token = self.prepare()
        before = self.control_path.read_bytes()
        result = self.call("prepare-handoff", owner=OWNER, manifest=str(self.manifest_path))
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["control"]["handoff"]["token"], token)
        self.assertEqual(self.control_path.read_bytes(), before)
        self.assertFalse(self.stored()["dispatch_enabled"])
        self.reject("assert-owner", "frozen", owner=OWNER)
        self.reject("claim-run", "frozen", owner=OWNER, record=str(self.record_path))

    def test_nonowner_cannot_prepare_handoff(self):
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", "not the current commander", owner=SUCCESSOR, manifest=str(self.manifest_path))

    def test_prepare_requires_existing_canonical_state_file(self):
        self.state.unlink()
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", "does not exist", owner=OWNER, manifest=str(self.manifest_path))
        self.assertTrue(self.stored()["dispatch_enabled"])

    def test_prepare_rejects_existing_noncanonical_state_file(self):
        alternate = self.base / "state.md"
        alternate.write_bytes(self.state.read_bytes())
        self.manifest["state_file"] = str(alternate)
        self.manifest["evidence_files"].append({"path": str(alternate), "sha256": digest(alternate)})
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", "canonical state.md", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_canonical_state_in_sealed_evidence(self):
        self.manifest["evidence_files"] = self.manifest["evidence_files"][1:]
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", "sealed as evidence", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_rejects_incorrect_sha_for_state_or_authorization(self):
        for path in (self.state, self.authorization):
            with self.subTest(path=path.name):
                manifest = copy.deepcopy(self.manifest)
                next(item for item in manifest["evidence_files"] if item["path"] == str(path))["sha256"] = "0" * 64
                self.write_json(self.manifest_path, manifest)
                self.reject("prepare-handoff", "checksum mismatch", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_existing_authorization_evidence(self):
        self.authorization.unlink()
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", "does not exist", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_authorization_reference_even_if_other_evidence_is_present(self):
        self.manifest.pop("authorization_evidence_file")
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", "authorization evidence", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_authorization_evidence_to_be_sha_sealed(self):
        self.manifest["evidence_files"] = [item for item in self.manifest["evidence_files"]
                                           if item["path"] != str(self.authorization)]
        self.write_json(self.manifest_path, self.manifest)
        self.reject("prepare-handoff", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_concrete_next_steps(self):
        for steps in (None, [], [""], ["  "], [7], "inspect the job"):
            with self.subTest(steps=steps):
                manifest = copy.deepcopy(self.manifest)
                if steps is None:
                    manifest.pop("next_steps")
                else:
                    manifest["next_steps"] = steps
                self.write_json(self.manifest_path, manifest)
                self.reject("prepare-handoff", "next_steps", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_manifest_owner_epoch_and_project_match(self):
        other_project = self.base / "other-project"
        other_project.mkdir()
        for field, value in (("outgoing_session_id", SUCCESSOR), ("owner_epoch", 2),
                             ("owner_epoch", True), ("project_root", str(other_project)), ("schema_version", 99)):
            with self.subTest(field=field, value=value):
                manifest = copy.deepcopy(self.manifest)
                manifest[field] = value
                self.write_json(self.manifest_path, manifest)
                self.reject("prepare-handoff", owner=OWNER, manifest=str(self.manifest_path))

    def test_prepare_requires_exact_complete_live_run_inventory(self):
        self.claim()
        for run_id, status in (("running-job", "running"), ("unknown-job", "unknown"), ("finished-job", "completed")):
            record = dict(self.record, run_id=run_id)
            self.write_json(self.record_path, record)
            self.claim()
            self.call("update-run", owner=OWNER, run_id=run_id, status=status)
        for active in ([], ["unique-run", "running-job"], ["unique-run", "running-job", "unknown-job", "extra-job"],
                       ["unique-run", "running-job", "unknown-job", "finished-job"],
                       ["unique-run", "running-job", "unknown-job", "unique-run"]):
            with self.subTest(active=active):
                manifest = copy.deepcopy(self.manifest)
                manifest["active_runs"] = active
                self.write_json(self.manifest_path, manifest)
                self.reject("prepare-handoff", owner=OWNER, manifest=str(self.manifest_path))
        self.manifest["active_runs"] = ["unique-run", "running-job", "unknown-job"]
        token = self.prepare()
        self.assertEqual(self.stored()["handoff"]["token"], token)
        self.assertFalse(self.stored()["dispatch_enabled"])

    def test_prepare_requires_inventory_lists(self):
        for field in ("active_runs", "open_tasks", "unknowns"):
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest.pop(field)
                self.write_json(self.manifest_path, manifest)
                self.reject("prepare-handoff", owner=OWNER, manifest=str(self.manifest_path))

    def test_register_cannot_replace_a_different_successor(self):
        token = self.register()
        self.reject("register-successor", "different successor", owner=OWNER, successor="third-session", token=token)
        result = self.call("register-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertEqual(result["control"]["handoff"]["successor_session_id"], SUCCESSOR)
        self.assertEqual(result["control"]["handoff"]["phase"], "registered")

    def test_register_requires_owner_correct_token_and_fresh_successor(self):
        token = self.prepare()
        for options in ({"owner": SUCCESSOR, "successor": "third-session", "token": token},
                        {"owner": OWNER, "successor": SUCCESSOR, "token": "wrong-token"},
                        {"owner": OWNER, "successor": OWNER, "token": token}):
            with self.subTest(options=options):
                self.reject("register-successor", **options)

    def test_ack_requires_a_registered_successor(self):
        token = self.prepare()
        reviewed = self.review_manifest()
        self.reject("acknowledge-handoff", "registered successor", successor=SUCCESSOR, token=token,
                    manifest_sha256=reviewed)
        self.call("register-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.reject("acknowledge-handoff", "registered successor", successor="third-session", token=token,
                    manifest_sha256=reviewed)
        self.reject("acknowledge-handoff", "token mismatch", successor=SUCCESSOR, token="wrong-token",
                    manifest_sha256=reviewed)
        self.assertEqual(self.stored()["handoff"]["phase"], "registered")

    def test_unacknowledged_successor_cannot_activate(self):
        token = self.register()
        self.reject("activate-successor", "not verified and acknowledged", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertEqual(self.stored()["owner_session_id"], OWNER)
        self.assertFalse(self.stored()["dispatch_enabled"])

    def test_delayed_ack_of_reviewed_revision_a_cannot_confirm_refreshed_revision_b(self):
        token = self.register()
        reviewed_a = self.review_manifest()
        original = copy.deepcopy(self.stored())
        revised = self.revised_manifest("revision-b.json")
        self.call("refresh-handoff", owner=OWNER, token=token, manifest=str(revised),
                  note="Revise next steps while the successor's A acknowledgement is delayed.")
        handoff = self.stored()["handoff"]
        self.assertEqual(handoff["token"], token)
        self.assertEqual(handoff["successor_session_id"], SUCCESSOR)
        self.assertEqual(handoff["manifest_sha256"], digest(revised))
        self.assertNotEqual(handoff["manifest_sha256"], reviewed_a)
        self.reject("acknowledge-handoff", "does not match current handoff revision",
                    successor=SUCCESSOR, token=token, manifest_sha256=reviewed_a)
        frozen = self.stored()
        self.assertEqual(frozen["owner_session_id"], OWNER)
        self.assertEqual(frozen["epoch"], 1)
        self.assertEqual(frozen["runs"], original["runs"])
        self.assertFalse(frozen["dispatch_enabled"])
        self.assertEqual(frozen["handoff"]["phase"], "registered")
        self.assertNotIn("acknowledged_at", frozen["handoff"])
        self.assertNotIn("acknowledged_manifest_sha256", frozen["handoff"])
        self.reject("activate-successor", "not verified and acknowledged",
                    owner=OWNER, successor=SUCCESSOR, token=token)
        self.reject("assert-owner", "frozen", owner=OWNER)
        self.reject("claim-run", "frozen", owner=OWNER, record=str(self.record_path))
        self.reject("claim-run", "not the current commander", owner=SUCCESSOR, record=str(self.record_path))
        # Only a second semantic review of B supplies the hash that may confirm B.
        reviewed_b = self.review_manifest(revised)
        self.assertNotEqual(reviewed_b, reviewed_a)
        self.call("acknowledge-handoff", successor=SUCCESSOR, token=token, manifest_sha256=reviewed_b)
        acknowledged = self.stored()
        self.assertEqual(acknowledged["handoff"]["acknowledged_manifest_sha256"], reviewed_b)
        self.assertEqual(acknowledged["handoff"]["phase"], "acknowledged")
        self.assertFalse(acknowledged["dispatch_enabled"])
        self.assertEqual(acknowledged["owner_session_id"], OWNER)
        self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        activated = self.stored()
        self.assertEqual(activated["owner_session_id"], SUCCESSOR)
        self.assertEqual(activated["epoch"], 2)
        self.assertTrue(activated["dispatch_enabled"])
        self.assertEqual(activated["handoff"]["phase"], "activated")
        self.assertEqual(activated["handoff"]["acknowledged_manifest_sha256"], reviewed_b)
        self.assertEqual(activated["runs"], original["runs"])

    def test_ack_rejects_missing_reviewed_manifest_hash(self):
        token = self.register()
        self.review_manifest()
        self.reject("acknowledge-handoff", "requires the reviewed manifest_sha256",
                    successor=SUCCESSOR, token=token)
        self.assertEqual(self.stored()["handoff"]["phase"], "registered")
        self.assertNotIn("acknowledged_manifest_sha256", self.stored()["handoff"])
        self.assertEqual(self.stored()["owner_session_id"], OWNER)
        self.assertFalse(self.stored()["dispatch_enabled"])

    def test_ack_rejects_malformed_reviewed_manifest_hash(self):
        token = self.register()
        reviewed = self.review_manifest()
        for candidate in (None, "", " \t\n", 17, True, [], "a" * 63, "a" * 65,
                          "g" * 64, "A" * 64, reviewed + "\n", " " + reviewed):
            with self.subTest(candidate=candidate):
                self.reject("acknowledge-handoff", "requires the reviewed manifest_sha256",
                            successor=SUCCESSOR, token=token, manifest_sha256=candidate)
                self.assertEqual(self.stored()["handoff"]["phase"], "registered")
                self.assertNotIn("acknowledged_manifest_sha256", self.stored()["handoff"])
                self.assertEqual(self.stored()["owner_session_id"], OWNER)
                self.assertFalse(self.stored()["dispatch_enabled"])

    def test_ack_rejects_well_formed_but_wrong_reviewed_manifest_hash(self):
        token = self.register()
        reviewed = self.review_manifest()
        wrong = ("0" if reviewed[0] != "0" else "1") + reviewed[1:]
        self.reject("acknowledge-handoff", "does not match current handoff revision",
                    successor=SUCCESSOR, token=token, manifest_sha256=wrong)
        self.assertEqual(self.stored()["handoff"]["phase"], "registered")
        self.assertNotIn("acknowledged_manifest_sha256", self.stored()["handoff"])
        self.assertFalse(self.stored()["dispatch_enabled"])
        self.reject("activate-successor", "not verified and acknowledged",
                    owner=OWNER, successor=SUCCESSOR, token=token)

    def test_activate_rejects_missing_or_tampered_acknowledged_manifest_hash(self):
        token = self.acknowledge()
        original = self.stored()
        reviewed = original["handoff"]["acknowledged_manifest_sha256"]
        wrong = ("0" if reviewed[0] != "0" else "1") + reviewed[1:]
        # Missing confirmation is as invalid as a different or malformed digest.
        variants = [{}] + [{"acknowledged_manifest_sha256": value}
                           for value in (wrong, None, "", 17)]
        for fields in variants:
            with self.subTest(fields=fields):
                tampered = copy.deepcopy(original)
                tampered["handoff"].pop("acknowledged_manifest_sha256")
                tampered["handoff"].update(fields)
                self.write_json(self.control_path, tampered)
                self.reject("activate-successor", "acknowledged manifest revision does not match",
                            owner=OWNER, successor=SUCCESSOR, token=token)
                stored = self.stored()
                self.assertEqual(stored["owner_session_id"], OWNER)
                self.assertEqual(stored["epoch"], 1)
                self.assertFalse(stored["dispatch_enabled"])
                self.assertEqual(stored["handoff"]["phase"], "acknowledged")
                self.assertEqual(stored["handoff"]["manifest_sha256"], reviewed)
                self.assertEqual(stored["handoff"]["acknowledged_at"], original["handoff"]["acknowledged_at"])
                self.reject("claim-run", "frozen", owner=OWNER, record=str(self.record_path))
        self.write_json(self.control_path, original)
        self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertEqual(self.stored()["owner_session_id"], SUCCESSOR)
        self.assertEqual(self.stored()["epoch"], 2)

    def test_cli_ack_requires_manifest_sha256_option(self):
        token = self.register()
        self.review_manifest()
        before = self.snapshot_files()
        result = self.cli("acknowledge-handoff", "--successor", SUCCESSOR, "--token", token)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("--manifest-sha256", result.stderr)
        self.assertIn("required", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(self.snapshot_files(), before)
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(list(self.base.rglob("__pycache__")))

    def test_cli_ack_rejects_invalid_and_wrong_manifest_hash_without_committing(self):
        token = self.register()
        reviewed = self.review_manifest()
        wrong = ("0" if reviewed[0] != "0" else "1") + reviewed[1:]
        for candidate, message in (("", "requires the reviewed manifest_sha256"),
                                   (" \t", "requires the reviewed manifest_sha256"),
                                   ("not-a-hash", "requires the reviewed manifest_sha256"),
                                   (wrong, "does not match current handoff revision")):
            with self.subTest(candidate=candidate):
                before = self.snapshot_files()
                result = self.cli("acknowledge-handoff", "--successor", SUCCESSOR, "--token", token,
                                  "--manifest-sha256", candidate)
                self.assertEqual(result.returncode, 2, result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report["ok"])
                self.assertIn(message, report["error"])
                self.assertEqual(self.snapshot_files(), before)
                self.assertEqual(self.stored()["handoff"]["phase"], "registered")
                self.assertFalse(self.stored()["dispatch_enabled"])
                self.assertFalse(self.lock_path.exists())
                self.assertFalse(list(self.directory.glob(".control-*.tmp")))

    def test_cli_ack_records_reviewed_hash_and_allows_activation(self):
        token = self.register()
        reviewed = self.review_manifest()
        inputs = {path: path.read_bytes() for path in (self.manifest_path, self.state,
                                                     self.authorization, self.evidence)}
        result = self.cli("acknowledge-handoff", "--successor", SUCCESSOR, "--token", token,
                          "--manifest-sha256", reviewed)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["control"], self.stored())
        self.assertEqual(report["control"]["handoff"]["acknowledged_manifest_sha256"], reviewed)
        self.assertEqual(report["control"]["handoff"]["phase"], "acknowledged")
        self.assertFalse(report["control"]["dispatch_enabled"])
        activated = self.cli("activate-successor", "--owner", OWNER, "--successor", SUCCESSOR, "--token", token)
        self.assertEqual(activated.returncode, 0, activated.stderr)
        self.assertEqual(self.stored()["owner_session_id"], SUCCESSOR)
        self.assertEqual(self.stored()["epoch"], 2)
        self.assertTrue(self.stored()["dispatch_enabled"])
        for path, original in inputs.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(list(self.base.rglob("__pycache__")))

    def test_ack_rejects_manifest_drift(self):
        token = self.register()
        reviewed = self.review_manifest()
        self.manifest["next_steps"].append("An unreviewed extra instruction")
        self.write_json(self.manifest_path, self.manifest)
        self.reject("acknowledge-handoff", "manifest changed", successor=SUCCESSOR, token=token,
                    manifest_sha256=reviewed)
        self.assertEqual(self.stored()["handoff"]["phase"], "registered")

    def test_ack_rejects_missing_manifest(self):
        token = self.register()
        reviewed = self.review_manifest()
        self.manifest_path.unlink()
        self.reject("acknowledge-handoff", "does not exist", successor=SUCCESSOR, token=token,
                    manifest_sha256=reviewed)

    def test_ack_rejects_evidence_drift_or_disappearance(self):
        token = self.register()
        reviewed = self.review_manifest()
        for artifact in (self.state, self.authorization, self.evidence):
            original = artifact.read_bytes()
            for mutation in ("change", "remove"):
                with self.subTest(artifact=artifact.name, mutation=mutation):
                    if mutation == "change":
                        artifact.write_bytes(original + b"changed after registration\n")
                    else:
                        artifact.unlink()
                    try:
                        self.reject("acknowledge-handoff", successor=SUCCESSOR, token=token,
                                    manifest_sha256=reviewed)
                        self.assertEqual(self.stored()["owner_session_id"], OWNER)
                        self.assertEqual(self.stored()["handoff"]["phase"], "registered")
                    finally:
                        artifact.write_bytes(original)

    def test_activate_rechecks_manifest_and_evidence_after_ack(self):
        token = self.acknowledge()
        for artifact in (self.manifest_path, self.state, self.authorization):
            original = artifact.read_bytes()
            for mutation in ("change", "remove"):
                with self.subTest(artifact=artifact.name, mutation=mutation):
                    if mutation == "change":
                        artifact.write_bytes(original + b"\n")
                    else:
                        artifact.unlink()
                    try:
                        self.reject("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
                        self.assertEqual(self.stored()["epoch"], 1)
                        self.assertEqual(self.stored()["owner_session_id"], OWNER)
                        self.assertFalse(self.stored()["dispatch_enabled"])
                    finally:
                        artifact.write_bytes(original)

    def test_activation_gives_dispatch_only_to_new_owner_and_retry_keeps_epoch(self):
        token = self.activate()
        self.assertEqual(self.stored()["owner_session_id"], SUCCESSOR)
        self.assertEqual(self.stored()["epoch"], 2)
        self.assertTrue(self.stored()["dispatch_enabled"])
        self.reject("assert-owner", "not the current commander", owner=OWNER)
        self.reject("claim-run", "not the current commander", owner=OWNER, record=str(self.record_path))
        self.assertEqual(self.call("assert-owner", owner=SUCCESSOR)["epoch"], 2)
        self.claim(owner=SUCCESSOR)
        before = self.control_path.read_bytes()
        result = self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["control"]["epoch"], 2)
        self.assertEqual(self.control_path.read_bytes(), before)
        self.reject("activate-successor", owner=OWNER, successor="third-session", token=token)

    def test_handoff_preserves_active_job_and_successor_cannot_rerun_its_id(self):
        self.claim()
        checkpoint = self.project / "saved-checkpoint.bin"
        checkpoint.write_bytes(b"inert-checkpoint")
        self.call("update-run", owner=OWNER, run_id="unique-run", status="running", pid=43210,
                  checkpoint=str(checkpoint), note="existing job; observe rather than relaunch")
        previous_run = copy.deepcopy(self.stored()["runs"]["unique-run"])
        self.manifest["active_runs"] = ["unique-run"]
        self.activate()
        stored = self.stored()
        self.assertEqual(stored["runs"]["unique-run"], previous_run)
        self.assertEqual(json.loads(self.manifest_path.read_text())["active_runs"], ["unique-run"])
        self.assertEqual(previous_run["config_sha256"], digest(self.config))
        self.assertEqual(previous_run["record_sha256"], digest(self.record_path))
        self.assertEqual(previous_run["pid"], 43210)
        self.assertEqual(previous_run["checkpoint_path"], str(checkpoint))
        self.assertFalse(Path(self.record["log_path"]).exists(), "claiming a record must not execute its command")
        self.reject("claim-run", "already claimed", owner=SUCCESSOR, record=str(self.record_path))
        self.reject("update-run", "not the current commander", owner=OWNER, run_id="unique-run", status="completed")
        self.call("update-run", owner=SUCCESSOR, run_id="unique-run", status="completed")
        self.assertEqual(self.stored()["runs"]["unique-run"]["status"], "completed")

    def test_existing_job_can_be_updated_while_dispatch_is_frozen(self):
        self.claim()
        self.manifest["active_runs"] = ["unique-run"]
        token = self.register()
        self.call("update-run", owner=OWNER, run_id="unique-run", status="completed", note="finished during handoff")
        reviewed = self.review_manifest()
        self.call("acknowledge-handoff", successor=SUCCESSOR, token=token, manifest_sha256=reviewed)
        self.call("activate-successor", owner=OWNER, successor=SUCCESSOR, token=token)
        self.assertEqual(self.stored()["runs"]["unique-run"]["status"], "completed")
        self.reject("claim-run", "already claimed", owner=SUCCESSOR, record=str(self.record_path))

    def test_unique_run_id_claim_rejects_duplicates_even_if_record_changes(self):
        self.claim()
        self.record["command"] = ["different-inert-command"]
        self.write_json(self.record_path, self.record)
        self.reject("claim-run", "already claimed", owner=OWNER, record=str(self.record_path))
        self.assertEqual(len(self.stored()["runs"]), 1)
        self.assertEqual(self.stored()["runs"]["unique-run"]["command"], ["synthetic-trainer-must-never-execute", "--fixture-only"])

    def test_terminal_completed_and_failed_run_ids_cannot_be_revived(self):
        for terminal in ("completed", "failed"):
            with self.subTest(terminal=terminal):
                run_id = "terminal-" + terminal
                self.write_json(self.record_path, dict(self.record, run_id=run_id))
                self.claim()
                self.call("update-run", owner=OWNER, run_id=run_id, status=terminal)
                for live in ("claimed", "running", "unknown", "failed" if terminal == "completed" else "completed"):
                    with self.subTest(target=live):
                        self.reject("update-run", "terminal run", owner=OWNER, run_id=run_id, status=live)
                self.reject("claim-run", "already claimed", owner=OWNER, record=str(self.record_path))
                self.assertEqual(self.stored()["runs"][run_id]["status"], terminal)

    def test_invalid_claim_does_not_partially_commit_registry(self):
        for field, value in (("config_path", str(self.base / "missing-config.json")),
                             ("working_directory", str(self.base / "missing-directory")),
                             ("log_path", "relative.log"), ("run_id", "../unsafe"), ("command", []), ("command", [7])):
            with self.subTest(field=field, value=value):
                self.write_json(self.record_path, dict(self.record, **{field: value}))
                self.reject("claim-run", owner=OWNER, record=str(self.record_path))
                self.assertEqual(self.stored()["runs"], {})

    def test_failed_update_does_not_commit_pid_before_checkpoint_validation(self):
        self.claim()
        self.reject("update-run", "does not exist", owner=OWNER, run_id="unique-run", status="running", pid=999,
                    checkpoint=str(self.project / "missing-checkpoint.bin"))
        self.assertIsNone(self.stored()["runs"]["unique-run"]["pid"])
        self.assertEqual(self.stored()["runs"]["unique-run"]["status"], "claimed")

    def test_atomic_replace_failure_preserves_previous_control_and_removes_own_temporaries(self):
        before = self.snapshot_files()
        with patch.object(control.os, "replace", side_effect=OSError("synthetic replacement failure")):
            with self.assertRaisesRegex(OSError, "synthetic replacement failure"):
                self.claim()
        self.assertEqual(self.snapshot_files(), before)
        self.assertFalse(self.lock_path.exists())
        self.assertFalse(list(self.directory.glob(".control-*.tmp")))
        self.claim()
        self.assertIn("unique-run", self.stored()["runs"])

    def test_preexisting_lock_is_not_blindly_deleted_or_overwritten(self):
        original = b'{"pid": 99999999, "created_at": "1900-01-01T00:00:00Z", "owner": "other transaction"}\n'
        self.lock_path.write_bytes(original)
        before = self.snapshot_files()
        with self.assertRaisesRegex(control.ControlError, "locked"):
            self.claim()
        self.assertEqual(self.lock_path.read_bytes(), original)
        self.assertEqual(self.snapshot_files(), before)
        result = self.cli("claim-run", "--owner", OWNER, "--record", str(self.record_path))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertEqual(self.snapshot_files(), before)

    def test_transaction_cleanup_does_not_delete_a_replacement_lock(self):
        # Simulate another process replacing the pathname after this transaction
        # acquired its own lock. Cleanup must verify ownership before unlinking.
        replacement = b'{"pid": 99999999, "owner": "replacement transaction"}\n'
        before_control = self.control_path.read_bytes()
        with control.transaction(str(self.project)):
            self.assertTrue(self.lock_path.exists())
            self.lock_path.unlink()
            self.lock_path.write_bytes(replacement)
        self.assertTrue(self.lock_path.exists(), "cleanup blindly deleted a lock now owned by another transaction")
        self.assertEqual(self.lock_path.read_bytes(), replacement)
        self.assertEqual(self.control_path.read_bytes(), before_control)

    def test_cli_status_and_rejected_claim_are_structured_and_read_only(self):
        before = self.snapshot_files()
        result = self.cli("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["control"]["owner_session_id"], OWNER)
        rejected = self.cli("claim-run", "--owner", SUCCESSOR, "--record", str(self.record_path))
        self.assertEqual(rejected.returncode, 2, rejected.stderr)
        self.assertFalse(json.loads(rejected.stdout)["ok"])
        self.assertIn("not the current commander", json.loads(rejected.stdout)["error"])
        self.assertEqual(self.snapshot_files(), before)
        self.assertFalse(list(self.base.rglob("__pycache__")))

    def test_corrupt_control_cannot_be_reinitialized_or_partially_rewritten(self):
        for raw in (b"{broken-json", b"[]", b'{"schema_version": 999}'):
            with self.subTest(raw=raw):
                self.control_path.write_bytes(raw)
                self.reject("init", owner=OWNER)
                self.reject("claim-run", owner=OWNER, record=str(self.record_path))

    def test_concurrent_subprocess_claims_same_run_succeed_exactly_once(self):
        # A filesystem start gate makes all independently imported callers ready
        # before contention. They only claim records; no training is launched.
        worker = """
import importlib.util, json, pathlib, sys, time
source, project, record, owner, ready_name, gate_name = sys.argv[1:]
spec = importlib.util.spec_from_file_location('isolated_control_claim', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
pathlib.Path(ready_name).write_text('ready', encoding='utf-8')
deadline = time.monotonic() + 15
while not pathlib.Path(gate_name).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('start gate timed out')
    time.sleep(0.01)
try:
    result = module.perform(project, 'claim-run', owner=owner, record=record)
except (module.ControlError, OSError) as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}))
    raise SystemExit(2)
print(json.dumps(result))
"""
        gate = self.base / "start-gate"
        ready = [self.base / ("ready-" + str(i)) for i in range(4)]
        inputs = {path: path.read_bytes() for path in (SOURCE, self.record_path, self.config, self.state, self.authorization)}
        children = []
        try:
            for signal in ready:
                children.append(subprocess.Popen([str(PYTHON), "-I", "-B", "-c", worker, str(SOURCE),
                                                  str(self.project), str(self.record_path), OWNER, str(signal), str(gate)],
                                                 cwd=self.base, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                 text=True, encoding="utf-8"))
            deadline = time.monotonic() + 15
            while not all(path.exists() for path in ready):
                if any(child.poll() is not None for child in children):
                    self.fail("a claim worker exited before the shared start gate")
                if time.monotonic() >= deadline:
                    self.fail("claim workers did not become ready")
                time.sleep(0.01)
            gate.write_text("go", encoding="utf-8")
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=20)
                self.assertEqual(stderr, "", stderr)
                results.append((child.returncode, json.loads(stdout)))
            self.assertEqual(sum(code == 0 and result["ok"] for code, result in results), 1, results)
            self.assertEqual(sum(code == 2 and not result["ok"] for code, result in results), 3, results)
            for code, result in results:
                if code != 0:
                    self.assertTrue("locked" in result["error"] or "already claimed" in result["error"], result)
            stored = self.stored()
            self.assertEqual(set(stored["runs"]), {"unique-run"})
            self.assertEqual(stored["runs"]["unique-run"]["status"], "claimed")
            self.assertEqual(stored["runs"]["unique-run"]["claimed_by"], OWNER)
            self.assertEqual(stored["epoch"], 1)
            self.assertFalse(self.lock_path.exists())
            self.assertFalse(list(self.directory.glob(".control-*.tmp")))
            self.assertFalse(Path(self.record["log_path"]).exists())
            for path, original in inputs.items():
                self.assertEqual(path.read_bytes(), original, str(path))
            self.assertFalse(list(self.base.rglob("__pycache__")))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
