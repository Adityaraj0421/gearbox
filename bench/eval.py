#!/usr/bin/env python3
"""Routing eval: score labeled dispatches against a modeled always-opus baseline.

Reads bench/training-data.jsonl (or --labels PATH) and prints a per-tier
scorecard: acceptability rate, router cost, modeled baseline cost, and
cost-saved %.  Read-only: no files are written.

The baseline is MODELED (always-opus ceiling) — no second run needed.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical blended USD-per-million-tokens rate for opus, date-pinned 2026-06.
# Mirrors _BLENDED_RATES["opus"] in hooks/scripts/log-routing.py — re-pin both
# together when Anthropic pricing changes.
OPUS_RATE = 45.0


# ---------------------------------------------------------------------------
# Tier derivation
# ---------------------------------------------------------------------------

# Maps bare subagent_type names to routing tiers.  Mirrors _AGENT_ROUTING in
# hooks/scripts/log-routing.py (same keyset, tier values only).
_SUBAGENT_TIER: dict = {
    "scout":     "T0",
    "grunt":     "T0",
    "verifier":  "TV",
    "builder":   "T1",
    "architect": "T2",
}

# Fallback: derive tier from model string when subagent_type is unknown.
_MODEL_TIER: dict = {
    "haiku":  "T0",
    "sonnet": "T1",
    "opus":   "T2",
}


def _derive_tier(row: dict) -> str:
    """Return tier string for a labeled row.

    Prefers subagent_type; falls back to model substring match; returns
    '(unknown)' when neither resolves.
    """
    subagent = (row.get("subagent_type") or "").strip().removeprefix("gearbox:")
    if subagent in _SUBAGENT_TIER:
        return _SUBAGENT_TIER[subagent]

    model = (row.get("model") or "").lower()
    for key, tier in _MODEL_TIER.items():
        if key in model:
            return tier

    return "(unknown)"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_labeled_rows(labels_path: Path) -> list:
    """Read all valid JSON rows from the labeled data file.

    Malformed/blank lines are silently skipped.  Returns [] when the file
    does not exist (callers handle that case).
    """
    rows = []
    if not labels_path.exists():
        return rows
    with labels_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _empty_bucket() -> dict:
    return {
        "n": 0,
        "acceptable_count": 0,
        "router_cost": 0.0,
        "baseline_cost": 0.0,
        "any_estimated": False,
    }


def aggregate(rows: list) -> dict:
    """Return {tier: bucket} for all labeled rows."""
    buckets: dict = defaultdict(_empty_bucket)

    for row in rows:
        tier = _derive_tier(row)
        b = buckets[tier]
        b["n"] += 1

        if row.get("acceptable") is True:
            b["acceptable_count"] += 1

        cost = row.get("cost_usd")
        try:
            b["router_cost"] += float(cost)
        except (TypeError, ValueError):
            pass  # treat null/missing as 0 in sum

        tokens = row.get("total_tokens")
        try:
            # ponytail: modeled baseline — assumes each task's token count is
            # policy-invariant and that the top tier (opus) is always acceptable.
            # This is an estimated ceiling, NOT a measured counterfactual.  A
            # measured baseline would require re-running every task under an
            # always-opus policy — out of scope for v0.3.0.
            b["baseline_cost"] += int(tokens) * OPUS_RATE / 1e6
        except (TypeError, ValueError):
            pass

        if row.get("cost_estimated"):
            b["any_estimated"] = True

    return dict(buckets)


def _total_bucket(buckets: dict) -> dict:
    total = _empty_bucket()
    for b in buckets.values():
        total["n"] += b["n"]
        total["acceptable_count"] += b["acceptable_count"]
        total["router_cost"] += b["router_cost"]
        total["baseline_cost"] += b["baseline_cost"]
        if b["any_estimated"]:
            total["any_estimated"] = True
    return total


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_accept_rate(b: dict) -> str:
    if b["n"] == 0:
        return "n/a"
    return f"{100.0 * b['acceptable_count'] / b['n']:.1f}%"


def _fmt_cost(val: float) -> str:
    return f"${val:.4f}"


def _fmt_saved(b: dict) -> str:
    baseline = b["baseline_cost"]
    if baseline == 0.0:
        return "n/a"
    saved = (baseline - b["router_cost"]) / baseline * 100.0
    return f"{saved:.1f}%"


# ---------------------------------------------------------------------------
# Scorecard printer
# ---------------------------------------------------------------------------

def print_scorecard(buckets: dict) -> bool:
    """Print the aligned per-tier scorecard.  Returns True if any row has
    cost_estimated set (used to decide whether to print the footnote)."""

    rows = []
    for tier in sorted(buckets):
        b = buckets[tier]
        rows.append((
            tier,
            b["n"],
            _fmt_accept_rate(b),
            _fmt_cost(b["router_cost"]),
            _fmt_cost(b["baseline_cost"]),
            _fmt_saved(b),
        ))

    total = _total_bucket(buckets)
    rows.append((
        "TOTAL",
        total["n"],
        _fmt_accept_rate(total),
        _fmt_cost(total["router_cost"]),
        _fmt_cost(total["baseline_cost"]),
        _fmt_saved(total),
    ))

    headers = (
        "tier",
        "n",
        "accept-rate",
        "router-cost",
        "baseline-cost",
        "cost-saved%",
    )

    cols = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in col) for col in cols]

    sep = "  "
    header_line = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))

    for idx, row in enumerate(rows):
        is_total = idx == len(rows) - 1
        if is_total:
            print("-" * len(header_line))
        print(sep.join(str(cell).ljust(widths[j]) for j, cell in enumerate(row)))

    any_estimated = any(b["any_estimated"] for b in buckets.values())
    return any_estimated


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------

def selfcheck() -> None:
    """Assert-based tests on aggregation logic.  Exits 0 on success, non-zero
    on assertion failure."""

    # Synthetic labeled rows spanning T0/T1/T2 with mixed acceptable values.
    rows = [
        # T0 (grunt): acceptable, cost 0.001, 500 tokens
        {"subagent_type": "grunt",    "model": "haiku",  "acceptable": True,
         "cost_usd": 0.001,  "total_tokens": 500,  "cost_estimated": True},
        # T0 (scout): not acceptable, cost 0.002, 1000 tokens
        {"subagent_type": "scout",    "model": "haiku",  "acceptable": False,
         "cost_usd": 0.002,  "total_tokens": 1000, "cost_estimated": False},
        # T1 (builder): acceptable, cost 0.010, 2000 tokens
        {"subagent_type": "builder",  "model": "sonnet", "acceptable": True,
         "cost_usd": 0.010,  "total_tokens": 2000, "cost_estimated": True},
        # T1 (builder): acceptable, null cost, 1000 tokens
        {"subagent_type": "builder",  "model": "sonnet", "acceptable": True,
         "cost_usd": None,   "total_tokens": 1000, "cost_estimated": False},
        # T2 (architect): acceptable, cost 0.100, 4000 tokens
        {"subagent_type": "architect","model": "opus",   "acceptable": True,
         "cost_usd": 0.100,  "total_tokens": 4000, "cost_estimated": False},
        # T2 (architect): not acceptable, cost 0.050, 2000 tokens
        {"subagent_type": "architect","model": "opus",   "acceptable": False,
         "cost_usd": 0.050,  "total_tokens": 2000, "cost_estimated": False},
    ]

    buckets = aggregate(rows)

    # --- T0 ---
    t0 = buckets["T0"]
    assert t0["n"] == 2, f"T0 n: {t0['n']}"
    assert t0["acceptable_count"] == 1, f"T0 accept count: {t0['acceptable_count']}"
    accept_t0 = _fmt_accept_rate(t0)
    assert accept_t0 == "50.0%", f"T0 accept rate: {accept_t0}"
    assert abs(t0["router_cost"] - 0.003) < 1e-9, f"T0 router_cost: {t0['router_cost']}"
    expected_baseline_t0 = (500 + 1000) * OPUS_RATE / 1e6
    assert abs(t0["baseline_cost"] - expected_baseline_t0) < 1e-9, \
        f"T0 baseline_cost: {t0['baseline_cost']} vs {expected_baseline_t0}"
    saved_t0 = _fmt_saved(t0)
    expected_saved_t0 = (expected_baseline_t0 - 0.003) / expected_baseline_t0 * 100.0
    assert abs(float(saved_t0.rstrip("%")) - expected_saved_t0) < 0.05, \
        f"T0 cost-saved%: {saved_t0}"
    assert t0["any_estimated"] is True

    # --- T1 ---
    t1 = buckets["T1"]
    assert t1["n"] == 2, f"T1 n: {t1['n']}"
    assert t1["acceptable_count"] == 2, f"T1 accept count: {t1['acceptable_count']}"
    accept_t1 = _fmt_accept_rate(t1)
    assert accept_t1 == "100.0%", f"T1 accept rate: {accept_t1}"
    # null cost_usd treated as 0 in sum
    assert abs(t1["router_cost"] - 0.010) < 1e-9, f"T1 router_cost: {t1['router_cost']}"
    expected_baseline_t1 = (2000 + 1000) * OPUS_RATE / 1e6
    assert abs(t1["baseline_cost"] - expected_baseline_t1) < 1e-9, \
        f"T1 baseline_cost: {t1['baseline_cost']}"
    assert t1["any_estimated"] is True

    # --- T2 ---
    t2 = buckets["T2"]
    assert t2["n"] == 2, f"T2 n: {t2['n']}"
    assert t2["acceptable_count"] == 1, f"T2 accept count: {t2['acceptable_count']}"
    accept_t2 = _fmt_accept_rate(t2)
    assert accept_t2 == "50.0%", f"T2 accept rate: {accept_t2}"
    assert abs(t2["router_cost"] - 0.150) < 1e-9, f"T2 router_cost: {t2['router_cost']}"
    expected_baseline_t2 = (4000 + 2000) * OPUS_RATE / 1e6
    assert abs(t2["baseline_cost"] - expected_baseline_t2) < 1e-9, \
        f"T2 baseline_cost: {t2['baseline_cost']}"
    # T2 is always-opus so router_cost ≈ baseline_cost (both use opus rate here)
    assert t2["any_estimated"] is False

    # --- TOTAL ---
    total = _total_bucket(buckets)
    assert total["n"] == 6, f"total n: {total['n']}"
    assert total["acceptable_count"] == 4, f"total acceptable_count: {total['acceptable_count']}"
    expected_total_router = 0.003 + 0.010 + 0.150
    assert abs(total["router_cost"] - expected_total_router) < 1e-9, \
        f"total router_cost: {total['router_cost']}"
    expected_total_baseline = expected_baseline_t0 + expected_baseline_t1 + expected_baseline_t2
    assert abs(total["baseline_cost"] - expected_total_baseline) < 1e-9, \
        f"total baseline_cost: {total['baseline_cost']}"
    total_saved_str = _fmt_saved(total)
    expected_total_saved = (expected_total_baseline - expected_total_router) / expected_total_baseline * 100.0
    assert abs(float(total_saved_str.rstrip("%")) - expected_total_saved) < 0.05, \
        f"total cost-saved%: {total_saved_str}"
    assert total["any_estimated"] is True

    # --- baseline=0 edge case → n/a ---
    zero_bucket = _empty_bucket()
    zero_bucket["n"] = 1
    zero_bucket["acceptable_count"] = 1
    assert _fmt_saved(zero_bucket) == "n/a", "zero baseline must yield n/a"

    # --- tier derivation: model fallback ---
    assert _derive_tier({"subagent_type": "",      "model": "claude-haiku-4-5"}) == "T0"
    assert _derive_tier({"subagent_type": "",      "model": "claude-sonnet-4-6"}) == "T1"
    assert _derive_tier({"subagent_type": "",      "model": "claude-opus-4-7"})   == "T2"
    assert _derive_tier({"subagent_type": "scout", "model": ""})                  == "T0"
    assert _derive_tier({"subagent_type": "gearbox:builder", "model": ""})        == "T1"
    assert _derive_tier({"subagent_type": "",      "model": "unknown-model"})     == "(unknown)"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score labeled routing dispatches against a modeled always-opus baseline."
    )
    parser.add_argument(
        "--labels",
        default="bench/training-data.jsonl",
        metavar="PATH",
        help="Labeled training data to read (default: bench/training-data.jsonl)",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run assert-based self-tests and exit.",
    )
    args = parser.parse_args()

    if args.selfcheck:
        selfcheck()

    labels_path = Path(args.labels)
    rows = load_labeled_rows(labels_path)

    if not rows:
        print("No labeled data yet — run `python3 bench/label.py` first.")
        sys.exit(0)

    buckets = aggregate(rows)
    any_estimated = print_scorecard(buckets)

    print()
    print("Baseline: MODELED (always-opus ceiling).")
    print("Assumes per-task token counts are policy-invariant and that opus")
    print("is always acceptable — a rough ceiling, NOT a measured counterfactual.")
    if any_estimated:
        print("* costs are estimate-derived (blended per-model rates) where cost_estimated=true")


if __name__ == "__main__":
    main()
