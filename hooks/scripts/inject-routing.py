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
import time
from pathlib import Path

# ponytail: discard routing-prior artifact if older than this many days
FRESH_DAYS = 30

_PRIOR_PATH = os.path.expanduser("~/.claude/gearbox-recommendations.md")


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

        # Append routing-prior artifact if it exists and is fresh.
        try:
            prior_path = Path(_PRIOR_PATH)
            age_seconds = time.time() - prior_path.stat().st_mtime
            if age_seconds <= FRESH_DAYS * 86400:
                prior_text = prior_path.read_text(encoding="utf-8")
                content = content + "\n" + prior_text
        except Exception:
            pass  # silently fall back to policy-only

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
