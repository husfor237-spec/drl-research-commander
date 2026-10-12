#!/usr/bin/env python3
"""Atomic scheduler ownership, verified handoffs, and nonduplicating run claims.

Only writes <project>/.codex-run/drl-research/control.json and its short-lived
transaction lock. Does not create chats, launch/kill training, or change code.
Identity arguments must come from the actual calling session, not a guessed ID.
"""

import argparse
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


ROOT_PARTS = (".codex-run", "drl-research")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
LIVE_RUN_STATES = {"claimed", "running", "unknown"}
RUN_STATES = LIVE_RUN_STATES | {"completed", "failed"}


class ControlError(ValueError):
    pass


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ControlError("invalid " + label)
    return value


def absolute_path(value, label, must_exist=False):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ControlError(label + " must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise ControlError(label + " must be an absolute path")
    path = path.resolve()
    if must_exist and not path.is_file():
        raise ControlError(label + " does not exist: " + str(path))
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ControlError("cannot read JSON: " + str(path) + ": " + str(exc)) from exc
    if not isinstance(value, dict):
        raise ControlError("JSON must be an object: " + str(path))
    return value


def atomic_write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".control-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def project_path(project):
    root = absolute_path(project, "project")
    if not root.is_dir():
        raise ControlError("project directory does not exist")
    return root


@contextmanager
def transaction(project):
    root = project_path(project)
    directory = root.joinpath(*ROOT_PARTS)
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ".control.lock"
    lock_token = uuid.uuid4().hex
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ControlError("control is locked; inspect the lock and retry, never delete it blindly") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "created_at": timestamp(), "token": lock_token}, stream)
        yield root, directory / "control.json"
    finally:
        # Preserve a replacement lock belonging to another transaction. Normal
        # callers never delete somebody else's lock, even during error cleanup.
        try:
            if read_json(lock).get("token") == lock_token:
                lock.unlink()
        except (ControlError, OSError):
            pass


def validate_control(value, root):
    if value.get("schema_version") != 1:
        raise ControlError("unsupported control schema")
    if project_path(value.get("project_root")) != root:
        raise ControlError("control belongs to a different project")
    identifier(value.get("owner_session_id"), "owner session ID")
    if type(value.get("epoch")) is not int or value["epoch"] < 1:
        raise ControlError("invalid ownership epoch")
    if type(value.get("dispatch_enabled")) is not bool or not isinstance(value.get("runs"), dict):
        raise ControlError("invalid dispatch state or run registry")
    return value


def require_owner(control, owner, dispatch=False):
    identifier(owner, "owner session ID")
    if owner != control["owner_session_id"]:
        raise ControlError("caller is not the current commander")
    if dispatch and not control["dispatch_enabled"]:
        raise ControlError("dispatch is frozen for handoff")


def verify_manifest(path, root, outgoing, epoch, expected_hash=None):
    path = absolute_path(path, "manifest", must_exist=True)
    actual_hash = sha256(path)
    if expected_hash is not None and actual_hash != expected_hash:
        raise ControlError("handoff manifest changed after preparation")
    manifest = read_json(path)
    if manifest.get("schema_version") != 1 or project_path(manifest.get("project_root")) != root:
        raise ControlError("manifest schema/project mismatch")
    if (manifest.get("outgoing_session_id") != outgoing or
            type(manifest.get("owner_epoch")) is not int or manifest["owner_epoch"] != epoch):
        raise ControlError("manifest owner/epoch mismatch")
    steps = manifest.get("next_steps")
    if not isinstance(steps, list) or not steps or any(not isinstance(s, str) or not s.strip() for s in steps):
        raise ControlError("handoff needs concrete next_steps")
    for field in ("open_tasks", "active_runs", "unknowns"):
        if not isinstance(manifest.get(field), list):
            raise ControlError("manifest missing list: " + field)
    if any(not isinstance(run, str) for run in manifest["active_runs"]):
        raise ControlError("active_runs must contain run IDs")
    if len(set(manifest["active_runs"])) != len(manifest["active_runs"]):
        raise ControlError("duplicate active run IDs in manifest")
    state_file = absolute_path(manifest.get("state_file"), "state_file", must_exist=True)
    if state_file != root.joinpath(*ROOT_PARTS, "state.md").resolve():
        raise ControlError("manifest must reference this project's canonical state.md")
    evidence = manifest.get("evidence_files")
    if not isinstance(evidence, list) or not evidence:
        raise ControlError("handoff must contain evidence_files")
    seen = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise ControlError("invalid evidence entry")
        artifact = absolute_path(item.get("path"), "evidence file", must_exist=True)
        if artifact in seen:
            raise ControlError("duplicate evidence file")
        seen.add(artifact)
        recorded = item.get("sha256")
        if (not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded)
                or sha256(artifact) != recorded):
            raise ControlError("evidence checksum mismatch: " + str(artifact))
    if state_file not in seen:
        raise ControlError("canonical state.md must be sealed as evidence")
    # Trust comes from checking the referenced human request, never from this file
    # or its checksum alone. The helper only proves that the file still exists.
    authorization = absolute_path(manifest.get("authorization_evidence_file"),
                                  "authorization evidence", must_exist=True)
    if authorization not in seen:
        raise ControlError("authorization evidence must be sealed in evidence_files")
    return manifest, actual_hash


def check_handoff(control, token):
    handoff = control.get("handoff")
    if not isinstance(handoff, dict) or handoff.get("token") != token:
        raise ControlError("handoff token mismatch")
    return handoff


def validate_live_runs(control, manifest, exact=False):
    current = {key for key, run in control["runs"].items() if run.get("status") in LIVE_RUN_STATES}
    recorded = set(manifest["active_runs"])
    if (exact and current != recorded) or (not exact and not current.issubset(recorded)):
        raise ControlError("manifest does not account for active run registry")


def perform(project, action, **options):
    with transaction(project) as (root, path):
        if action == "init":
            owner = identifier(options.get("owner"), "owner session ID")
            if path.exists():
                control = validate_control(read_json(path), root)
                require_owner(control, owner)
                return {"ok": True, "unchanged": True, "control": control}
            control = {"schema_version": 1, "project_root": str(root),
                       "owner_session_id": owner, "epoch": 1, "dispatch_enabled": True,
                       "handoff": None, "runs": {}, "created_at": timestamp(),
                       "updated_at": timestamp()}
            atomic_write(path, control)
            return {"ok": True, "control": control}

        control = validate_control(read_json(path), root)
        owner = options.get("owner")
        if action == "status":
            return {"ok": True, "control": control}
        if action == "assert-owner":
            require_owner(control, owner, dispatch=True)
            return {"ok": True, "owner_session_id": owner, "epoch": control["epoch"]}

        if action == "prepare-handoff":
            require_owner(control, owner)
            manifest, digest = verify_manifest(options.get("manifest"), root, owner, control["epoch"])
            validate_live_runs(control, manifest, exact=True)
            existing = control.get("handoff")
            if existing and existing.get("phase") != "activated":
                if existing.get("manifest_sha256") == digest and existing.get("outgoing_session_id") == owner:
                    return {"ok": True, "unchanged": True, "control": control}
                raise ControlError("another handoff is already pending")
            control["dispatch_enabled"] = False
            control["handoff"] = {"token": uuid.uuid4().hex, "phase": "prepared",
                                  "outgoing_session_id": owner, "outgoing_epoch": control["epoch"],
                                  "manifest_path": str(absolute_path(options["manifest"], "manifest")),
                                  "manifest_sha256": digest, "successor_session_id": None,
                                  "prepared_at": timestamp()}
        elif action in ("register-successor", "acknowledge-handoff", "activate-successor"):
            handoff = check_handoff(control, options.get("token"))
            successor = identifier(options.get("successor"), "successor session ID")
            if successor == handoff["outgoing_session_id"]:
                raise ControlError("successor must be a different, fresh session")
            # Activation can be retried after a lost tool response without creating
            # another epoch or admitting the old commander to new dispatch.
            if action == "activate-successor" and handoff["phase"] == "activated":
                if (owner == handoff["outgoing_session_id"] and
                        successor == control["owner_session_id"] == handoff["successor_session_id"]):
                    return {"ok": True, "unchanged": True, "control": control}
                raise ControlError("handoff already activated")
            if action != "acknowledge-handoff":
                require_owner(control, owner)
            if control["dispatch_enabled"]:
                raise ControlError("handoff requires frozen dispatch")
            if action == "register-successor":
                previous = handoff.get("successor_session_id")
                if previous is not None and previous != successor:
                    raise ControlError("a different successor is already registered")
                if handoff["phase"] not in ("prepared", "registered", "acknowledged"):
                    raise ControlError("unexpected handoff phase")
                handoff["successor_session_id"] = successor
                if handoff["phase"] != "acknowledged":
                    handoff["phase"] = "registered"
            else:
                if handoff.get("successor_session_id") != successor:
                    raise ControlError("caller is not the registered successor")
                manifest, _ = verify_manifest(handoff["manifest_path"], root,
                                              handoff["outgoing_session_id"], handoff["outgoing_epoch"],
                                              handoff["manifest_sha256"])
                validate_live_runs(control, manifest)
                if action == "acknowledge-handoff":
                    if handoff["phase"] not in ("registered", "acknowledged"):
                        raise ControlError("successor cannot acknowledge this phase")
                    reviewed = options.get("manifest_sha256")
                    if not isinstance(reviewed, str) or not re.fullmatch(r"[0-9a-f]{64}", reviewed):
                        raise ControlError("ack requires the reviewed manifest_sha256")
                    if reviewed != handoff["manifest_sha256"]:
                        raise ControlError("reviewed manifest_sha256 does not match current handoff revision")
                    handoff["phase"] = "acknowledged"
                    handoff["acknowledged_manifest_sha256"] = reviewed
                    handoff["acknowledged_at"] = timestamp()
                else:
                    if handoff["phase"] != "acknowledged":
                        raise ControlError("successor has not verified and acknowledged the package")
                    if handoff.get("acknowledged_manifest_sha256") != handoff["manifest_sha256"]:
                        raise ControlError("acknowledged manifest revision does not match current handoff")
                    control["owner_session_id"] = successor
                    control["epoch"] += 1
                    control["dispatch_enabled"] = True
                    handoff["phase"] = "activated"
                    handoff["activated_at"] = timestamp()
        elif action == "refresh-handoff":
            require_owner(control, owner)
            handoff = check_handoff(control, options.get("token"))
            if control["dispatch_enabled"] or handoff["phase"] == "activated":
                raise ControlError("only a pending frozen handoff can be refreshed")
            note = options.get("note")
            if not isinstance(note, str) or not note.strip():
                raise ControlError("refresh requires a reason")
            manifest, digest = verify_manifest(options.get("manifest"), root, owner, control["epoch"])
            validate_live_runs(control, manifest, exact=True)
            handoff.setdefault("revisions", []).append({
                "manifest_path": handoff["manifest_path"],
                "manifest_sha256": handoff["manifest_sha256"],
                "replaced_at": timestamp(), "reason": note})
            handoff.update(manifest_path=str(absolute_path(options["manifest"], "manifest")),
                           manifest_sha256=digest,
                           phase="registered" if handoff.get("successor_session_id") else "prepared")
            handoff.pop("acknowledged_at", None)
            handoff.pop("acknowledged_manifest_sha256", None)
        elif action == "claim-run":
            require_owner(control, owner, dispatch=True)
            record_path = absolute_path(options.get("record"), "run record", must_exist=True)
            record = read_json(record_path)
            run_id = identifier(record.get("run_id"), "run ID")
            if run_id in control["runs"]:
                raise ControlError("run ID already claimed; inspect the existing run, never launch it again")
            config = absolute_path(record.get("config_path"), "run config", must_exist=True)
            working = absolute_path(record.get("working_directory"), "working directory")
            if not working.is_dir():
                raise ControlError("run working directory does not exist")
            command = record.get("command")
            if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s for s in command):
                raise ControlError("run command must be a nonempty argument list")
            log_path = absolute_path(record.get("log_path"), "run log")
            control["runs"][run_id] = {"run_id": run_id, "status": "claimed",
                                       "config_path": str(config), "config_sha256": sha256(config),
                                       "record_path": str(record_path), "record_sha256": sha256(record_path),
                                       "command": command, "working_directory": str(working),
                                       "log_path": str(log_path), "claimed_by": owner,
                                       "claimed_epoch": control["epoch"], "claimed_at": timestamp(),
                                       "pid": None, "checkpoint_path": None}
        elif action == "update-run":
            # Status/checkpoint preservation is allowed while new dispatch is
            # frozen, and after takeover only the new commander may change it.
            require_owner(control, owner)
            run_id = identifier(options.get("run_id"), "run ID")
            if run_id not in control["runs"]:
                raise ControlError("run was not claimed")
            status = options.get("status")
            if status not in RUN_STATES:
                raise ControlError("invalid run status")
            run = control["runs"][run_id]
            if run["status"] in ("completed", "failed") and status != run["status"]:
                raise ControlError("terminal run IDs cannot be relaunched; use a new run ID")
            pid = options.get("pid")
            if pid is not None:
                if type(pid) is not int or pid <= 0:
                    raise ControlError("invalid process ID")
                run["pid"] = pid
            checkpoint = options.get("checkpoint")
            if checkpoint is not None:
                run["checkpoint_path"] = str(absolute_path(checkpoint, "checkpoint", must_exist=True))
            run.update(status=status, updated_at=timestamp(), note=options.get("note") or "")
        else:
            raise ControlError("unknown action")
        control["updated_at"] = timestamp()
        atomic_write(path, control)
        return {"ok": True, "control": control}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Absolute existing paper project directory")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "assert-owner"):
        child = sub.add_parser(name)
        child.add_argument("--owner", required=True)
    sub.add_parser("status")
    child = sub.add_parser("prepare-handoff")
    child.add_argument("--owner", required=True)
    child.add_argument("--manifest", required=True)
    child = sub.add_parser("refresh-handoff")
    child.add_argument("--owner", required=True)
    child.add_argument("--token", required=True)
    child.add_argument("--manifest", required=True)
    child.add_argument("--note", required=True)
    for name in ("register-successor", "activate-successor"):
        child = sub.add_parser(name)
        child.add_argument("--owner", required=True)
        child.add_argument("--successor", required=True)
        child.add_argument("--token", required=True)
    child = sub.add_parser("acknowledge-handoff")
    child.add_argument("--successor", required=True)
    child.add_argument("--token", required=True)
    child.add_argument("--manifest-sha256", required=True,
                       help="SHA-256 of the exact manifest the successor semantically reviewed")
    child = sub.add_parser("claim-run")
    child.add_argument("--owner", required=True)
    child.add_argument("--record", required=True)
    child = sub.add_parser("update-run")
    child.add_argument("--owner", required=True)
    child.add_argument("--run-id", required=True)
    child.add_argument("--status", required=True, choices=sorted(RUN_STATES))
    child.add_argument("--pid", type=int)
    child.add_argument("--checkpoint")
    child.add_argument("--note")
    args = vars(parser.parse_args(argv))
    project, action = args.pop("project"), args.pop("action")
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass
    try:
        result = perform(project, action, **args)
    except (ControlError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
