"""Gearbox hook-payload probe (0.2.4). Imported by every Gearbox hook script.

The next telemetry fixes (a completion record carrying tokens + files_modified,
and an exact verdict->delegation join) depend on facts nobody has checked: which
ids each hook receives, whether a background agent's PostToolUse fires before
its usage exists, and whether SubagentStop can be linked back to the dispatch
that started it. This probe records the SHAPE of every hook payload so those
questions are answered from data rather than guessed.

OFF by default. Turn it on per project with:

    touch .claude/gearbox-probe.on

and off by deleting that file (no restart needed — every hook checks on each
run). Records go to .claude/gearbox-probe.jsonl.

PRIVACY. No prompt, response, path or message text is written. Each record is:
  * the key tree of the payload, with every leaf replaced by its type name;
  * values of a short allowlist of non-content keys (event/tool/agent names,
    model, run_in_background, status);
  * id-like values (keys ending in "id"/"Id") as an 8-char sha1, so the same id
    can be matched across hooks without the id itself being stored;
  * for *_path keys, only whether the file exists.

Never raises: a probe must not break a session.
"""
import hashlib
import json
import time
from pathlib import Path

PROBE_FLAG = Path(".claude") / "gearbox-probe.on"
PROBE_LOG = Path(".claude") / "gearbox-probe.jsonl"

# Scalar values safe to record verbatim: names and flags, never content.
SAFE_VALUE_KEYS = {"hook_event_name", "tool_name", "subagent_type", "agent_type",
                   "run_in_background", "model", "status", "source",
                   "isAsync", "is_async", "stop_hook_active"}
MAX_DEPTH = 4


def _is_id_key(k):
    return k == "id" or k.endswith("_id") or k.endswith("Id")


def _shape(v, depth=0):
    """Replace every leaf with its type name; keep structure to MAX_DEPTH."""
    if isinstance(v, dict):
        if depth >= MAX_DEPTH:
            return "dict"
        return {k: _shape(x, depth + 1) for k, x in v.items()}
    if isinstance(v, list):
        if depth >= MAX_DEPTH or not v:
            return f"list[{len(v)}]"
        # one sample element is enough to show the element shape
        return [f"len={len(v)}", _shape(v[0], depth + 1)]
    return type(v).__name__


def _collect(v, safe, ids, paths, prefix=""):
    """Walk the payload, pulling allowlisted values, hashed ids, path existence."""
    if isinstance(v, dict):
        for k, x in v.items():
            key = f"{prefix}{k}"
            if isinstance(x, (dict, list)):
                _collect(x, safe, ids, paths, key + ".")
            elif k in SAFE_VALUE_KEYS and isinstance(x, (str, bool, int, float)):
                safe[key] = x if not isinstance(x, str) else x[:80]
            elif _is_id_key(k) and isinstance(x, (str, int)) and x != "":
                ids[key] = hashlib.sha1(str(x).encode()).hexdigest()[:8]
            elif k.endswith("_path") and isinstance(x, str):
                paths[key] = bool(x) and Path(x).exists()
    elif isinstance(v, list):
        for x in v[:1]:                               # first element only
            _collect(x, safe, ids, paths, prefix + "[0].")


def probe(event, cwd=None):
    """Append one shape record for this hook payload, if probing is enabled."""
    try:
        if not isinstance(event, dict):
            return
        base = Path(cwd or event.get("cwd") or ".")
        if not (base / PROBE_FLAG).exists():
            return
        safe, ids, paths = {}, {}, {}
        _collect(event, safe, ids, paths)
        record = {
            "ts": time.time(),
            "hook_event_name": event.get("hook_event_name", ""),
            "values": safe,
            "id_hashes": ids,
            "path_exists": paths,
            "shape": _shape(event),
        }
        with (base / PROBE_LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # a probe must never break the session
