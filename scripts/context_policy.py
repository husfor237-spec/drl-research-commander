#!/usr/bin/env python3
"""Read explicitly bound Codex telemetry; apply the DRL project's 80/90/2 policy.

Uses the installed context-gauge reader, not its thresholds, model tables or
capability estimates. Never writes session files, configuration, hooks or panels.
"""

import argparse
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys


POLICY = {"prepare_fill_percent": 80, "required_fill_percent": 90,
          "compaction_limit": 2, "policy_is_heuristic": True}
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def default_gauge_path():
    sibling = Path(__file__).resolve().parents[2] / "context-gauge/scripts/context_gauge.py"
    if sibling.is_file():
        return sibling
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return root / "skills/context-gauge/scripts/context_gauge.py"


def load_gauge(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("context-gauge reader not found: " + str(path))
    spec = importlib.util.spec_from_file_location("drl_context_gauge_reader", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load context-gauge reader")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    for name in ("find_session", "scan_session", "snapshot_status"):
        if not callable(getattr(module, name, None)):
            raise ValueError("incompatible context-gauge reader: missing " + name)
    return module


def audit_boundaries(path):
    """Check structural events and provenance, including post-compaction gaps.

    This small audit supplements the shared reader: strings inside a message are
    not session metadata or compacted events; a new window or model needs its own
    usage report. A partial final JSONL line is ignored and can be retried later.
    """
    result = {"session_ids": set(), "metadata_valid": True,
              "reader_events": [], "model": None, "compactions": 0,
              "last_compaction_line": -1, "last_compaction_timestamp": None,
              "last_model_line": -1, "last_turn_line": -1,
              "latest_turn_timestamp": None, "usage_line": -1,
              "usage_timestamp": None, "used": None, "window": None}
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream):
            if not any(tag in line for tag in
                       ('"session_meta"', '"turn_context"', '"compacted"', '"token_count"')):
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            payload = event.get("payload")
            if not isinstance(payload, dict):
                if kind == "session_meta":
                    result["metadata_valid"] = False
                continue
            clean = None
            if kind == "session_meta":
                ids = [payload[field] for field in ("session_id", "id") if field in payload]
                if (not ids or any(not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) for sid in ids)
                        or len(set(ids)) != 1):
                    result["metadata_valid"] = False
                else:
                    result["session_ids"].add(ids[0])
                    clean = {"id": ids[0]}
            elif kind == "turn_context":
                model = payload.get("model")
                # The latest turn owns model identity. Missing/invalid identity
                # cannot inherit a previous model, even with new usage telemetry.
                model = model if isinstance(model, str) and model.strip() else None
                if model is None or model != result["model"]:
                    result["last_model_line"] = number
                    result.update(usage_line=-1, usage_timestamp=None,
                                  used=None, window=None)
                result["model"] = model
                result["last_turn_line"] = number
                result["latest_turn_timestamp"] = event.get("timestamp")
                clean = {"model": model} if model is not None else {}
            elif kind == "compacted":
                result["compactions"] += 1
                result["last_compaction_line"] = number
                result["last_compaction_timestamp"] = event.get("timestamp")
                clean = {}
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                if not isinstance(usage, dict):
                    continue
                used = usage.get("input_tokens")
                if type(used) is not int or used < 0:
                    continue
                window = info.get("model_context_window")
                result.update(usage_line=number, usage_timestamp=event.get("timestamp"),
                              used=used, window=window if type(window) is int and window > 0 else None)
                clean = {"type": "token_count", "info": {"last_token_usage": {"input_tokens": used}}}
                if result["window"] is not None:
                    clean["info"]["model_context_window"] = result["window"]
            if clean is not None:
                canonical = {"type": kind, "payload": clean, "timestamp": event.get("timestamp")}
                result["reader_events"].append(json.dumps(canonical, ensure_ascii=False) + "\n")
    return result


def empty_report(session_id, reason):
    return {"schema_version": "1.0", "ok": False, "binding_verified": False,
            "bound_by": "explicit", "requested_session_id": session_id,
            "session_id": None, "model": None, "used_tokens": None,
            "context_window": None, "window_source": "unknown", "fill_percent": None,
            "compactions": None, "snapshot_status": "unavailable",
            "revalidate_after_compaction": False, "policy": dict(POLICY),
            "rotation": {"action": "unknown", "reasons": [reason]},
            "usage_timestamp": None, "latest_turn_timestamp": None,
            "last_compaction_timestamp": None,
            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def read_policy(session_id, session=None, gauge_path=None, now=None):
    report = empty_report(session_id, "telemetry_unavailable")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        report["rotation"]["reasons"] = ["invalid_session_id"]
        return report
    try:
        gauge = load_gauge(gauge_path or default_gauge_path())
        path = gauge.find_session(session_id=session_id, path=session, prefer_recent=False)
        if not path:
            report["rotation"]["reasons"] = ["explicit_session_not_found"]
            return report
        audit = audit_boundaries(path)
    except (OSError, ValueError, ImportError, AttributeError, TypeError, KeyError) as exc:
        report["rotation"]["reasons"] = ["reader_unavailable"]
        report["error"] = str(exc)
        return report

    if not audit["metadata_valid"] or audit["session_ids"] != {session_id}:
        report["snapshot_status"] = "binding_mismatch"
        report["rotation"]["reasons"] = ["session_metadata_does_not_match_requested_id"]
        return report

    # The installed reader prefilters by strings, so feed it only canonical
    # structural telemetry from this single read. This is an isolated imported
    # module, not a change to its file or a global builtins patch. No temporary
    # transcript is written, and user-message text cannot masquerade as events.
    events = "".join(audit["reader_events"])
    if audit["model"] is None:
        # The shared reader retains historical identity for a model-less turn.
        # A fresh in-memory segment drops that history without changing the reader.
        events = "".join(line for line in audit["reader_events"]
                         if json.loads(line)["type"] in ("session_meta", "compacted"))
        if audit["usage_line"] >= 0:
            events += json.dumps({"type": "event_msg", "timestamp": audit["usage_timestamp"],
                                  "payload": {"type": "token_count", "info": {
                                      "last_token_usage": {"input_tokens": audit["used"]},
                                      "model_context_window": audit["window"]}}}) + "\n"
    gauge.open = lambda *args, **kwargs: io.StringIO(events)
    try:
        state = gauge.scan_session(path)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        report["rotation"]["reasons"] = ["reader_unavailable"]
        report["error"] = str(exc)
        return report

    report.update(ok=True, binding_verified=True, session_id=session_id,
                  model=audit["model"], compactions=audit["compactions"],
                  revalidate_after_compaction=audit["compactions"] >= 1,
                  usage_timestamp=audit["usage_timestamp"],
                  latest_turn_timestamp=audit["latest_turn_timestamp"],
                  last_compaction_timestamp=audit["last_compaction_timestamp"])
    reasons = []
    has_usage = audit["usage_line"] >= 0
    consistent = (state.get("session_id") == session_id and
                  state.get("model") == audit["model"] and
                  state.get("compactions") == audit["compactions"] and
                  state.get("used") == audit["used"] and
                  state.get("usage_ts") == audit["usage_timestamp"])
    if not has_usage:
        status = "unavailable"
        reasons.append("no_usage_for_current_model")
    elif not consistent:
        status = "reader_mismatch"
        reasons.append("transcript_changed_or_reader_provenance_mismatch")
    else:
        status = gauge.snapshot_status(state, now=now)["snapshot_status"]
        if audit["usage_line"] < audit["last_compaction_line"]:
            status = "awaiting_post_compaction_usage"
        elif audit["usage_line"] < audit["last_turn_line"]:
            status = "awaiting_usage"
        report["used_tokens"] = audit["used"]
        # A specification fallback, or a window from a different event, is never
        # promoted to measured telemetry. Keep this adapter free of model tables.
        if (state.get("window_source") == "codex" and audit["window"] is not None
                and state.get("window") == audit["window"]):
            report["context_window"] = audit["window"]
            report["window_source"] = "codex"
        if status != "latest_report":
            reasons.append(status)

    report["snapshot_status"] = status
    measured = (consistent and has_usage and status == "latest_report" and
                bool(audit["model"]) and report["context_window"] is not None)
    action = "continue" if measured else "unknown"
    if measured:
        used, window = report["used_tokens"], report["context_window"]
        report["fill_percent"] = used * 100 / window
        if used * 100 >= window * POLICY["required_fill_percent"]:
            action = "required"
            reasons.append("context_at_or_above_90_percent")
        elif used * 100 >= window * POLICY["prepare_fill_percent"]:
            action = "prepare"
            reasons.append("context_at_or_above_80_percent")
    else:
        if report["context_window"] is None:
            reasons.append("no_measured_window_for_usage_snapshot")
        if not audit["model"]:
            reasons.append("model_identity_unknown")
    if audit["compactions"] >= POLICY["compaction_limit"]:
        action = "required"
        reasons.append("second_compaction_requires_handoff")
    report["rotation"] = {"action": action, "reasons": reasons}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True, help="This role's actual Codex session ID")
    parser.add_argument("--session", help="Explicit JSONL transcript; its metadata must match the ID")
    parser.add_argument("--gauge-path", type=Path, help="Explicit installed context-gauge reader path")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass
    result = read_policy(args.session_id, args.session, args.gauge_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
