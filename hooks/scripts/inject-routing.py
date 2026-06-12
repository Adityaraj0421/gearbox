#!/usr/bin/env python3
"""Gearbox session-start context injector.

SessionStart hook. Injects the gearbox routing policy into every session's
context window so the orchestrator has the tier table and routing rules
available automatically, without requiring project-level CLAUDE.md changes.

If the project has a .claude/routing.md (placed by /gearbox:init), that file
takes precedence — it may be a customised local copy. Falls back to the plugin
copy.
"""
import json
import os
from pathlib import Path


def main() -> None:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

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
