#!/usr/bin/env python3
"""Append one validated decision entry to a project's decisions.jsonl ledger.

The ledger shape is defined in references/templates.md. This tool only appends
to <project>/.codex-run/drl-research/decisions.jsonl; it never rewrites state.md,
control.json or existing lines, and it never launches training. `--check-only`
validates the whole existing ledger and writes nothing.
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
import uuid


ROOT_PARTS = (".codex-run", "drl-research")
LEDGER_NAME = "decisions.jsonl"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
STAGE = re.compile(r"G[0-9]\Z")
FIELDS = ("id", "timestamp", "stage", "spec_version", "decision", "reason",
          "evidence", "affected_runs", "revalidation")


class DecisionError(ValueError):
    pass


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DecisionError(label + " must be a nonempty string")
    return value.strip()


def string_list(value, label, allow_empty=True):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise DecisionError(label + " must be a list of nonempty strings")
    if not value and not allow_empty:
        raise DecisionError(label + " must contain at least one item")
    return [item.strip() for item in value]


def validate_entry(entry, where="entry"):
    if not isinstance(entry, dict):
        raise DecisionError(where + " must be a JSON object")
    missing = [key for key in FIELDS if key not in entry]
    if missing:
        raise DecisionError(where + " is missing: " + ", ".join(missing))
    extra = [key for key in entry if key not in FIELDS]
    if extra:
        raise DecisionError(where + " has unknown fields: " + ", ".join(sorted(extra)))
    identifier = text(entry["id"], where + " id")
    if not TOKEN.fullmatch(identifier):
        raise DecisionError(where + " id must match " + TOKEN.pattern)
    stamp = text(entry["timestamp"], where + " timestamp")
    stage = text(entry["stage"], where + " stage")
    warnings = []
    if not STAGE.fullmatch(stage):
        warnings.append(where + ": stage is not one of G0-G9: " + stage)
    return {"id": identifier, "timestamp": stamp, "stage": stage,
            "spec_version": text(entry["spec_version"], where + " spec_version"),
            "decision": text(entry["decision"], where + " decision"),
            "reason": text(entry["reason"], where + " reason"),
            "evidence": string_list(entry["evidence"], where + " evidence", allow_empty=False),
            "affected_runs": string_list(entry["affected_runs"], where + " affected_runs"),
            "revalidation": text(entry["revalidation"], where + " revalidation")}, warnings


def read_ledger(path):
    if not path.exists():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            raise DecisionError("line " + str(number) + " is blank; one JSON object per line")
        try:
            entries.append((number, json.loads(line)))
        except ValueError as exc:
            raise DecisionError("line " + str(number) + " is not valid JSON: " + str(exc)) from exc
    return entries


def ledger_path(project):
    root = Path(project)
    if not root.is_absolute():
        raise DecisionError("project must be an absolute path")
    if not root.is_dir():
        raise DecisionError("project directory does not exist: " + str(root))
    return root.resolve().joinpath(*ROOT_PARTS, LEDGER_NAME)


def check_ledger(path):
    entries = read_ledger(path)
    seen = set()
    warnings = []
    for number, entry in entries:
        clean, notes = validate_entry(entry, "line " + str(number))
        warnings.extend(notes)
        if clean["id"] in seen:
            raise DecisionError("duplicate decision id on line " + str(number) + ": " + clean["id"])
        seen.add(clean["id"])
    return len(entries), warnings


def append_entry(path, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(entry, ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="Absolute existing paper project directory")
    parser.add_argument("--check-only", action="store_true",
                        help="Validate the existing ledger and write nothing")
    parser.add_argument("--id", help="Decision ID; defaults to a generated unique ID")
    parser.add_argument("--timestamp", help="ISO timestamp; defaults to now (UTC)")
    parser.add_argument("--stage", help="Current stage, normally G0-G9")
    parser.add_argument("--spec-version", help="Specification/frozen version this decision belongs to")
    parser.add_argument("--decision", help="What was decided")
    parser.add_argument("--reason", help="Why, including rejected options")
    parser.add_argument("--evidence", action="append", default=[],
                        help="Absolute artifact path or citation; repeatable, at least one required")
    parser.add_argument("--affected-run", action="append", default=[],
                        help="Affected run ID; repeatable, may be empty")
    parser.add_argument("--revalidation", help="What must be re-run or re-checked because of this")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass

    try:
        path = ledger_path(args.project)
        count, warnings = check_ledger(path)
        if args.check_only:
            print(json.dumps({"ok": True, "ledger": str(path), "entries": count,
                              "warnings": warnings}, ensure_ascii=False, indent=2))
            return 0
        stamp = args.timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        stage = args.stage
        if not stage:
            raise DecisionError("--stage is required when appending")
        entry = {"id": args.id or ("d-" + uuid.uuid4().hex[:12]),
                 "timestamp": stamp, "stage": stage,
                 "spec_version": args.spec_version, "decision": args.decision,
                 "reason": args.reason, "evidence": args.evidence,
                 "affected_runs": args.affected_run, "revalidation": args.revalidation}
        clean, warnings = validate_entry(entry)
        existing = {item.get("id") for _, item in read_ledger(path) if isinstance(item, dict)}
        if clean["id"] in existing:
            raise DecisionError("decision id already present: " + clean["id"])
        append_entry(path, clean)
        print(json.dumps({"ok": True, "ledger": str(path), "entries": count + 1,
                          "appended": clean, "warnings": warnings},
                         ensure_ascii=False, indent=2))
        return 0
    except (DecisionError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
