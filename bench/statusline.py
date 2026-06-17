#!/usr/bin/env python3
"""Gearbox status-line segment: current-session tier/role distribution + running cost.

Reads the statusLine JSON from stdin (Claude Code statusLine command protocol).
Prints a compact segment string to stdout (no trailing newline) for use in a
composed status line.  Fail-open: any parse / IO error → silent exit 0.

Wiring (user's settings.json):
  "statusLine": "python3 /path/to/gearbox/bench/statusline.py"

Or pipe the same JSON to this script alongside other segment producers and
concatenate their outputs in your own shell wrapper.

# ponytail: full-file scan of the global log per refresh; add a tail/index if
# the log grows large enough that the scan time exceeds the debounce window.
"""
import json
import os
import sys
from collections import Counter
from pathlib import Path

# Canonical role order for tie-breaking (matches gearbox tier ladder).
_CANONICAL_ORDER = ["scout", "grunt", "builder", "architect", "verifier"]


def _canonical_rank(role: str) -> tuple:
    """Sort key: (canonical-index or large-int, role) — canonical first, then alpha."""
    try:
        return (_CANONICAL_ORDER.index(role), role)
    except ValueError:
        return (len(_CANONICAL_ORDER), role)


def build_segment(
    records: list,
    session_id: str,
    total_cost_usd: "float | None",
    color: bool,
) -> str:
    """Build the status-line segment string.  Pure function — no I/O.

    Args:
        records:        All log records (list of dicts).  May be empty.
        session_id:     The current session ID to filter on.
        total_cost_usd: Running session cost in USD, or None if unavailable.
        color:          Emit truecolor ANSI codes when True.

    Returns:
        The segment string (no trailing newline), or "" when there are no
        matched dispatches (caller should treat "" as "nothing to render").
    """
    # Filter to this session; skip records with no subagent_type.
    counts: Counter = Counter()
    for rec in records:
        if rec.get("session_id") != session_id:
            continue
        raw = rec.get("subagent_type") or ""
        if not raw:
            continue
        # Strip "gearbox:" prefix to get a short role name.
        role = raw.removeprefix("gearbox:")
        counts[role] += 1

    if not counts:
        return ""

    # Sort by count descending; tie-break by canonical order then alpha.
    sorted_roles = sorted(counts.keys(), key=lambda r: (-counts[r], _canonical_rank(r)))

    if color:
        # Truecolor ANSI: dim brackets/×, muted blue for roles, green for cost.
        DIM    = "\x1b[2m"
        ROLE_C = "\x1b[38;2;100;160;220m"
        COST_C = "\x1b[38;2;80;200;120m"
        RESET  = "\x1b[0m"

        parts = []
        for role in sorted_roles:
            n = counts[role]
            parts.append(f"{ROLE_C}{role}{DIM}×{RESET}{ROLE_C}{n}{RESET}")

        inner = f"{DIM}[{RESET}" + f"{DIM} {RESET}".join(parts) + f"{DIM}]{RESET}"

        if total_cost_usd is not None:
            inner += f" {COST_C}${total_cost_usd:.2f}{RESET}"
    else:
        parts = [f"{role}×{counts[role]}" for role in sorted_roles]
        inner = "[" + " ".join(parts) + "]"
        if total_cost_usd is not None:
            inner += f" ${total_cost_usd:.2f}"

    return inner


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _selfcheck() -> None:
    """Assert-based tests.  Exits 0 on success, non-zero on assertion failure."""

    # --- session filtering: only matching session_id rows are counted ---
    recs = [
        {"session_id": "S1", "subagent_type": "gearbox:builder"},
        {"session_id": "S1", "subagent_type": "gearbox:builder"},
        {"session_id": "S1", "subagent_type": "gearbox:scout"},
        {"session_id": "S2", "subagent_type": "gearbox:architect"},  # different session
        {"session_id": "S1", "subagent_type": "gearbox:architect"},
    ]

    seg = build_segment(recs, "S1", None, color=False)
    # S1: builder×2, scout×1, architect×1  → builder first (count 2), then scout
    # vs architect tie-break: scout before architect (canonical order)
    assert seg.startswith("[builder×2"), f"expected builder first: {seg!r}"
    assert "scout×1" in seg, f"scout missing: {seg!r}"
    assert "architect×1" in seg, f"architect missing: {seg!r}"
    assert "S2" not in seg, "S2 session must not appear"
    assert "architect×1" not in seg.split("scout×1")[0] if "scout×1" in seg else True, \
        "scout must appear before architect (canonical tie-break)"

    # More explicit tie-break check: scout < architect in canonical order
    idx_scout = seg.index("scout×1")
    idx_arch  = seg.index("architect×1")
    assert idx_scout < idx_arch, f"scout must precede architect in tie-break: {seg!r}"

    # --- "gearbox:" prefix stripped ---
    recs2 = [{"session_id": "S1", "subagent_type": "gearbox:verifier"}]
    seg2 = build_segment(recs2, "S1", None, color=False)
    assert "verifier×1" in seg2, f"prefix not stripped: {seg2!r}"
    assert "gearbox:" not in seg2, f"raw prefix leaked: {seg2!r}"

    # --- zero matches → empty string ---
    seg_empty = build_segment(recs, "NOSUCHSESSION", None, color=False)
    assert seg_empty == "", f"zero match must return '': {seg_empty!r}"

    # --- cost appended when present ---
    recs3 = [{"session_id": "S1", "subagent_type": "gearbox:builder"}]
    seg3 = build_segment(recs3, "S1", 2.43, color=False)
    assert seg3.endswith("$2.43"), f"cost suffix missing: {seg3!r}"

    # --- cost omitted when None ---
    seg4 = build_segment(recs3, "S1", None, color=False)
    assert "$" not in seg4, f"cost must be absent when None: {seg4!r}"

    # --- NO_COLOR / color=False → no ANSI escapes ---
    recs5 = [{"session_id": "S1", "subagent_type": "gearbox:grunt"}]
    seg5 = build_segment(recs5, "S1", 1.00, color=False)
    assert "\x1b" not in seg5, f"ANSI escape found in plain output: {seg5!r}"

    # --- color=True → ANSI escapes present ---
    seg6 = build_segment(recs5, "S1", 1.00, color=True)
    assert "\x1b" in seg6, f"ANSI escape expected in color output: {seg6!r}"

    # --- passthrough role name kept verbatim (no "gearbox:" prefix to strip) ---
    recs7 = [
        {"session_id": "S1", "subagent_type": "general-purpose"},
        {"session_id": "S1", "subagent_type": "general-purpose"},
        {"session_id": "S1", "subagent_type": "Explore"},
    ]
    seg7 = build_segment(recs7, "S1", None, color=False)
    assert "general-purpose×2" in seg7, f"passthrough role missing: {seg7!r}"
    assert "Explore×1" in seg7, f"Explore role missing: {seg7!r}"

    # --- descending count ordering ---
    recs8 = [
        {"session_id": "S1", "subagent_type": "gearbox:scout"},
        {"session_id": "S1", "subagent_type": "gearbox:scout"},
        {"session_id": "S1", "subagent_type": "gearbox:scout"},
        {"session_id": "S1", "subagent_type": "gearbox:builder"},
        {"session_id": "S1", "subagent_type": "gearbox:builder"},
        {"session_id": "S1", "subagent_type": "gearbox:architect"},
    ]
    seg8 = build_segment(recs8, "S1", None, color=False)
    idx_s = seg8.index("scout×3")
    idx_b = seg8.index("builder×2")
    idx_a = seg8.index("architect×1")
    assert idx_s < idx_b < idx_a, f"must be descending count order: {seg8!r}"

    # --- records with no subagent_type are skipped ---
    recs9 = [
        {"session_id": "S1", "subagent_type": "gearbox:builder"},
        {"session_id": "S1"},                              # no subagent_type key
        {"session_id": "S1", "subagent_type": ""},         # empty string
        {"session_id": "S1", "subagent_type": None},       # explicit None
    ]
    seg9 = build_segment(recs9, "S1", None, color=False)
    assert "builder×1" in seg9, f"builder record missing: {seg9!r}"
    # Only builder should appear; no phantom empty-role entries
    assert seg9 == "[builder×1]", f"unexpected content in seg9: {seg9!r}"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Main (stdin / file I/O)
# ---------------------------------------------------------------------------

def main() -> None:
    # Parse statusLine JSON from stdin.
    try:
        data = json.load(sys.stdin)
        session_id = data.get("session_id") or ""
        cost_obj = data.get("cost") or {}
        total_cost_usd = cost_obj.get("total_cost_usd")
        if total_cost_usd is not None:
            total_cost_usd = float(total_cost_usd)
    except Exception:
        sys.exit(0)  # fail-open: never error on bad input

    if not session_id:
        sys.exit(0)

    # Load the global gearbox log (same path as log-routing.py / dashboard.py).
    log_path = Path.home() / ".claude" / "gearbox-log.jsonl"
    records = []
    if log_path.exists():
        try:
            with log_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass  # treat unreadable log as empty

    # Gate color on NO_COLOR env var only (stdout-is-TTY is unreliable in pipe).
    use_color = "NO_COLOR" not in os.environ

    segment = build_segment(records, session_id, total_cost_usd, color=use_color)
    if segment:
        sys.stdout.write(segment)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        _selfcheck()
    main()
