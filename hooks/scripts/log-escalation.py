#!/usr/bin/env python3
"""Gearbox escalation logger (0.2.1). PreToolUse hook for Task|Agent.

No hook can *infer* that an untagged re-dispatch is an escalation of a previous
task — that stays true. But it can log one reliably if the orchestrator marks
it: routing.md rule 3 now requires an escalation Task's prompt to carry the
marker  [GEARBOX-ESCALATE from=T0 to=T1]. This hook detects that marker and
appends the escalation event automatically, moving the logging out of a
hand-written Bash command (which produced 0 events across the entire 0.2.0
window) and into a hook.

The marker's *presence* still depends on orchestrator compliance, so this is not
a fully autonomous signal — /gearbox:doctor CHECK 10 flags logs that have T1/T2
volume but zero escalation events, catching the case where rule 3 is ignored.
"""
import json
import re
import sys
import time
from pathlib import Path

try:
    from gearbox_probe import probe
except Exception:  # probe is optional; never let it break a hook
    def probe(event, cwd=None):
        pass

# e.g. [GEARBOX-ESCALATE from=T0 to=T1]  (tiers T0..T2, case-insensitive)
MARKER = re.compile(r"\[GEARBOX-ESCALATE\s+from=(T[0-2])\s+to=(T[0-2])\]", re.I)


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return  # never block the session on logger failure
    probe(event)

    prompt = ((event.get("tool_input") or {}).get("prompt") or "")
    m = MARKER.search(prompt)
    if not m:
        return  # ordinary delegation, not a marked escalation

    record = {
        "event": "escalation",
        "from_tier": m.group(1).upper(),
        "to_tier": m.group(2).upper(),
        "ts": int(time.time()),
        "session_id": event.get("session_id", ""),
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
