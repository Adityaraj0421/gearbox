#!/usr/bin/env python3
"""Gearbox session summary writer.

SessionEnd hook. Fires exactly once per session. Reads dispatch records from
~/.claude/gearbox-log.jsonl that match the ending session_id, aggregates them,
and appends one summary record to ~/.claude/gearbox-sessions.jsonl.

Output (stdout/stderr) is ignored by Claude Code; this hook cannot block.
Fail-open: every path exits 0.
"""
import json
import os
import sys
import time
from pathlib import Path


def _log_dir() -> Path:
    """Resolve the directory that holds gearbox log files.

    Mirrors the path used by log-routing.py: Path.home() / ".claude".
    """
    return Path.home() / ".claude"


def summarize(records: list, session_id: str, cwd: str, reason: str, ts: int) -> dict:
    """Aggregate dispatch records for session_id into a summary dict.

    Pure function — no I/O. All records are assumed to already be filtered
    to the target session_id by the caller; any record whose session_id does
    not match is skipped defensively.

    Args:
        records: list of dispatch record dicts (from gearbox-log.jsonl).
        session_id: the session being summarised.
        cwd: working directory from the SessionEnd payload.
        reason: session end reason from the SessionEnd payload.
        ts: unix timestamp (int) to embed in the record.

    Returns:
        A dict with type "session_summary" and all aggregated fields.
    """
    dispatches = 0
    tier_mix: dict = {}
    cost_usd_total: float = 0.0
    cost_estimated = False
    approves = 0
    rejects = 0
    escalations = 0

    for rec in records:
        # Defensive: skip records that belong to a different session.
        if rec.get("session_id") != session_id:
            continue

        dispatches += 1

        # tier bucketing — null/absent → "untiered"
        tier = rec.get("tier")
        bucket = tier if (tier is not None and isinstance(tier, str)) else "untiered"
        tier_mix[bucket] = tier_mix.get(bucket, 0) + 1

        # cost accumulation — skip None values
        cost = rec.get("cost_usd")
        if cost is not None:
            try:
                cost_usd_total += float(cost)
            except (TypeError, ValueError):
                pass

        # cost_estimated: True if ANY record is estimated
        if rec.get("cost_estimated") is True:
            cost_estimated = True

        # verdict counts
        verdict = rec.get("verdict")
        if verdict == "approve":
            approves += 1
        elif verdict == "reject":
            rejects += 1

        # escalation count
        if rec.get("escalation") is True:
            escalations += 1

    return {
        "type": "session_summary",
        "ts": ts,
        "session_id": session_id,
        "cwd": cwd,
        "reason": reason,
        "dispatches": dispatches,
        "tier_mix": tier_mix,
        "cost_usd": round(cost_usd_total, 8),
        "cost_estimated": cost_estimated,
        "approves": approves,
        "rejects": rejects,
        "escalations": escalations,
    }


def main() -> None:
    # Parse the SessionEnd payload from stdin.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    session_id = payload.get("session_id")
    if not session_id:
        sys.exit(0)

    cwd = payload.get("cwd", "")
    reason = payload.get("reason", "")

    # Locate the dispatch log.
    log_dir = _log_dir()
    dispatch_log = log_dir / "gearbox-log.jsonl"

    if not dispatch_log.exists():
        sys.exit(0)

    # Read and filter dispatch records for this session.
    matched: list = []
    try:
        with dispatch_log.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("session_id") == session_id:
                        matched.append(rec)
                except Exception:
                    continue  # skip malformed lines
    except Exception:
        sys.exit(0)

    if not matched:
        sys.exit(0)  # nothing to summarise

    ts = int(time.time())
    summary = summarize(matched, session_id, cwd, reason, ts)

    # Append to the sessions log (separate file from dispatch log).
    sessions_log = log_dir / "gearbox-sessions.jsonl"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with sessions_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never block the session on logger failure

    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        _TS = 1700000000

        # --- mixed session: correct aggregation ---
        records = [
            # T1 builder, has cost, not estimated, verdict=approve, escalation
            {
                "session_id": "sess-A",
                "tier": "T1",
                "cost_usd": 0.005,
                "cost_estimated": False,
                "verdict": "approve",
                "escalation": True,
            },
            # T0 scout, has cost, estimated, verdict=None, no escalation
            {
                "session_id": "sess-A",
                "tier": "T0",
                "cost_usd": 0.001,
                "cost_estimated": True,
                "verdict": None,
                "escalation": False,
            },
            # TV verifier, cost=None (skip it), verdict=reject
            {
                "session_id": "sess-A",
                "tier": "TV",
                "cost_usd": None,
                "cost_estimated": False,
                "verdict": "reject",
                "escalation": False,
            },
            # null tier → "untiered"
            {
                "session_id": "sess-A",
                "tier": None,
                "cost_usd": 0.002,
                "cost_estimated": False,
                "verdict": None,
                "escalation": False,
            },
            # DIFFERENT session — must be excluded
            {
                "session_id": "sess-OTHER",
                "tier": "T2",
                "cost_usd": 99.0,
                "cost_estimated": False,
                "verdict": "approve",
                "escalation": True,
            },
        ]

        result = summarize(records, "sess-A", "/home/user/proj", "normal", _TS)

        assert result["type"] == "session_summary", f"wrong type: {result['type']!r}"
        assert result["ts"] == _TS, f"wrong ts: {result['ts']}"
        assert result["session_id"] == "sess-A", f"wrong session_id: {result['session_id']!r}"
        assert result["cwd"] == "/home/user/proj", f"wrong cwd: {result['cwd']!r}"
        assert result["reason"] == "normal", f"wrong reason: {result['reason']!r}"

        # dispatches: 4 matching records (sess-OTHER excluded)
        assert result["dispatches"] == 4, f"expected 4 dispatches, got {result['dispatches']}"

        # tier_mix: T1=1, T0=1, TV=1, untiered=1 — sess-OTHER (T2) excluded
        assert result["tier_mix"].get("T1") == 1, f"T1 count wrong: {result['tier_mix']}"
        assert result["tier_mix"].get("T0") == 1, f"T0 count wrong: {result['tier_mix']}"
        assert result["tier_mix"].get("TV") == 1, f"TV count wrong: {result['tier_mix']}"
        assert result["tier_mix"].get("untiered") == 1, f"untiered count wrong: {result['tier_mix']}"
        assert "T2" not in result["tier_mix"], f"T2 must not appear (other session): {result['tier_mix']}"

        # cost_usd: 0.005 + 0.001 + 0.002 = 0.008 (None skipped; 99.0 excluded)
        expected_cost = round(0.005 + 0.001 + 0.002, 8)
        assert result["cost_usd"] == expected_cost, \
            f"expected cost_usd={expected_cost}, got {result['cost_usd']}"

        # cost_estimated: True because one record has cost_estimated=True
        assert result["cost_estimated"] is True, \
            f"expected cost_estimated=True, got {result['cost_estimated']}"

        # approves=1, rejects=1, escalations=1
        assert result["approves"] == 1, f"expected approves=1, got {result['approves']}"
        assert result["rejects"] == 1, f"expected rejects=1, got {result['rejects']}"
        assert result["escalations"] == 1, f"expected escalations=1, got {result['escalations']}"

        # --- zero-match: summarize on empty list → dispatches=0 ---
        empty_result = summarize([], "sess-EMPTY", "/tmp", "timeout", _TS)
        assert empty_result["dispatches"] == 0, \
            f"expected dispatches=0 for empty list, got {empty_result['dispatches']}"
        assert empty_result["tier_mix"] == {}, \
            f"expected empty tier_mix, got {empty_result['tier_mix']}"
        assert empty_result["cost_usd"] == 0.0, \
            f"expected cost_usd=0.0 for empty list, got {empty_result['cost_usd']}"
        assert empty_result["cost_estimated"] is False, \
            f"expected cost_estimated=False for empty list, got {empty_result['cost_estimated']}"

        # --- records from different session only → zero match after filtering ---
        other_only = [
            {"session_id": "sess-X", "tier": "T0", "cost_usd": 1.0,
             "cost_estimated": False, "verdict": "approve", "escalation": False},
        ]
        other_result = summarize(other_only, "sess-MINE", "/tmp", "normal", _TS)
        assert other_result["dispatches"] == 0, \
            f"expected 0 dispatches when all records belong to another session, got {other_result['dispatches']}"

        # --- absent tier field → "untiered" ---
        no_tier_records = [
            {"session_id": "sess-B", "cost_usd": 0.003,
             "cost_estimated": False, "verdict": None, "escalation": False},
        ]
        nt_result = summarize(no_tier_records, "sess-B", "/tmp", "normal", _TS)
        assert nt_result["tier_mix"].get("untiered") == 1, \
            f"missing tier key must bucket as 'untiered': {nt_result['tier_mix']}"

        print("selfcheck OK")
        sys.exit(0)

    main()
