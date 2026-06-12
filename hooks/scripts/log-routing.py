#!/usr/bin/env python3
"""Gearbox routing logger.

PostToolUse hook for the Task tool. Reads the hook event JSON from stdin and
appends one line per delegation to .claude/gearbox-log.jsonl in the PROJECT
directory (cwd), not the plugin directory — the telemetry belongs to the repo
being worked on.

This log is the seed data for a future learned router (contextual bandit over
{model x tier} with reward = success/cost). Verify the exact hook input schema
against your Claude Code version's hooks docs if fields come back empty.
"""
import json
import sys
import time
from pathlib import Path


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return  # never block the session on logger failure

    tool_input = event.get("tool_input", {}) or {}
    record = {
        "ts": int(time.time()),
        "session_id": event.get("session_id", ""),
        "subagent_type": tool_input.get("subagent_type", ""),
        "model": tool_input.get("model", "(not passed)"),
        "prompt_head": (tool_input.get("prompt", "") or "")[:200],
        "cwd": event.get("cwd", ""),
    }

    log_path = Path(event.get("cwd") or ".") / ".claude" / "gearbox-log.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the session


if __name__ == "__main__":
    main()
