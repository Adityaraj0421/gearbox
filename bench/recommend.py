#!/usr/bin/env python3
"""Routing prior: turn gearbox delegation telemetry into an advisory table.

Reads ~/.claude/gearbox-log.jsonl (or --log PATH), buckets each dispatch into
a task-class by keyword-matching prompt_head, aggregates per (task_class, tier),
and writes a Markdown recommendation table to ~/.claude/gearbox-recommendations.md
(or --out PATH).

Read-only on the log; writes one Markdown artifact.
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum sample count before a recommendation is issued.
# ponytail: K=5 is a low-sample floor — raise to 20+ once the log has
# hundreds of verified rows per cell.  A proper solution uses a Beta-
# distribution credible interval rather than a hard cutoff.
K = 5

# Tier display order within each task-class group.
TIER_ORDER = ["T0", "T1", "T2"]

# Task-class definitions: (class_name, [keywords]).  FIRST MATCH WINS.
# Order matters: specific/narrower classes precede generic ones so that
# "format" and "rename" don't fall into "implement/fix".
#
# ponytail: keyword matching is a ceiling — brittle for paraphrased prompts.
# Upgrade path post-1.0.0: embed prompt_head with a small local model and
# use a nearest-centroid or bandit classifier trained on verified rows.
TASK_CLASSES = [
    ("mechanical-edit", [
        "rename", "typo", "comment", "docstring", "format", "bump version",
        "add log line", "gitignore", "whitespace",
    ]),
    ("explore/read", [
        "read", "summarize", "where is", "how does", "find", "grep", "list",
        "locate", "search", "inspect", "report back",
    ]),
    ("test", [
        "write test", "add test", "unit test", "coverage", "test_",
    ]),
    ("design/debug-hard", [
        "design", "architecture", "debug", "race", "concurrency", "migration",
        "security", "performance", "cross-cutting", "root cause", "refactor",
    ]),
    ("implement/fix", [
        "implement", "fix", "add", "feature", "endpoint", "component", "bug",
        "build", "create",
    ]),
    ("other", []),  # fallback — matches nothing above
]

# Canonical task-class order for table output (matches TASK_CLASSES order).
CLASS_ORDER = [name for name, _ in TASK_CLASSES]


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
# Task-class bucketing
# ---------------------------------------------------------------------------

def bucket_task_class(prompt_head: str) -> str:
    """Return the first matching task-class for a lowercased prompt_head.

    Falls back to 'other' when no keyword matches.
    """
    text = (prompt_head or "").lower()
    for class_name, keywords in TASK_CLASSES:
        if not keywords:
            return class_name  # 'other' fallback
        for kw in keywords:
            if kw in text:
                return class_name
    return "other"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(records: list) -> dict:
    """Return {(task_class, tier): cell} excluding TV rows.

    cell keys:
      n           - row count
      approve     - count of 'approve' verdicts
      reject      - count of 'reject' verdicts
      cost_sum    - sum of cost_usd (non-null)
      cost_count  - count of non-null cost_usd rows
    """
    cells: dict = defaultdict(lambda: {
        "n": 0,
        "approve": 0,
        "reject": 0,
        "cost_sum": 0.0,
        "cost_count": 0,
    })

    for rec in records:
        tier = rec.get("tier") or ""
        if not tier or tier == "TV":
            continue

        prompt_head = rec.get("prompt_head") or ""
        task_class = bucket_task_class(prompt_head)

        key = (task_class, tier)
        cell = cells[key]
        cell["n"] += 1

        verdict = rec.get("verdict")
        if verdict == "approve":
            cell["approve"] += 1
        elif verdict == "reject":
            cell["reject"] += 1

        cost = rec.get("cost_usd")
        if cost is not None:
            try:
                cell["cost_sum"] += float(cost)
                cell["cost_count"] += 1
            except (TypeError, ValueError):
                pass

    return dict(cells)


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

def _approve_pct(cell: dict) -> float | None:
    """Return approve% as a float [0,100], or None when no verdicts exist."""
    verified = cell["approve"] + cell["reject"]
    if verified == 0:
        return None
    return 100.0 * cell["approve"] / verified


def _mean_cost(cell: dict) -> float | None:
    if cell["cost_count"] == 0:
        return None
    return cell["cost_sum"] / cell["cost_count"]


def assign_recs(cells: dict) -> dict:
    """Return {(task_class, tier): rec_string} for all cells.

    Rules:
    - n < K → 'low-n'
    - Within each task_class, the single cell with n>=K AND highest approve_pct
      (tie-break: lower mean_cost) is marked '✓ prefer'.
    - All other cells with n>=K: blank string.
    - Task-classes where no cell has a verdict: no '✓ prefer' issued.
    """
    recs: dict = {}

    # Group keys by task_class.
    by_class: dict = defaultdict(list)
    for task_class, tier in cells:
        by_class[task_class].append(tier)

    for task_class, tiers in by_class.items():
        # Candidates: n>=K and have at least one verdict.
        candidates = []
        for tier in tiers:
            cell = cells[(task_class, tier)]
            if cell["n"] >= K:
                pct = _approve_pct(cell)
                if pct is not None:
                    candidates.append((tier, pct, _mean_cost(cell) or 0.0))

        # Pick winner: highest approve_pct, tie-break lower mean_cost.
        winner_tier = None
        if candidates:
            candidates.sort(key=lambda x: (x[1], -x[2]), reverse=True)
            winner_tier = candidates[0][0]

        for tier in tiers:
            cell = cells[(task_class, tier)]
            if cell["n"] < K:
                recs[(task_class, tier)] = "low-n"
            elif tier == winner_tier:
                recs[(task_class, tier)] = "✓ prefer"
            else:
                recs[(task_class, tier)] = ""

    return recs


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _fmt_approve_pct(cell: dict) -> str:
    pct = _approve_pct(cell)
    if pct is None:
        return "—"
    return f"{pct:.0f}%"


def _fmt_mean_cost(cell: dict) -> str:
    mc = _mean_cost(cell)
    if mc is None:
        return "—"
    return f"{mc:.4f}"


def render_markdown(cells: dict, recs: dict, n_total: int, n_verdict: int) -> str:
    """Return the full Markdown artifact as a string."""
    today = date.today().isoformat()
    lines = [
        "# Gearbox routing prior",
        "",
        f"Generated {today} from {n_total} dispatches ({n_verdict} with a verifier verdict).",
        "",
        "| task-class | tier | n | approve% | mean $ | rec |",
        "|------------|------|---|----------|--------|-----|",
    ]

    for task_class in CLASS_ORDER:
        for tier in TIER_ORDER:
            key = (task_class, tier)
            if key not in cells:
                continue
            cell = cells[key]
            rec = recs.get(key, "")
            lines.append(
                f"| {task_class} | {tier} | {cell['n']} "
                f"| {_fmt_approve_pct(cell)} "
                f"| {_fmt_mean_cost(cell)} "
                f"| {rec} |"
            )

    lines += [
        "",
        "Advisory prior — use as a tie-breaker only. It never overrides the hard floors,"
        " max-dimension routing, or the circuit breaker in the routing policy.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------

def selfcheck() -> None:
    """Assert-based tests.  Does NOT read the real log or write any file.
    Exits 0 on success, non-zero on assertion failure."""

    # --- bucketing ---
    assert bucket_task_class("rename foo to bar") == "mechanical-edit", \
        "rename should bucket to mechanical-edit"
    assert bucket_task_class("summarize the README") == "explore/read", \
        "summarize should bucket to explore/read"
    assert bucket_task_class("write test for the login flow") == "test", \
        "write test should bucket to test"
    assert bucket_task_class("design the new architecture") == "design/debug-hard", \
        "design should bucket to design/debug-hard"
    assert bucket_task_class("implement the login endpoint") == "implement/fix", \
        "implement should bucket to implement/fix"
    assert bucket_task_class("zxqwerty nothing matches here") == "other", \
        "unmatched should fall to other"

    # --- TV exclusion ---
    tv_recs = [
        {"tier": "TV", "prompt_head": "implement something", "verdict": "approve",
         "cost_usd": 0.10},
        {"tier": "T1", "prompt_head": "implement something", "verdict": "approve",
         "cost_usd": 0.05},
    ]
    cells = aggregate(tv_recs)
    assert ("implement/fix", "TV") not in cells, "TV rows must be excluded"
    assert ("implement/fix", "T1") in cells, "T1 row must be included"

    # --- low-n tagging ---
    # Build K-1 rows so the cell is below threshold.
    low_n_recs = [
        {"tier": "T1", "prompt_head": "rename x to y", "verdict": "approve",
         "cost_usd": 0.01}
        for _ in range(K - 1)
    ]
    cells_low = aggregate(low_n_recs)
    recs_low = assign_recs(cells_low)
    assert recs_low.get(("mechanical-edit", "T1")) == "low-n", \
        f"n<K should be tagged low-n, got: {recs_low}"

    # --- approve_pct math: 4 approve + 1 reject → 80% ---
    pct_recs = (
        [{"tier": "T1", "prompt_head": "fix the bug", "verdict": "approve",
          "cost_usd": 0.01}] * 4
        + [{"tier": "T1", "prompt_head": "fix the bug", "verdict": "reject",
            "cost_usd": 0.01}]
    )
    cells_pct = aggregate(pct_recs)
    cell = cells_pct[("implement/fix", "T1")]
    assert cell["approve"] == 4
    assert cell["reject"] == 1
    pct = _approve_pct(cell)
    assert abs(pct - 80.0) < 1e-9, f"approve_pct: expected 80.0, got {pct}"

    # --- ✓ prefer goes to higher-approve tier within a class ---
    # T1: 4/5 → 80%; T2: 3/5 → 60% — T1 should win.
    prefer_recs = (
        [{"tier": "T1", "prompt_head": "implement feature", "verdict": "approve",
          "cost_usd": 0.02}] * 4
        + [{"tier": "T1", "prompt_head": "implement feature", "verdict": "reject",
            "cost_usd": 0.02}]
        + [{"tier": "T2", "prompt_head": "implement feature", "verdict": "approve",
            "cost_usd": 0.10}] * 3
        + [{"tier": "T2", "prompt_head": "implement feature", "verdict": "reject",
            "cost_usd": 0.10}] * 2
    )
    cells_prefer = aggregate(prefer_recs)
    recs_prefer = assign_recs(cells_prefer)
    assert recs_prefer.get(("implement/fix", "T1")) == "✓ prefer", \
        f"T1 (80%) should beat T2 (60%): {recs_prefer}"
    assert recs_prefer.get(("implement/fix", "T2")) == "", \
        f"T2 should have blank rec: {recs_prefer}"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a routing prior table from gearbox delegation telemetry."
    )
    parser.add_argument(
        "--log",
        default=os.path.expanduser("~/.claude/gearbox-log.jsonl"),
        metavar="PATH",
        help="Delegation log to read (default: ~/.claude/gearbox-log.jsonl)",
    )
    parser.add_argument(
        "--out",
        default=os.path.expanduser("~/.claude/gearbox-recommendations.md"),
        metavar="PATH",
        help="Output Markdown path (default: ~/.claude/gearbox-recommendations.md)",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run assert-based self-tests and exit (no files read or written).",
    )
    args = parser.parse_args()

    if args.selfcheck:
        selfcheck()

    log_path = Path(args.log)
    records = load_records(log_path)

    if not records:
        print(f"No records found in {log_path}")
        sys.exit(0)

    # Counts before TV exclusion (for the header).
    n_total_raw = len(records)

    cells = aggregate(records)

    # n_total: rows that were aggregated (non-TV rows with a tier field).
    n_total = sum(c["n"] for c in cells.values())
    # n_verdict: rows with a non-null verdict across all aggregated cells.
    n_verdict = sum(c["approve"] + c["reject"] for c in cells.values())

    recs = assign_recs(cells)

    md = render_markdown(cells, recs, n_total, n_verdict)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    print(f"Written {out_path}  ({n_total} dispatches aggregated)")


if __name__ == "__main__":
    main()
