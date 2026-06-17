#!/usr/bin/env python3
"""Routing dashboard: aggregate gearbox delegation log into per-tier rollups.

Reads ~/.claude/gearbox-log.jsonl (or a path given by --log) and prints a
plain-text table grouped by tier (default) or by project cwd (--by-project).
Read-only: no files are written.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Log loading
# ---------------------------------------------------------------------------

def load_records(log_path: Path) -> list:
    """Read all valid JSON records from the log.  Malformed/blank lines are
    silently skipped."""
    records = []
    if not log_path.exists():
        return records
    with log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _empty_bucket() -> dict:
    return {
        "dispatches": 0,
        "approve": 0,
        "reject": 0,
        "none": 0,
        "escalations": 0,
        "cost_sum": 0.0,
        "cost_count": 0,    # non-null cost records
        "total_tokens": 0,
        "any_estimated": False,
    }


def aggregate(records: list, group_by: str) -> dict:
    """Return a dict of {group_key: bucket} for all records.

    group_by: "tier" or "cwd"
    """
    buckets = defaultdict(_empty_bucket)
    for rec in records:
        key = rec.get(group_by) or "(unknown)"
        b = buckets[key]
        b["dispatches"] += 1

        verdict = rec.get("verdict", "")
        if verdict == "approve":
            b["approve"] += 1
        elif verdict == "reject":
            b["reject"] += 1
        else:
            b["none"] += 1

        if rec.get("escalation"):
            b["escalations"] += 1

        cost = rec.get("cost_usd")
        if cost is not None:
            try:
                b["cost_sum"] += float(cost)
                b["cost_count"] += 1
            except (TypeError, ValueError):
                pass

        tokens = rec.get("total_tokens")
        if tokens is not None:
            try:
                b["total_tokens"] += int(tokens)
            except (TypeError, ValueError):
                pass

        if rec.get("cost_estimated"):
            b["any_estimated"] = True

    return dict(buckets)


# ---------------------------------------------------------------------------
# Reject-rate / escalation-rate
# ---------------------------------------------------------------------------

def reject_rate(bucket: dict) -> str:
    """Return reject-rate as a percentage string, or 'n/a' when no verified
    dispatches exist at this group.

    # ponytail: reject / (approve + reject) is the available proxy for routing
    # miscalibration — a high rate signals too-hard work being sent to this
    # tier.  True escalation-chain rate (re-dispatch correlation) is deferred
    # to G32 transcript mining.
    """
    verified = bucket["approve"] + bucket["reject"]
    if verified == 0:
        return "n/a"
    return f"{100.0 * bucket['reject'] / verified:.1f}%"


def esc_rate(bucket: dict) -> str:
    """Return escalation rate as a percentage string, or 'n/a' when no dispatches."""
    if bucket["dispatches"] == 0:
        return "n/a"
    return f"{100.0 * bucket['escalations'] / bucket['dispatches']:.1f}%"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_cost(val: float) -> str:
    return f"${val:.4f}"


def _fmt_mean(bucket: dict) -> str:
    if bucket["cost_count"] == 0:
        return "n/a"
    return _fmt_cost(bucket["cost_sum"] / bucket["cost_count"])


def _total_bucket(buckets: dict) -> dict:
    total = _empty_bucket()
    for b in buckets.values():
        total["dispatches"] += b["dispatches"]
        total["approve"] += b["approve"]
        total["reject"] += b["reject"]
        total["none"] += b["none"]
        total["escalations"] += b["escalations"]
        total["cost_sum"] += b["cost_sum"]
        total["cost_count"] += b["cost_count"]
        total["total_tokens"] += b["total_tokens"]
        if b["any_estimated"]:
            total["any_estimated"] = True
    return total


def _print_table(buckets: dict, group_label: str) -> bool:
    """Print the aligned table.  Returns True if any row has cost_estimated."""
    rows = []
    for key in sorted(buckets):
        b = buckets[key]
        rows.append((
            key,
            b["dispatches"],
            b["approve"],
            b["reject"],
            b["none"],
            reject_rate(b),
            esc_rate(b),
            _fmt_cost(b["cost_sum"]),
            _fmt_mean(b),
            b["total_tokens"],
        ))

    total = _total_bucket(buckets)
    rows.append((
        "TOTAL",
        total["dispatches"],
        total["approve"],
        total["reject"],
        total["none"],
        reject_rate(total),
        esc_rate(total),
        _fmt_cost(total["cost_sum"]),
        _fmt_mean(total),
        total["total_tokens"],
    ))

    headers = (
        group_label,
        "dispatches",
        "approve",
        "reject",
        "none",
        "reject-rate",
        "esc-rate",
        "cost_usd",
        "mean_cost",
        "total_tokens",
    )

    # Compute column widths.
    cols = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in col) for col in cols]

    sep = "  "
    header_line = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print(header_line)
    print("-" * len(header_line))

    for i, row in enumerate(rows):
        is_total = i == len(rows) - 1
        if is_total:
            print("-" * len(header_line))
        print(sep.join(str(cell).ljust(widths[j]) for j, cell in enumerate(row)))

    any_estimated = any(b["any_estimated"] for b in buckets.values())
    return any_estimated


# ---------------------------------------------------------------------------
# Cost over time  (--over-time)
# ---------------------------------------------------------------------------

def _day_buckets(records: list) -> dict:
    """Return {date_iso: {"dispatches":int, "cost_sum":float, "cost_count":int,
    "any_estimated":bool}} sorted ascending by date.

    Records missing or invalid ts are silently skipped.
    """
    buckets: dict = defaultdict(lambda: {
        "dispatches": 0,
        "cost_sum": 0.0,
        "cost_count": 0,
        "any_estimated": False,
    })
    for rec in records:
        ts = rec.get("ts")
        if ts is None:
            continue
        try:
            day = datetime.fromtimestamp(float(ts)).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        b = buckets[day]
        b["dispatches"] += 1
        cost = rec.get("cost_usd")
        if cost is not None:
            try:
                b["cost_sum"] += float(cost)
                b["cost_count"] += 1
            except (TypeError, ValueError):
                pass
        if rec.get("cost_estimated"):
            b["any_estimated"] = True
    return dict(sorted(buckets.items()))


def print_over_time(records: list) -> None:
    """Print a per-day cost table, sorted ascending by date."""
    buckets = _day_buckets(records)
    if not buckets:
        print("No timestamped records found.")
        return

    rows = []
    for day, b in buckets.items():
        if b["cost_count"] == 0:
            mean_str = "n/a"
        else:
            mean_str = _fmt_cost(b["cost_sum"] / b["cost_count"])
        rows.append((day, b["dispatches"], _fmt_cost(b["cost_sum"]), mean_str))

    headers = ("date", "dispatches", "cost_usd", "mean_cost")
    cols = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in col) for col in cols]

    sep = "  "
    header_line = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print("\n-- cost over time --")
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(sep.join(str(cell).ljust(widths[j]) for j, cell in enumerate(row)))

    if any(b["any_estimated"] for b in buckets.values()):
        print()
        print("* costs are estimate-derived (blended per-model rates), not billed figures")


# ---------------------------------------------------------------------------
# Prior vs actual tier mix  (--prior)
# ---------------------------------------------------------------------------

def print_prior(records: list) -> None:
    """Print a table comparing actual tier mix per task-class to the prior rec."""
    # Import lazily to keep the default path stdlib-only.
    try:
        from recommend import bucket_task_class, recommended_tiers
    except ImportError as exc:
        print(f"Cannot import recommend.py: {exc}")
        return

    # Build actual tier mix per task-class.
    class_tiers: dict = defaultdict(lambda: defaultdict(int))
    for rec in records:
        prompt_head = rec.get("prompt_head") or ""
        task_class = bucket_task_class(prompt_head)
        tier = rec.get("tier") or "(unknown)"
        class_tiers[task_class][tier] += 1

    # Get recommended tier per class from recommend.py (single source of truth).
    rec_tiers = recommended_tiers(records)

    # Determine class order: use CLASS_ORDER from recommend where possible,
    # then append any extra classes seen in records.
    try:
        from recommend import CLASS_ORDER
        ordered_classes = [c for c in CLASS_ORDER if c in class_tiers]
        ordered_classes += [c for c in sorted(class_tiers) if c not in ordered_classes]
    except ImportError:
        ordered_classes = sorted(class_tiers)

    rows = []
    for tc in ordered_classes:
        tier_counts = class_tiers[tc]
        # Format actual mix: "T0×12 T1×3" sorted by tier name.
        mix_parts = [f"{t}×{n}" for t, n in sorted(tier_counts.items())]
        mix_str = " ".join(mix_parts) if mix_parts else "(none)"
        prior_rec = rec_tiers.get(tc) or "—"
        rows.append((tc, mix_str, prior_rec))

    if not rows:
        print("No records to show for prior comparison.")
        return

    headers = ("task-class", "actual tier mix", "prior rec")
    cols = list(zip(headers, *rows))
    widths = [max(len(str(cell)) for cell in col) for col in cols]

    sep = "  "
    header_line = sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    print("\n-- prior vs actual tier mix --")
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print(sep.join(str(cell).ljust(widths[j]) for j, cell in enumerate(row)))


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------

def selfcheck() -> None:
    """Assert-based tests on aggregation and reject-rate logic.  Exits 0 on
    success, non-zero on assertion failure."""

    # --- synthetic records covering all verdict/cost/escalation cases ---
    # ts values: 2024-01-15 and 2024-01-16 in UTC (well-defined epoch offsets)
    # 2024-01-15 00:30:00 UTC → 1705275000
    # 2024-01-16 00:30:00 UTC → 1705361400
    DAY1_TS = 1705275000
    DAY2_TS = 1705361400

    recs = [
        # T1: 1 approve, 1 reject, cost 0.10 + 0.20, tokens 1000+2000; 1 escalation
        {"tier": "T1", "cwd": "/proj/a", "verdict": "approve", "cost_usd": 0.10,
         "cost_estimated": False, "total_tokens": 1000,
         "escalation": True,  "escalated_from": None, "escalated_to": "T2",
         "ts": DAY1_TS, "prompt_head": "implement the login endpoint"},
        {"tier": "T1", "cwd": "/proj/a", "verdict": "reject",  "cost_usd": 0.20,
         "cost_estimated": True,  "total_tokens": 2000,
         "escalation": False, "escalated_from": None, "escalated_to": None,
         "ts": DAY1_TS, "prompt_head": "implement auth"},
        # T2: 2 approve, 0 reject, cost 0.05 (other has null cost), tokens 500
        {"tier": "T2", "cwd": "/proj/b", "verdict": "approve", "cost_usd": 0.05,
         "cost_estimated": False, "total_tokens": 500,
         "escalation": False, "escalated_from": None, "escalated_to": None,
         "ts": DAY2_TS, "prompt_head": "fix the bug"},
        {"tier": "T2", "cwd": "/proj/b", "verdict": "approve", "cost_usd": None,
         "cost_estimated": False, "total_tokens": 300,
         "escalation": False, "escalated_from": None, "escalated_to": None,
         "ts": DAY2_TS, "prompt_head": "read the config"},
        # T0: empty verdict (unverified scout), no cost, tokens 100; 1 escalation
        {"tier": "T0", "cwd": "/proj/a", "verdict": "",         "cost_usd": None,
         "cost_estimated": False, "total_tokens": 100,
         "escalation": True,  "escalated_from": None, "escalated_to": "T1",
         "ts": None, "prompt_head": "summarize the README"},
    ]

    # --- aggregate by tier ---
    by_tier = aggregate(recs, "tier")
    assert set(by_tier.keys()) == {"T0", "T1", "T2"}, f"unexpected keys: {by_tier.keys()}"

    t0 = by_tier["T0"]
    assert t0["dispatches"] == 1
    assert t0["approve"] == 0
    assert t0["reject"] == 0
    assert t0["none"] == 1
    assert t0["escalations"] == 1
    assert t0["cost_sum"] == 0.0
    assert t0["cost_count"] == 0
    assert t0["total_tokens"] == 100
    assert not t0["any_estimated"]

    t1 = by_tier["T1"]
    assert t1["dispatches"] == 2
    assert t1["approve"] == 1
    assert t1["reject"] == 1
    assert t1["none"] == 0
    assert t1["escalations"] == 1
    assert abs(t1["cost_sum"] - 0.30) < 1e-9, f"T1 cost_sum: {t1['cost_sum']}"
    assert t1["cost_count"] == 2
    assert t1["total_tokens"] == 3000
    assert t1["any_estimated"]

    t2 = by_tier["T2"]
    assert t2["dispatches"] == 2
    assert t2["approve"] == 2
    assert t2["reject"] == 0
    assert t2["escalations"] == 0
    assert abs(t2["cost_sum"] - 0.05) < 1e-9, f"T2 cost_sum: {t2['cost_sum']}"
    assert t2["cost_count"] == 1   # null excluded from count
    assert t2["total_tokens"] == 800
    assert not t2["any_estimated"]

    # --- reject-rate ---
    assert reject_rate(t0) == "n/a", "T0 no verified dispatches → n/a"
    assert reject_rate(t1) == "50.0%", f"T1 reject-rate: {reject_rate(t1)}"
    assert reject_rate(t2) == "0.0%",  f"T2 reject-rate: {reject_rate(t2)}"

    # --- esc_rate ---
    assert esc_rate(t0) == "100.0%", f"T0 esc-rate: {esc_rate(t0)}"
    assert esc_rate(t1) == "50.0%",  f"T1 esc-rate: {esc_rate(t1)}"
    assert esc_rate(t2) == "0.0%",   f"T2 esc-rate: {esc_rate(t2)}"
    # zero-dispatch bucket edge case
    empty = _empty_bucket()
    assert esc_rate(empty) == "n/a", "zero dispatches → n/a"
    # zero-escalation bucket
    no_esc = _empty_bucket()
    no_esc["dispatches"] = 3
    assert esc_rate(no_esc) == "0.0%", f"zero escalations: {esc_rate(no_esc)}"

    # --- mean cost ---
    assert _fmt_mean(t0) == "n/a"
    assert _fmt_mean(t1) == "$0.1500", f"T1 mean: {_fmt_mean(t1)}"
    assert _fmt_mean(t2) == "$0.0500", f"T2 mean: {_fmt_mean(t2)}"

    # --- aggregate by cwd ---
    by_cwd = aggregate(recs, "cwd")
    assert set(by_cwd.keys()) == {"/proj/a", "/proj/b"}, f"unexpected cwd keys: {by_cwd.keys()}"
    proj_a = by_cwd["/proj/a"]
    assert proj_a["dispatches"] == 3   # T1 approve + T1 reject + T0 none
    assert proj_a["approve"] == 1
    assert proj_a["reject"] == 1
    assert proj_a["none"] == 1
    assert proj_a["escalations"] == 2  # T1 approve escalated + T0 escalated

    # --- total bucket ---
    total = _total_bucket(by_tier)
    assert total["dispatches"] == 5
    assert total["approve"] == 3
    assert total["reject"] == 1
    assert total["none"] == 1
    assert total["escalations"] == 2
    assert abs(total["cost_sum"] - 0.35) < 1e-9, f"total cost_sum: {total['cost_sum']}"
    assert total["cost_count"] == 3
    assert total["total_tokens"] == 3900
    assert total["any_estimated"]

    # --- day bucketing (_day_buckets) ---
    # DAY1_TS → two records (T1 approve + T1 reject); DAY2_TS → two records (T2 x2).
    # T0 record has ts=None and must be skipped.
    day_b = _day_buckets(recs)
    assert len(day_b) == 2, f"expected 2 days, got: {list(day_b.keys())}"
    days_sorted = sorted(day_b.keys())
    d1, d2 = days_sorted[0], days_sorted[1]
    assert d1 < d2, "days must be sorted ascending"
    assert day_b[d1]["dispatches"] == 2, f"day1 dispatches: {day_b[d1]['dispatches']}"
    assert abs(day_b[d1]["cost_sum"] - 0.30) < 1e-9, f"day1 cost_sum: {day_b[d1]['cost_sum']}"
    assert day_b[d1]["cost_count"] == 2
    assert day_b[d2]["dispatches"] == 2, f"day2 dispatches: {day_b[d2]['dispatches']}"
    assert abs(day_b[d2]["cost_sum"] - 0.05) < 1e-9, f"day2 cost_sum: {day_b[d2]['cost_sum']}"
    assert day_b[d2]["cost_count"] == 1   # one null cost on day2
    # missing-ts record was skipped (only 4 total, not 5)
    assert sum(b["dispatches"] for b in day_b.values()) == 4, \
        "null-ts record must be skipped"

    # --- prior vs actual: bucket_task_class and recommended_tiers imported from recommend ---
    try:
        from recommend import bucket_task_class, recommended_tiers
    except ImportError:
        # recommend.py not on path in some environments — skip these assertions
        print("selfcheck OK (recommend import skipped)")
        sys.exit(0)

    # Records in recs:
    # "implement the login endpoint" → implement/fix; tier T1
    # "implement auth"              → implement/fix; tier T1
    # "fix the bug"                 → implement/fix; tier T2
    # "read the config"             → explore/read;  tier T2
    # "summarize the README"        → explore/read;  tier T0
    assert bucket_task_class("implement the login endpoint") == "implement/fix"
    assert bucket_task_class("read the config") == "explore/read"

    # Build a bigger set so recommended_tiers can produce a winner (n >= K=5).
    many_recs = (
        [{"tier": "T1", "prompt_head": "implement feature", "verdict": "approve",
          "cost_usd": 0.01}] * 4
        + [{"tier": "T1", "prompt_head": "implement feature", "verdict": "reject",
            "cost_usd": 0.01}]
    )
    rt = recommended_tiers(many_recs)
    assert rt.get("implement/fix") == "T1", \
        f"recommended_tiers: expected T1 for implement/fix, got {rt}"

    # With only our 5-record recs fixture, implement/fix has T1×2 T2×1 — all below K=5,
    # so recommended_tiers returns None for every class.
    rt_small = recommended_tiers(recs)
    assert rt_small.get("implement/fix") is None, \
        f"small sample → None expected, got {rt_small}"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Show gearbox routing log aggregated by tier or project."
    )
    parser.add_argument(
        "--log",
        default=str(Path.home() / ".claude" / "gearbox-log.jsonl"),
        metavar="PATH",
        help="Delegation log to read (default: ~/.claude/gearbox-log.jsonl)",
    )
    parser.add_argument(
        "--by-project",
        action="store_true",
        help="Group rows by project cwd instead of tier.",
    )
    parser.add_argument(
        "--over-time",
        action="store_true",
        help="Show a per-day cost breakdown table after the main table.",
    )
    parser.add_argument(
        "--prior",
        action="store_true",
        help="Show actual tier mix vs routing prior recommendation per task-class.",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run assert-based self-tests and exit.",
    )
    args = parser.parse_args()

    if args.selfcheck:
        selfcheck()

    log_path = Path(args.log)
    records = load_records(log_path)

    if not records:
        print(f"No records found in {log_path}")
        sys.exit(0)

    group_by = "cwd" if args.by_project else "tier"
    group_label = "project" if args.by_project else "tier"

    buckets = aggregate(records, group_by)
    any_estimated = _print_table(buckets, group_label)

    if any_estimated:
        print()
        print("* costs are estimate-derived (blended per-model rates), not billed figures")

    if args.over_time:
        print_over_time(records)

    if args.prior:
        print_prior(records)


if __name__ == "__main__":
    main()
