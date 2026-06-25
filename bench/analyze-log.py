#!/usr/bin/env python3
"""Analyze Gearbox routing telemetry (.claude/gearbox-log.jsonl).

Gearbox's PostToolUse hook appends one JSON line per Task/Agent delegation to
`.claude/gearbox-log.jsonl` in the project being worked on. This script
aggregates one or more of those logs and reports how work was routed:

  1. total delegations + timestamp range
  2. distribution by model tier (haiku / sonnet / opus) -- the headline:
     what fraction of work ran on the cheap tier
  3. distribution by agent (raw subagent_type, and mapped to Gearbox role)
  4. verifier coverage: verifier runs vs T1/T2 work
  5. whether any outcome fields (escalation / verdict / fallback) are present

It is written to survive the schema drift that already exists in real logs:
  * old lines predate the `tool_name` field -- never required here
  * `model` may be the literal "(not passed)" when no model arg was logged
  * `subagent_type` may be a built-in proxy (`Explore`, `general-purpose`)
    used as a fallback instead of a named gearbox:* agent

A second, independent recount runs at the end and asserts its totals match the
primary pass. That self-check is intentional: telemetry you cannot trust is
worse than none, so the analyzer proves its own arithmetic before you quote it.

Usage:
    python3 bench/analyze-log.py                  # glob ~ for every log
    python3 bench/analyze-log.py path/to/log.jsonl [more.jsonl ...]
"""
import sys
import json
import glob
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

KNOWN_FIELDS = {"ts", "session_id", "tool_name", "subagent_type", "model",
                "prompt_head", "cwd"}
# Fields that would carry an escalation/outcome signal if Gearbox logged one.
SIGNAL_FIELDS = {"escalation", "escalated", "verdict", "outcome", "result",
                 "fallback", "tier", "from_tier", "to_tier", "verify",
                 "success", "reject", "approve", "retry", "attempt"}

MODEL_TIER = {"haiku": "T0 (cheap)", "sonnet": "T1 (mid)", "opus": "T2 (expensive)"}


def norm(s):
    return (s or "").strip()


def role_of(subagent_type):
    """Map a raw subagent_type to a Gearbox role bucket.

    Named agents install namespaced (gearbox:scout) but may also appear bare
    (scout) depending on how they were invoked; both fold to one role. Built-in
    proxies used as rule-8 fallbacks are labelled so they don't masquerade as
    named tier agents.
    """
    s = norm(subagent_type).lower()
    base = s.split(":")[-1]                       # gearbox:scout -> scout
    for role in ("scout", "grunt", "builder", "architect", "verifier"):
        if role in base:
            return role
    if base == "explore":                         # fallback proxy for scout (T0)
        return "explore (fallback)"
    if base in ("general-purpose", "plan"):
        return base
    return base or "(empty)"


def resolve_paths(args):
    """Explicit paths win; otherwise glob ~ for logs, excluding cache dirs."""
    if args:
        return args
    pattern = os.path.expanduser("~/**/.claude/gearbox-log.jsonl")
    return [p for p in glob.glob(pattern, recursive=True) if "/cache/" not in p]


def load(paths):
    """Primary loader: returns (rows, malformed_line_count)."""
    rows, bad = [], 0
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        r["_file"] = p
                        rows.append(r)
                    except json.JSONDecodeError:
                        bad += 1
        except OSError as e:
            print(f"  ! could not read {p}: {e}", file=sys.stderr)
    return rows, bad


def independent_recount(paths):
    """Second, deliberately separate pass over the same files.

    Re-reads from disk with its own minimal parser and tallies model values, so
    a bug in the primary loader/normaliser cannot hide. Returns (total, models).
    """
    total = 0
    models = Counter()
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        m = json.loads(line).get("model")
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    models[(m or "").strip() or "(empty)"] += 1
        except OSError:
            pass
    return total, models


def pct(n, d):
    return f"{(100 * n / d):.1f}%" if d else "n/a"


def bar(n, d, width=24):
    filled = round(width * n / d) if d else 0
    return "█" * filled + "·" * (width - filled)


def table(title, counter, total, key_label):
    print(f"\n{title}")
    print(f"  {key_label:<22} {'count':>6} {'pct':>7}  share")
    print("  " + "-" * 58)
    for k, n in counter.most_common():
        print(f"  {str(k):<22} {n:>6} {pct(n, total):>7}  {bar(n, total)}")
    print(f"  {'TOTAL':<22} {total:>6} {'100.0%':>7}")


def main():
    paths = resolve_paths(sys.argv[1:])
    if not paths:
        print("No gearbox-log.jsonl files found "
              "(looked for ~/**/.claude/gearbox-log.jsonl).")
        return 0

    rows, bad = load(paths)
    total = len(rows)
    print("=" * 60)
    print(f"GEARBOX ROUTING LOG ANALYSIS — {len(paths)} file(s), "
          f"{total} delegations")
    if bad:
        print(f"(skipped {bad} malformed line(s))")
    print("=" * 60)

    if total == 0:
        print("\nNo delegations recorded yet.")
        return 0

    # [1] date range
    ts = [r["ts"] for r in rows if isinstance(r.get("ts"), (int, float))]
    if ts:
        lo = datetime.fromtimestamp(min(ts), tz=timezone.utc)
        hi = datetime.fromtimestamp(max(ts), tz=timezone.utc)
        span_days = (max(ts) - min(ts)) / 86400
        print("\n[1] DATE RANGE (UTC)")
        print(f"  first : {lo:%Y-%m-%d %H:%M}")
        print(f"  last  : {hi:%Y-%m-%d %H:%M}")
        print(f"  span  : {span_days:.1f} days   sessions: "
              f"{len({r.get('session_id') for r in rows})}")

    # [2] model tier -- the headline
    models = Counter(norm(r.get("model")) or "(empty)" for r in rows)
    print("\n[2] MODEL / TIER DISTRIBUTION  ← headline: how much ran cheap")
    print(f"  {'model':<14} {'tier':<16} {'count':>6} {'pct':>7}  share")
    print("  " + "-" * 64)
    for m, n in models.most_common():
        tier = MODEL_TIER.get(m, "—" if m == "(not passed)" else "?")
        print(f"  {m:<14} {tier:<16} {n:>6} {pct(n, total):>7}  {bar(n, total)}")
    haiku = models.get("haiku", 0)
    explicit = sum(v for k, v in models.items() if k != "(not passed)")
    print(f"\n  cheap (haiku) / all delegations      = "
          f"{haiku}/{total} = {pct(haiku, total)}")
    print(f"  cheap (haiku) / explicitly-routed    = "
          f"{haiku}/{explicit} = {pct(haiku, explicit)}")
    print(f"  not-passed (tier unknown / inherited)= "
          f"{models.get('(not passed)', 0)}")

    # [3] agent distribution
    raw = Counter(norm(r.get("subagent_type")) or "(empty)" for r in rows)
    table("[3a] AGENT DISTRIBUTION (raw subagent_type)", raw, total,
          "subagent_type")
    roles = Counter(role_of(r.get("subagent_type")) for r in rows)
    table("[3b] AGENT DISTRIBUTION (mapped to Gearbox role)", roles, total,
          "role")

    # [4] verifier coverage
    verifier = sum(1 for r in rows
                   if role_of(r.get("subagent_type")) == "verifier")
    t1t2 = sum(1 for r in rows if norm(r.get("model")) in ("sonnet", "opus"))
    print("\n[4] VERIFIER COVERAGE")
    print(f"  verifier runs               : {verifier}")
    print(f"  T1/T2 work (sonnet+opus)    : {t1t2}")
    print(f"  coverage (verifier / T1+T2) : {pct(verifier, t1t2)}")
    print("  NOTE: lower bound — file-modifying T1/T2 *should* be verified;")
    print("  read-only/escalated-without-edits correctly skip the verifier,")
    print("  and the log has no 'files modified' flag to distinguish them.")

    # [5] escalation / outcome fields
    all_keys = set()
    for r in rows:
        all_keys.update(k for k in r.keys() if not k.startswith("_"))
    extra = all_keys - KNOWN_FIELDS
    found_signal = all_keys & SIGNAL_FIELDS
    print("\n[5] ESCALATION / OUTCOME FIELDS")
    if found_signal:
        print(f"  present: {sorted(found_signal)}")
    else:
        print("  NONE. The log records the routing DECISION (which agent/model)")
        print("  but no OUTCOME: no escalation events, no verifier verdict, no")
        print("  success/fail, no fallback flag, no tier transitions.")
        print("  -> Gap for 0.2.x: without outcomes the log cannot tell a good")
        print("     route from a bad one, so it cannot train a learned router.")
    if extra:
        print(f"  (non-standard fields seen: {sorted(extra)})")

    # [+] per-project breakdown
    byproj = defaultdict(Counter)
    for r in rows:
        proj = os.path.basename(norm(r.get("cwd")).rstrip("/")) or "?"
        byproj[proj][norm(r.get("model")) or "(empty)"] += 1
    print("\n[+] PER-PROJECT BREAKDOWN (model counts)")
    print(f"  {'project':<24} {'total':>5} {'haiku':>6} {'sonnet':>7} "
          f"{'opus':>5} {'n/p':>5}")
    print("  " + "-" * 60)
    for proj, c in sorted(byproj.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values())
        print(f"  {proj[:24]:<24} {tot:>5} {c.get('haiku', 0):>6} "
              f"{c.get('sonnet', 0):>7} {c.get('opus', 0):>5} "
              f"{c.get('(not passed)', 0):>5}")

    # self-check: independent recount must agree, or the report is not trustworthy
    rc_total, rc_models = independent_recount(paths)
    ok = (rc_total == total) and all(
        rc_models.get(k, 0) == v for k, v in models.items())
    print("\n[self-check] independent recount", end=" ")
    if ok:
        print(f"OK — {rc_total} lines, model tallies match primary pass.")
        return 0
    print("FAILED — primary vs recount disagree:")
    print(f"  total: primary={total} recount={rc_total}")
    for k in sorted(set(models) | set(rc_models)):
        if models.get(k, 0) != rc_models.get(k, 0):
            print(f"  {k}: primary={models.get(k, 0)} recount={rc_models.get(k, 0)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
