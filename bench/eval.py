#!/usr/bin/env python3
"""Routing eval: score labeled dispatches against three modeled baselines.

Reads bench/training-data.jsonl (or --labels PATH) and prints a policy-
comparison scorecard: router (actual) cost vs always-sonnet, always-opus, and
escalate-on-fail modeled baselines.  Read-only: no files are written.

Baselines are MODELED (token count × per-tier blended rate) — no second run
needed.  Only T0/T1/T2 task rows are scored; TV (verifier) and (unknown) rows
are excluded from all baseline and router totals.
"""
import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical blended USD-per-million-tokens rates, date-pinned 2026-06.
# Mirrors _BLENDED_RATES in hooks/scripts/log-routing.py — re-pin both
# together when Anthropic pricing changes.
_BLENDED_RATES: dict = {
    "haiku":  1.5,
    "sonnet": 5.0,
    "opus":   8.0,
}

# Ordered routing tiers and their blended rates.  Order matters for
# escalate-on-fail: a T1 row pays T0 + T1; a T2 row pays T0 + T1 + T2.
TASK_TIERS = ["T0", "T1", "T2"]
_TIER_RATES: dict = {
    "T0": _BLENDED_RATES["haiku"],
    "T1": _BLENDED_RATES["sonnet"],
    "T2": _BLENDED_RATES["opus"],
}


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
# Policy-total computation
# ---------------------------------------------------------------------------

def compute_policy_totals(rows: list) -> dict:
    """Return per-policy cost totals and router acceptability counts.

    Only T0/T1/T2 task rows are included.  TV (verifier) and (unknown) rows
    are excluded from all totals.

    Returns a dict with keys:
      router            float  — sum of row["cost_usd"] (exact, null→0)
      always_sonnet     float  — modeled: each task N × sonnet_rate / 1e6
      always_opus       float  — modeled: each task N × opus_rate / 1e6
      escalate_on_fail  float  — modeled: each task pays T0..K blended rates
      acceptable_count  int    — task rows where acceptable is True
      task_n            int    — total task rows scored
      any_estimated     bool   — True if any task row has cost_estimated=true
    """
    router_total = 0.0
    always_sonnet_total = 0.0
    always_opus_total = 0.0
    escalate_total = 0.0
    acceptable_count = 0
    task_n = 0
    any_estimated = False

    sonnet_rate = _TIER_RATES["T1"]
    opus_rate   = _TIER_RATES["T2"]

    for row in rows:
        tier = _derive_tier(row)
        if tier not in TASK_TIERS:
            # TV (verifier) and (unknown) — not routing decisions, skip
            continue

        task_n += 1

        if row.get("acceptable") is True:
            acceptable_count += 1

        cost = row.get("cost_usd")
        try:
            router_total += float(cost)
        except (TypeError, ValueError):
            pass  # treat null/missing as 0

        if row.get("cost_estimated"):
            any_estimated = True

        tokens = row.get("total_tokens")
        try:
            n = int(tokens)
        except (TypeError, ValueError):
            # ponytail: no token count → skip this row's modeled contributions;
            # router_total already accumulated cost_usd above.
            continue

        # always-sonnet: all tasks dispatched to sonnet
        always_sonnet_total += n * sonnet_rate / 1e6

        # always-opus: all tasks dispatched to opus
        always_opus_total += n * opus_rate / 1e6

        # escalate-on-fail: pays for each tier from T0 up through the router's
        # chosen tier (ponytail: models a policy that tries cheapest first and
        # escalates one tier on failure — the router's value prop is skipping
        # the wasted cheaper attempts).
        tier_idx = TASK_TIERS.index(tier)
        for t in TASK_TIERS[: tier_idx + 1]:
            escalate_total += n * _TIER_RATES[t] / 1e6

    return {
        "router":           router_total,
        "always_sonnet":    always_sonnet_total,
        "always_opus":      always_opus_total,
        "escalate_on_fail": escalate_total,
        "acceptable_count": acceptable_count,
        "task_n":           task_n,
        "any_estimated":    any_estimated,
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_cost(val: float) -> str:
    return f"${val:.4f}"


def _fmt_vs_router(baseline: float, router: float) -> str:
    if baseline == 0.0:
        return "n/a"
    saved = (baseline - router) / baseline * 100.0
    return f"router saves {saved:.1f}%"


def _fmt_accept_rate(acceptable_count: int, task_n: int) -> str:
    if task_n == 0:
        return "n/a"
    return f"{100.0 * acceptable_count / task_n:.1f}%"


# ---------------------------------------------------------------------------
# Scorecard printer
# ---------------------------------------------------------------------------

def print_policy_comparison(totals: dict) -> None:
    """Print the policy-comparison scorecard and acceptability summary."""

    router = totals["router"]

    policies = [
        ("router (actual)",  router),
        ("always-sonnet",    totals["always_sonnet"]),
        ("always-opus",      totals["always_opus"]),
        ("escalate-on-fail", totals["escalate_on_fail"]),
    ]

    rows = []
    for name, cost in policies:
        if name == "router (actual)":
            vs = "—"
        else:
            vs = _fmt_vs_router(cost, router)
        rows.append((name, _fmt_cost(cost), vs))

    headers = ("policy", "total-cost", "vs router")
    cols = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in col) for col in cols]
    sep = "  "

    print("Policy comparison (baselines MODELED; router cost is exact per-component):")
    header_line = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(sep.join(str(cell).ljust(widths[j]) for j, cell in enumerate(row)))

    print()
    accept_str = _fmt_accept_rate(totals["acceptable_count"], totals["task_n"])
    print(
        f"Router acceptability: {accept_str}"
        f" ({totals['acceptable_count']}/{totals['task_n']} task dispatches)."
    )
    print(
        "Baseline acceptability is ASSUMED >= router (modeled, not measured); the"
    )
    print(
        "measured counterfactual needs forced-tier headless runs."
    )

    if totals["any_estimated"]:
        print(
            "* some router costs are estimate-derived (blended per-model rates)"
            " where cost_estimated=true"
        )


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------

def selfcheck() -> None:
    """Assert-based tests on aggregation logic.  Exits 0 on success, non-zero
    on assertion failure."""

    # Synthetic labeled rows spanning T0/T1/T2, plus one TV and one (unknown)
    # row to prove they are excluded from all totals.
    #
    # Rates (USD/M tokens): T0=haiku=1.5, T1=sonnet=5.0, T2=opus=8.0
    #
    # Task rows only (TV and (unknown) excluded):
    #   R0a: T0 (grunt),    tokens=500,  cost_usd=0.001, acceptable=True,  cost_estimated=True
    #   R0b: T0 (scout),    tokens=1000, cost_usd=0.002, acceptable=False, cost_estimated=False
    #   R1a: T1 (builder),  tokens=2000, cost_usd=0.010, acceptable=True,  cost_estimated=True
    #   R1b: T1 (builder),  tokens=1000, cost_usd=None,  acceptable=True,  cost_estimated=False
    #   R2a: T2 (architect),tokens=4000, cost_usd=0.100, acceptable=True,  cost_estimated=False
    #   R2b: T2 (architect),tokens=2000, cost_usd=0.050, acceptable=False, cost_estimated=False
    #
    # Expected totals (hand-computed):
    #   router           = 0.001 + 0.002 + 0.010 + 0 + 0.100 + 0.050 = 0.163
    #   always_sonnet    = (500+1000+2000+1000+4000+2000) * 5.0/1e6
    #                    = 10500 * 5.0/1e6 = 0.0525
    #   always_opus      = 10500 * 8.0/1e6 = 0.084
    #   escalate_on_fail:
    #     R0a (T0): 500  * 1.5/1e6                         = 0.000750
    #     R0b (T0): 1000 * 1.5/1e6                         = 0.001500
    #     R1a (T1): 2000 * (1.5+5.0)/1e6                   = 2000*6.5/1e6 = 0.013000
    #     R1b (T1): 1000 * (1.5+5.0)/1e6                   = 1000*6.5/1e6 = 0.006500
    #     R2a (T2): 4000 * (1.5+5.0+8.0)/1e6               = 4000*14.5/1e6= 0.058000
    #     R2b (T2): 2000 * (1.5+5.0+8.0)/1e6               = 2000*14.5/1e6= 0.029000
    #     total escalate = 0.000750+0.001500+0.013000+0.006500+0.058000+0.029000 = 0.108750
    #
    #   task_n = 6, acceptable_count = 4

    rows = [
        # T0 task rows
        {"subagent_type": "grunt",     "model": "haiku",  "acceptable": True,
         "cost_usd": 0.001, "total_tokens": 500,  "cost_estimated": True},
        {"subagent_type": "scout",     "model": "haiku",  "acceptable": False,
         "cost_usd": 0.002, "total_tokens": 1000, "cost_estimated": False},
        # T1 task rows
        {"subagent_type": "builder",   "model": "sonnet", "acceptable": True,
         "cost_usd": 0.010, "total_tokens": 2000, "cost_estimated": True},
        {"subagent_type": "builder",   "model": "sonnet", "acceptable": True,
         "cost_usd": None,  "total_tokens": 1000, "cost_estimated": False},
        # T2 task rows
        {"subagent_type": "architect", "model": "opus",   "acceptable": True,
         "cost_usd": 0.100, "total_tokens": 4000, "cost_estimated": False},
        {"subagent_type": "architect", "model": "opus",   "acceptable": False,
         "cost_usd": 0.050, "total_tokens": 2000, "cost_estimated": False},
        # TV row — must be excluded from all totals
        {"subagent_type": "verifier",  "model": "haiku",  "acceptable": True,
         "cost_usd": 9.999, "total_tokens": 9999, "cost_estimated": False},
        # (unknown) row — must be excluded from all totals
        {"subagent_type": "",          "model": "unknown-model", "acceptable": True,
         "cost_usd": 9.999, "total_tokens": 9999, "cost_estimated": False},
    ]

    totals = compute_policy_totals(rows)

    # --- task row count and acceptability ---
    assert totals["task_n"] == 6, f"task_n: {totals['task_n']}"
    assert totals["acceptable_count"] == 4, \
        f"acceptable_count: {totals['acceptable_count']}"

    # --- router total: exact cost_usd, null treated as 0 ---
    expected_router = 0.001 + 0.002 + 0.010 + 0.0 + 0.100 + 0.050  # = 0.163
    assert abs(totals["router"] - expected_router) < 1e-9, \
        f"router: {totals['router']} vs {expected_router}"

    # --- always-sonnet: excludes TV and (unknown) ---
    expected_always_sonnet = (500 + 1000 + 2000 + 1000 + 4000 + 2000) * 5.0 / 1e6  # 0.0525
    assert abs(totals["always_sonnet"] - expected_always_sonnet) < 1e-9, \
        f"always_sonnet: {totals['always_sonnet']} vs {expected_always_sonnet}"

    # --- always-opus: excludes TV and (unknown) ---
    expected_always_opus = (500 + 1000 + 2000 + 1000 + 4000 + 2000) * 8.0 / 1e6  # 0.084
    assert abs(totals["always_opus"] - expected_always_opus) < 1e-9, \
        f"always_opus: {totals['always_opus']} vs {expected_always_opus}"

    # --- escalate-on-fail: T2 row = (haiku+sonnet+opus) × N / 1e6 ---
    escalate_r2a = 4000 * (1.5 + 5.0 + 8.0) / 1e6  # 0.058
    escalate_r2b = 2000 * (1.5 + 5.0 + 8.0) / 1e6  # 0.029
    escalate_r0a = 500  * 1.5 / 1e6                 # 0.000750
    escalate_r0b = 1000 * 1.5 / 1e6                 # 0.001500
    escalate_r1a = 2000 * (1.5 + 5.0) / 1e6         # 0.013
    escalate_r1b = 1000 * (1.5 + 5.0) / 1e6         # 0.0065
    expected_escalate = (
        escalate_r0a + escalate_r0b +
        escalate_r1a + escalate_r1b +
        escalate_r2a + escalate_r2b
    )  # 0.108750
    assert abs(totals["escalate_on_fail"] - expected_escalate) < 1e-9, \
        f"escalate_on_fail: {totals['escalate_on_fail']} vs {expected_escalate}"

    # --- any_estimated reflects only task rows ---
    assert totals["any_estimated"] is True, "any_estimated should be True (T0 rows have it)"

    # --- baseline=0 edge case → _fmt_vs_router returns "n/a" ---
    assert _fmt_vs_router(0.0, 0.05) == "n/a", "zero baseline must yield n/a"

    # --- _fmt_vs_router calculation ---
    vs = _fmt_vs_router(0.100, 0.025)
    assert vs == "router saves 75.0%", f"_fmt_vs_router: {vs}"

    # --- acceptability formatting ---
    assert _fmt_accept_rate(4, 6) == "66.7%", f"accept rate: {_fmt_accept_rate(4, 6)}"
    assert _fmt_accept_rate(0, 0) == "n/a"

    # --- tier derivation: model fallback ---
    assert _derive_tier({"subagent_type": "",      "model": "claude-haiku-4-5"}) == "T0"
    assert _derive_tier({"subagent_type": "",      "model": "claude-sonnet-4-6"}) == "T1"
    assert _derive_tier({"subagent_type": "",      "model": "claude-opus-4-7"})   == "T2"
    assert _derive_tier({"subagent_type": "scout", "model": ""})                  == "T0"
    assert _derive_tier({"subagent_type": "gearbox:builder", "model": ""})        == "T1"
    assert _derive_tier({"subagent_type": "",      "model": "unknown-model"})     == "(unknown)"
    assert _derive_tier({"subagent_type": "verifier", "model": ""})               == "TV"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score labeled routing dispatches against three modeled baselines."
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

    totals = compute_policy_totals(rows)
    print_policy_comparison(totals)

    print()
    print("Baselines are MODELED (token count × per-tier blended rate, 2026-06 rates).")
    print("  always-sonnet:    all tasks dispatched to sonnet regardless of complexity.")
    print("  always-opus:      all tasks dispatched to opus (quality ceiling).")
    print("  escalate-on-fail: starts at T0, escalates one tier on failure until")
    print("                    reaching the router's chosen tier — pays for wasted")
    print("                    cheaper attempts.  Token counts assumed policy-invariant.")
    print("These are rough estimates, NOT measured counterfactuals.  A measured baseline")
    print("would require re-running every task under each policy — out of scope.")


if __name__ == "__main__":
    main()
