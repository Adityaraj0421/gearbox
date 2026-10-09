#!/usr/bin/env python3
"""Gearbox verifier-verdict logger (0.2.0).

SubagentStop hook. When the finishing subagent is gearbox:verifier, this parses
its verdict ("VERDICT: APPROVE" / "VERDICT: REJECT", emitted anywhere in the
verifier's final output per routing.md) and appends one outcome record:

    {"event": "verdict", "verdict": "approve"|"reject", "ts": ..., "session_id": ...}

DEFENSIVE BY DESIGN. The SubagentStop event schema varies across Claude Code
versions. Current docs expose `agent_type` and `last_assistant_message`
directly; older/different surfaces may not. So this script:
  * self-filters on agent identity instead of trusting a hooks.json matcher,
    looking at agent_type, then subagent_type;
  * sources the verdict text from last_assistant_message, then the subagent's
    own transcript (agent_transcript_path), then the main transcript;
  * writes NOTHING (rather than a wrong record) if it cannot positively
    identify the verifier or cannot parse a verdict;
  * never raises — a telemetry hook must never break a session.

If no {"event":"verdict"} lines ever appear in your log, your Claude Code
version likely does not surface the verifier's output to SubagentStop; treat
the verdict as a manual field until it does.
"""
import json
import sys
import time
from pathlib import Path

try:
    from gearbox_probe import probe
except Exception:  # probe is optional; never let it break a hook
    def probe(event, cwd=None):
        pass


def _last_assistant_text(transcript_path: str) -> str:
    """Best-effort: pull the last assistant text message from a JSONL transcript.

    Transcript line shapes differ by version; handle the common ones and bail
    quietly on anything unexpected.
    """
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.exists():
        return ""
    last = ""
    try:
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # role may live at top level or under "message"
            role = msg.get("role") or (msg.get("message") or {}).get("role")
            if role != "assistant":
                continue
            content = msg.get("content")
            if content is None:
                content = (msg.get("message") or {}).get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = ""
            if text.strip():
                last = text
    except OSError:
        return ""
    return last


def _correlate_delegation(cwd: str, session_id: str):
    """Attach the verdict to the delegation it most likely judged.

    SubagentStop's payload identifies the *verifier* that just finished, not the
    T1/T2 delegation it was checking — there is no hard link. So this is a
    HEURISTIC, labelled as such in the record via join_method: pick the
    most-recent delegation in the same session that (a) ran on a T1/T2 model,
    (b) is not itself a verifier, and (c) has not already been claimed by an
    earlier verdict. Returns (delegation_id | None, join_method).
    """
    log_path = Path(cwd or ".") / ".claude" / "gearbox-log.jsonl"
    if not log_path.exists():
        return None, "unmatched"
    claimed, candidates = set(), []
    try:
        for line in log_path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") == "verdict":
                if r.get("delegation_id"):
                    claimed.add(r["delegation_id"])
                continue
            if r.get("event"):                       # e.g. escalation records
                continue
            if r.get("session_id") != session_id:
                continue
            did = r.get("delegation_id")
            if (did and (r.get("model") or "") in ("sonnet", "opus")
                    and "verifier" not in (r.get("subagent_type") or "").lower()):
                candidates.append(did)               # append order = chronological
    except OSError:
        return None, "unmatched"
    for did in reversed(candidates):                 # most-recent unclaimed wins
        if did not in claimed:
            return did, "heuristic:last_unverified_t1t2_same_session"
    return None, "unmatched"


def _verdict_from(text: str):
    """Return 'approve' / 'reject' / None. Earliest marker wins if both appear."""
    if not text:
        return None
    i_app = text.find("VERDICT: APPROVE")
    i_rej = text.find("VERDICT: REJECT")
    if i_app == -1 and i_rej == -1:
        return None
    if i_rej == -1:
        return "approve"
    if i_app == -1:
        return "reject"
    return "approve" if i_app < i_rej else "reject"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return  # never break the session
    probe(event)

    # Identify the finishing subagent. Bail silently if we cannot tell it was
    # the verifier — never attribute a verdict to the wrong agent.
    agent = (event.get("agent_type") or event.get("subagent_type") or "")
    if "verifier" not in agent.lower():
        return

    # Source the verifier's final text, most-direct source first.
    text = event.get("last_assistant_message") or ""
    if not text:
        text = _last_assistant_text(event.get("agent_transcript_path", ""))
    if not text:
        text = _last_assistant_text(event.get("transcript_path", ""))

    verdict = _verdict_from(text)
    if verdict is None:
        return  # verifier finished but no parseable verdict; don't pollute log

    session_id = event.get("session_id", "")
    delegation_id, join_method = _correlate_delegation(event.get("cwd"), session_id)
    record = {
        "event": "verdict",
        "verdict": verdict,
        "delegation_id": delegation_id,   # None when unmatched — never a hard join
        "join_method": join_method,
        "ts": int(time.time()),
        "session_id": session_id,
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
