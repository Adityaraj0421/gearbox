#!/usr/bin/env python3
"""Gearbox routing logger.

PostToolUse hook for the Task tool. Reads the hook event JSON from stdin and
appends one line per delegation to .claude/gearbox-log.jsonl in the PROJECT
directory (cwd), not the plugin directory — the telemetry belongs to the repo
being worked on.

This log is the seed data for a future learned router (contextual bandit over
{model x tier} with reward = success/cost). Verify the exact hook input schema
against your Claude Code version's hooks docs if fields come back empty.

0.2.0 adds two outcome-oriented fields derived purely from subagent_type (no
schema dependency, so they are always present going forward):
  * is_named_tier — True iff a namespaced gearbox: tier agent handled the work.
  * fallback      — True iff a generic proxy agent (general-purpose / Explore)
                    handled it instead, i.e. the rule-8 fallback path. This
                    turns "half my traffic bypassed named agents" from a guess
                    into a hard count.
"""
import json
import sys
import time
from pathlib import Path

# Generic proxy agents the routing policy falls back to when a named gearbox:
# agent is unavailable (routing.md rule 8: scout->Explore, others->general-purpose).
PROXY_AGENTS = {"general-purpose", "explore"}


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return  # never block the session on logger failure

    tool_input = event.get("tool_input", {}) or {}
    subagent_type = tool_input.get("subagent_type", "") or ""
    record = {
        "ts": int(time.time()),
        "session_id": event.get("session_id", ""),
        "tool_name": event.get("tool_name", ""),
        "subagent_type": subagent_type,
        "is_named_tier": subagent_type.startswith("gearbox:"),
        "fallback": subagent_type.strip().lower() in PROXY_AGENTS,
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
