#!/usr/bin/env python3
"""Gearbox status-line segment: estimated savings vs. running everything on Opus.

Reads the statusLine JSON from stdin (Claude Code statusLine command protocol).
Prints a compact segment string to stdout (no trailing newline) for use in a
composed status line.  Fail-open: any parse / IO error → silent exit 0.

Wiring (user's settings.json):
  "statusLine": "python3 /path/to/gearbox/bench/statusline.py"

Or pipe the same JSON to this script alongside other segment producers and
concatenate their outputs in your own shell wrapper.

Unit mode (env var GEARBOX_STATUSLINE_UNIT):
  usd    (default) → "gearbox saved $0.43"
  tokens            → "gearbox saved 840k tok"  (Haiku-equivalent weighted tokens)
  Anything else     → treated as "usd" (fail-open).

Savings computation:
  actual        = sum of cost_usd over this session's dispatch rows in gearbox-log.jsonl
  counterfactual = re-price each row's token split at Opus rates
  savings       = counterfactual − actual

  Caveat: re-pricing the recorded token counts at Opus rates is an estimate
  (actual token counts would differ on Opus). Consistent with the existing
  cost_estimated framing in the log.

# ponytail: full-file scan of the global log per refresh; add a tail/index if
# the log grows large enough that the scan time exceeds the debounce window.
"""
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Rate constants
# ---------------------------------------------------------------------------

# ponytail: Opus per-component USD-per-million-tokens rates sourced from
# hooks/scripts/log-routing.py _TOKEN_RATES["opus"], 2026-06 Anthropic rate card.
# Must stay in sync with that table when pricing changes.
#   hooks/scripts/log-routing.py line 50:
#     "opus": {"input": 5.00, "output": 25.00, "cache_read": 0.50,
#              "cache_write_5m": 6.25, "cache_write_1h": 10.00}
_OPUS_RATES = {
    "input": 5.00,
    "output": 25.00,
    "cache_read": 0.50,
    "cache_write_5m": 6.25,
    "cache_write_1h": 10.00,
}

# ponytail: Haiku input rate (USD per million tokens) sourced from
# hooks/scripts/budget_common.py HAIKU_REF == 1.00.
# Must stay in sync with that constant when pricing changes.
#   hooks/scripts/budget_common.py line 18: HAIKU_REF = 1.00
_HAIKU_REF = 1.00  # USD per million tokens (used as denominator for weighted tokens)


def _counterfactual_cost(rec: dict) -> "float | None":
    """Re-price one log record's token split at Opus rates.

    Returns cost in USD, or None if no token split components are present.
    When the cache-creation sub-breakdown (5m/1h) is absent, the full
    cache_creation_tokens amount is billed at the 5m write rate — matching
    the same fallback used in log-routing.py _exact_cost.
    """
    in_t = rec.get("input_tokens")
    out_t = rec.get("output_tokens")
    cr_t = rec.get("cache_read_tokens")
    cc_t = rec.get("cache_creation_tokens")

    if in_t is None and out_t is None and cr_t is None and cc_t is None:
        return None

    # Use 5m rate for all cache-creation when no finer breakdown is available.
    cost = (
        (in_t or 0) * _OPUS_RATES["input"]
        + (out_t or 0) * _OPUS_RATES["output"]
        + (cr_t or 0) * _OPUS_RATES["cache_read"]
        + (cc_t or 0) * _OPUS_RATES["cache_write_5m"]
    ) / 1e6
    return cost


def _compact_tokens(n: float) -> str:
    """Format a token count with k/M suffix for thousands/millions."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        v = n / 1_000
        # Drop the .0 for clean integers (e.g. 840k not 840.0k)
        if v == int(v):
            return f"{int(v)}k"
        return f"{v:.1f}k"
    return str(int(n))


def build_segment(
    records: list,
    session_id: str,
    unit: str,
    color: bool,
) -> str:
    """Build the status-line segment string.  Pure function — no I/O.

    Args:
        records:    All log records (list of dicts).  May be empty.
        session_id: The current session ID to filter on.
        unit:       "usd" (money savings) or "tokens" (Haiku-equivalent savings).
        color:      Emit truecolor ANSI codes when True.

    Returns:
        The segment string (no trailing newline), or "" when there are no
        matched dispatches (caller should treat "" as "nothing to render").
    """
    actual_total = 0.0
    counterfactual_total = 0.0
    matched = 0

    for rec in records:
        if rec.get("session_id") != session_id:
            continue
        cost = rec.get("cost_usd")
        if cost is None:
            continue
        cf = _counterfactual_cost(rec)
        if cf is None:
            # No token split — skip this record; we cannot estimate savings.
            continue
        actual_total += float(cost)
        counterfactual_total += cf
        matched += 1

    if matched == 0:
        return ""

    savings_usd = counterfactual_total - actual_total

    if unit == "tokens":
        # Convert USD savings to Haiku-equivalent weighted tokens.
        savings_tok = savings_usd * 1e6 / _HAIKU_REF
        value_str = f"{_compact_tokens(savings_tok)} tok"
    else:
        value_str = f"${savings_usd:.2f}"

    label = "gearbox saved"

    if color:
        GREEN = "\x1b[38;2;80;200;120m"
        DIM   = "\x1b[2m"
        RESET = "\x1b[0m"
        return f"{DIM}{label}{RESET} {GREEN}{value_str}{RESET}"
    else:
        return f"{label} {value_str}"


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _selfcheck() -> None:
    """Assert-based tests.  Exits 0 on success, non-zero on assertion failure."""

    # --- zero matches → empty string ---
    seg_empty = build_segment([], "NOSUCHSESSION", "usd", color=False)
    assert seg_empty == "", f"zero match must return '': {seg_empty!r}"

    # --- records with no cost_usd are skipped ---
    recs_nocost = [
        {"session_id": "S1", "cost_usd": None, "input_tokens": 100, "output_tokens": 50},
        {"session_id": "S1"},  # no cost_usd key
    ]
    seg_nocost = build_segment(recs_nocost, "S1", "usd", color=False)
    assert seg_nocost == "", f"no cost_usd → must return '': {seg_nocost!r}"

    # --- records with no token split are skipped (can't compute counterfactual) ---
    recs_nosplit = [
        {"session_id": "S1", "cost_usd": 0.01},  # no token split
    ]
    seg_nosplit = build_segment(recs_nosplit, "S1", "usd", color=False)
    assert seg_nosplit == "", f"no token split → must return '': {seg_nosplit!r}"

    # --- session filtering: only matching session_id rows are used ---
    recs_mixed = [
        {"session_id": "S1", "cost_usd": 0.001,
         "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cache_creation_tokens": 0},
        {"session_id": "S2", "cost_usd": 0.999,
         "input_tokens": 10000, "output_tokens": 5000, "cache_read_tokens": 0, "cache_creation_tokens": 0},
    ]
    seg_s1 = build_segment(recs_mixed, "S1", "usd", color=False)
    # S1 only: actual=0.001, counterfactual = (100*5.00 + 50*25.00)/1e6 = (500+1250)/1e6 = 0.00175
    # savings = 0.00175 - 0.001 = 0.00075
    assert seg_s1 == "gearbox saved $0.00", f"S1 savings (rounds to $0.00): {seg_s1!r}"
    assert "S2" not in seg_s1, "S2 must not appear in S1 segment"

    # --- USD mode: savings math correctness ---
    # haiku scout: input=1000, output=200, cache_read=500, cache_creation=100
    # actual cost at haiku rates (from log): 1000*1.00/1e6 + 200*5.00/1e6 + 500*0.10/1e6 + 100*1.25/1e6
    #   = (1000 + 1000 + 50 + 125) / 1e6 = 2175 / 1e6 = 0.002175
    # counterfactual at opus rates: 1000*5.00/1e6 + 200*25.00/1e6 + 500*0.50/1e6 + 100*6.25/1e6
    #   = (5000 + 5000 + 250 + 625) / 1e6 = 10875 / 1e6 = 0.010875
    # savings = 0.010875 - 0.002175 = 0.0087
    recs_math = [
        {
            "session_id": "S1",
            "cost_usd": 0.002175,
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 500,
            "cache_creation_tokens": 100,
        }
    ]
    seg_math = build_segment(recs_math, "S1", "usd", color=False)
    assert seg_math == "gearbox saved $0.01", f"savings math USD: {seg_math!r}"

    # --- tokens mode: same data, Haiku-equivalent weighted tokens ---
    # savings_usd = 0.0087 (from above)
    # savings_tok = 0.0087 * 1e6 / 1.00 = 8700
    seg_tok = build_segment(recs_math, "S1", "tokens", color=False)
    assert seg_tok == "gearbox saved 8.7k tok", f"savings math tokens: {seg_tok!r}"

    # --- tokens mode: large savings → M suffix ---
    recs_large = [
        {
            "session_id": "S1",
            "cost_usd": 0.0,
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
    ]
    # counterfactual = 1_000_000 * 5.00 / 1e6 = 5.0; savings_tok = 5.0 * 1e6 = 5_000_000
    seg_large = build_segment(recs_large, "S1", "tokens", color=False)
    assert seg_large == "gearbox saved 5.0M tok", f"large tokens M suffix: {seg_large!r}"

    # --- tokens mode: k suffix clean integer ---
    recs_1k = [
        {
            "session_id": "S1",
            "cost_usd": 0.0,
            "input_tokens": 200,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
    ]
    # counterfactual = 200 * 5.00 / 1e6 = 0.001; savings_tok = 0.001 * 1e6 = 1000 → "1k"
    seg_1k = build_segment(recs_1k, "S1", "tokens", color=False)
    assert seg_1k == "gearbox saved 1k tok", f"k suffix clean int: {seg_1k!r}"

    # --- savings ≤ 0: still shown ---
    recs_zero = [
        {
            "session_id": "S1",
            "cost_usd": 99.0,  # wildly high actual cost
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
    ]
    seg_zero = build_segment(recs_zero, "S1", "usd", color=False)
    assert seg_zero.startswith("gearbox saved"), f"negative savings still shown: {seg_zero!r}"

    # --- NO_COLOR / color=False → no ANSI escapes ---
    seg_plain = build_segment(recs_math, "S1", "usd", color=False)
    assert "\x1b" not in seg_plain, f"ANSI escape found in plain output: {seg_plain!r}"

    # --- color=True → ANSI escapes present ---
    seg_color = build_segment(recs_math, "S1", "usd", color=True)
    assert "\x1b" in seg_color, f"ANSI escape expected in color output: {seg_color!r}"

    # --- unknown unit falls back to usd ---
    seg_unknown = build_segment(recs_math, "S1", "badunit", color=False)
    assert "$" in seg_unknown, f"unknown unit must behave as usd: {seg_unknown!r}"

    # --- multiple records summed ---
    recs_multi = [
        {
            "session_id": "S1",
            "cost_usd": 0.001,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
        {
            "session_id": "S1",
            "cost_usd": 0.002,
            "input_tokens": 200,
            "output_tokens": 100,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        },
    ]
    # actual = 0.003
    # counterfactual: rec1=(100*5+50*25)/1e6=(500+1250)/1e6=0.00175
    #                 rec2=(200*5+100*25)/1e6=(1000+2500)/1e6=0.0035
    #                 total=0.00525
    # savings = 0.00525 - 0.003 = 0.00225 → $0.00
    seg_multi = build_segment(recs_multi, "S1", "usd", color=False)
    assert seg_multi.startswith("gearbox saved $"), f"multi-record segment: {seg_multi!r}"

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
    except Exception:
        sys.exit(0)  # fail-open: never error on bad input

    if not session_id:
        sys.exit(0)

    # Read GEARBOX_STATUSLINE_UNIT; fail-open to "usd".
    raw_unit = os.environ.get("GEARBOX_STATUSLINE_UNIT", "usd").strip().lower()
    unit = raw_unit if raw_unit in ("usd", "tokens") else "usd"

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

    segment = build_segment(records, session_id, unit, color=use_color)
    if segment:
        sys.stdout.write(segment)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        _selfcheck()
    main()
