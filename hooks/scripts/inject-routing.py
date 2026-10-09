#!/usr/bin/env python3
"""Gearbox session-start context injector.

SessionStart hook. Injects the gearbox routing policy into every session's
context window so the orchestrator has the tier table and routing rules
available automatically, without requiring project-level CLAUDE.md changes.

If the project has a .claude/routing.md (placed by /gearbox:init), that file
takes precedence — it may be a customised local copy. Falls back to the plugin
copy.

0.2.4 also appends a {"event": "session_start"} record to the project's
gearbox-log.jsonl. Without it, a session that never delegated leaves no trace,
so "routing was active but never used" could not be told apart from "the
session never happened". The analyzer reports sessions with zero delegations
from these records.
"""
import json
import os
import sys
import time
from pathlib import Path

try:
    from gearbox_probe import probe
except Exception:  # probe is optional; never let it break a hook
    def probe(event, cwd=None):
        pass


def _read_event():
    """SessionStart sends a JSON payload on stdin; tolerate its absence."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def _log_session_start(event, cwd):
    record = {
        "event": "session_start",
        "ts": int(time.time()),
        "session_id": event.get("session_id", ""),
        "source": event.get("source", ""),   # startup / resume / clear / compact
    }
    log_path = Path(cwd) / ".claude" / "gearbox-log.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the session


def main() -> None:
    event = _read_event()
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    # Log where the other hooks log (the event's cwd), so session_start lines
    # land in the same file as that session's delegations.
    log_cwd = event.get("cwd") or cwd
    probe(event, log_cwd)
    _log_session_start(event, log_cwd)

    # Prefer a project-local copy (placed by /gearbox:init), then plugin copy.
    candidates = [
        Path(cwd) / ".claude" / "routing.md",
        Path(plugin_root) / "routing" / "routing.md",
    ]
    routing_file = next((p for p in candidates if p.exists()), None)

    if routing_file is None:
        return  # never block session startup

    try:
        content = routing_file.read_text(encoding="utf-8")
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        }
        print(json.dumps(output))
    except Exception:
        pass  # never block session startup


if __name__ == "__main__":
    main()
