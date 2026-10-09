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
  6. outcomes (0.2.0): a HARD fallback rate (named gearbox: tier vs generic
     proxy, from the fallback/is_named_tier fields), the verifier approve/reject
     ratio, and escalation frequency, read from {"event":"verdict"} and
     {"event":"escalation"} records
  7. cost signal (tokens per tier) and, from 0.2.4 {"event":"session_start"}
     records, sessions that started with routing active but never delegated

Worktrees carry their own copy of a project's .claude/ directory, so the same
records can appear in several files. Records that are byte-for-byte identical
(after key sorting) across DIFFERENT files are counted once; the number dropped
is printed in the header. Identical records inside one file are kept.

Two record shapes coexist in a log: delegation records (one per Task/Agent
call) and outcome-event records ({"event": "verdict"|"escalation"}). Delegation
stats are computed over the former only; events are tallied separately.

It is written to survive the schema drift that already exists in real logs:
  * old lines predate the `tool_name` field -- never required here
  * `model` may be the literal "(not passed)" when no model arg was logged
  * `subagent_type` may be a built-in proxy (`Explore`, `general-purpose`)
    used as a fallback instead of a named gearbox:* agent

A second, independent recount runs at the end and asserts its totals match the
primary pass. That self-check is intentional: telemetry you cannot trust is
worse than none, so the analyzer proves its own arithmetic before you quote it.

Usage:
    python3 bench/analyze-log.py                  # walk ~ for every log
    python3 bench/analyze-log.py path/to/log.jsonl [more.jsonl ...]
    python3 bench/analyze-log.py --selftest       # assert join-coverage + token math
"""
import sys
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

KNOWN_FIELDS = {"ts", "session_id", "tool_name", "subagent_type", "model",
                "prompt_head", "cwd", "is_named_tier", "fallback",
                "delegation_id", "cost", "cost_source"}
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


# Directory names never worth descending into when auto-discovering logs: no
# real project keeps its .claude log inside these, and Library in particular is
# where the mirror problem lives.
_PRUNE_DIRS = {"Library", "node_modules", ".git", "cache", ".Trash"}


def resolve_paths(args):
    """Explicit paths win; otherwise discover logs under ~.

    Two filters keep auto-discovery honest. (1) De-duplicate by realpath: macOS
    symlinks ~/Downloads into every app sandbox under ~/Library/Containers, so a
    naive ~/** glob ingests hundreds of mirror copies of the same file and
    inflates every total. (2) Exclude Library/Containers and Library/Group
    Containers outright — nothing under them is a real project.

    Discovery walks the tree with followlinks=False and prunes _PRUNE_DIRS
    rather than globbing ~/**: the glob follows the sandbox symlinks (both the
    count explosion and a multi-minute traversal), whereas an unfollowed,
    pruned walk finds the same real logs in a fraction of a second.
    """
    if args:
        return args
    home = os.path.expanduser("~")
    seen, out = set(), []
    for root, dirs, files in os.walk(home):          # followlinks=False (default)
        dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
        if "gearbox-log.jsonl" not in files or os.path.basename(root) != ".claude":
            continue
        p = os.path.join(root, "gearbox-log.jsonl")
        if ("/cache/" in p or "Library/Containers" in p
                or "Library/Group Containers" in p):
            continue
        rp = os.path.realpath(p)
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


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


def _canon(r):
    """Canonical form of a record for duplicate detection (ignores _file)."""
    return json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                      sort_keys=True, ensure_ascii=False)


def dedupe_across_files(rows):
    """Drop records already seen, identically, in a DIFFERENT file.

    A worktree under .claude/worktrees/ keeps its own copy of the project log,
    so one delegation can be logged two or three times. Realpath de-duplication
    cannot catch this because the copies are separate files. Exact duplicates
    inside a single file are kept: those are not copies, and the hooks never
    write one record twice. Returns (kept_rows, n_dropped).
    """
    first_file, kept, dropped = {}, [], 0
    for r in rows:
        key = _canon(r)
        owner = first_file.setdefault(key, r.get("_file"))
        if owner != r.get("_file"):
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


# verdict / escalation (0.2.0+), session_start (0.2.4). Any record carrying an
# "event" key is an event, so a future kind can never be miscounted as a
# delegation.
EVENT_KINDS = ("verdict", "escalation", "session_start")
TIER_RANK = {"T0": 0, "T1": 1, "T2": 2}


def is_event(r):
    return bool(r.get("event"))


def independent_recount(paths):
    """Second, deliberately separate pass over the same files.

    Re-reads from disk with its own minimal parser, partitions event records
    from delegations the same way the primary pass does, and tallies delegation
    model values, so a bug in the primary loader/partitioner cannot hide.
    Returns (n_delegations, models, n_events).
    """
    delegs = 0
    events = 0
    models = Counter()
    owner = {}                       # canonical record -> first file it came from
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(r, dict):
                        continue
                    key = json.dumps(r, sort_keys=True, ensure_ascii=False)
                    if owner.setdefault(key, p) != p:
                        continue     # cross-file copy (worktree); count once
                    if r.get("event"):
                        events += 1
                        continue
                    delegs += 1
                    models[(r.get("model") or "").strip() or "(empty)"] += 1
        except OSError:
            pass
    return delegs, models, events


def pct(n, d):
    return f"{(100 * n / d):.1f}%" if d else "n/a"


def total_tokens(cost):
    """Total tokens for one delegation's `cost` payload.

    THERE IS NO `total_tokens` KEY in Claude Code's `tool_response.usage`. A
    direct `cost["total_tokens"]` lookup returns nothing and silently reads as
    zero, which is how a log full of real token data can report a cost of 0.
    The total is the sum of four separate counters:

        input_tokens + output_tokens
        + cache_creation_input_tokens + cache_read_input_tokens

    Cache reads are counted: they are cheaper per token, not free, and omitting
    them understates a cached delegation by an order of magnitude (a real
    sample: 2 input + 1517 output vs 96964 cache_read). Any subkey may be
    absent on a different Claude Code version, so each is defaulted to 0 and
    non-numeric values are skipped rather than raising.
    """
    if not isinstance(cost, dict):
        return 0
    out = 0
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = cost.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        out += v
    return int(out)


def join_coverage(verdict_rows):
    """(joined, joinable) over 0.2.1-schema verdicts only.

    Only the 0.2.1 verdict logger writes a `delegation_id` key at all (null when
    the heuristic found no match). Verdicts predating it have no key and are
    structurally unjoinable, so counting them in the denominator invents a
    failure rate for a feature that never ran on them. Presence of the KEY, not
    its truthiness, is what makes a verdict eligible.
    """
    joinable = [r for r in verdict_rows if "delegation_id" in r]
    return sum(1 for r in joinable if r.get("delegation_id")), len(joinable)


def _selftest():
    """Guard the join-coverage denominator. A silently-wrong instrument is the
    exact failure this function exists to prevent, so it gets the one check."""
    pre = [{"event": "verdict", "verdict": "approve"}]                    # no key
    null = [{"event": "verdict", "delegation_id": None}]                  # unmatched
    hit = [{"event": "verdict", "delegation_id": "abc"}]                  # joined
    assert join_coverage([]) == (0, 0)
    assert join_coverage(pre * 15) == (0, 0), "pre-0.2.1 must not be a denominator"
    assert join_coverage(pre * 15 + null) == (0, 1)
    assert join_coverage(pre * 15 + null + hit) == (1, 2)
    assert join_coverage(hit * 3) == (3, 3)
    # total_tokens: no total_tokens key exists, so a real payload must sum to
    # the four counters, and junk/missing subkeys must degrade to 0 not raise.
    real = {"input_tokens": 2, "output_tokens": 1517,
            "cache_creation_input_tokens": 609, "cache_read_input_tokens": 96964}
    assert total_tokens(real) == 99092
    assert total_tokens({"total_tokens": 5000}) == 0, "no such key — must not be trusted"
    assert total_tokens({"input_tokens": 5}) == 5
    assert total_tokens({"input_tokens": "x", "output_tokens": 3}) == 3
    assert total_tokens(None) == 0 and total_tokens("nope") == 0
    # cross-file dedupe: a worktree copy is dropped, a same-file repeat is kept,
    # and key order / the _file tag never make two copies look different.
    a = {"ts": 1, "model": "haiku", "_file": "A"}
    a_copy = {"model": "haiku", "ts": 1, "_file": "B"}
    a_again = {"ts": 1, "model": "haiku", "_file": "A"}
    b = {"ts": 2, "model": "opus", "_file": "B"}
    kept, dropped = dedupe_across_files([a, a_again, a_copy, b])
    assert dropped == 1 and kept == [a, a_again, b], (kept, dropped)
    assert dedupe_across_files([]) == ([], 0)
    # any "event" key is an event, including kinds this version does not know
    assert is_event({"event": "session_start"}) and is_event({"event": "x"})
    assert not is_event({"model": "haiku"}) and not is_event({"event": ""})
    print("selftest OK")
    return 0


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
    if sys.argv[1:2] == ["--selftest"]:
        return _selftest()
    paths = resolve_paths(sys.argv[1:])
    if not paths:
        print("No gearbox-log.jsonl files found "
              "(looked for ~/**/.claude/gearbox-log.jsonl).")
        return 0

    all_rows, bad = load(paths)
    all_rows, copies = dedupe_across_files(all_rows)
    events = [r for r in all_rows if is_event(r)]
    rows = [r for r in all_rows if not is_event(r)]   # delegation records only
    total = len(rows)
    print("=" * 60)
    hdr = f"GEARBOX ROUTING LOG ANALYSIS — {len(paths)} file(s), {total} delegations"
    if events:
        hdr += f", {len(events)} event record(s)"
    print(hdr)
    if copies:
        print(f"(dropped {copies} record(s) duplicated across files — "
              f"worktree copies of one log, counted once)")
    if bad:
        print(f"(skipped {bad} malformed line(s))")
    print("=" * 60)

    if total == 0 and not events:
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
    vmodels = Counter(norm(r.get("model")) or "(empty)" for r in rows
                      if role_of(r.get("subagent_type")) == "verifier")
    if vmodels:
        print("  verifier model             : " + ", ".join(
            f"{k}:{v}" for k, v in vmodels.most_common()))
        print("    (the tier table says haiku; a project rule may raise it on")
        print("     purpose, e.g. sonnet for auth/migration review)")
    print("  NOTE: lower bound — file-modifying T1/T2 *should* be verified;")
    print("  read-only/escalated-without-edits correctly skip the verifier,")
    print("  and the log has no 'files modified' flag to distinguish them.")

    # [5] escalation / outcome fields
    all_keys = set()
    for r in rows:
        all_keys.update(k for k in r.keys() if not k.startswith("_"))
    extra = all_keys - KNOWN_FIELDS
    found_signal = all_keys & SIGNAL_FIELDS
    event_kinds = Counter(r.get("event") for r in events)
    print("\n[5] OUTCOME SIGNALS PRESENT")
    print(f"  on delegation records : "
          f"{sorted(found_signal) if found_signal else 'none'}")
    print(f"  event records         : " + (", ".join(
        f"{k}:{v}" for k, v in event_kinds.most_common()) or "none"))
    if not found_signal and not (event_kinds.keys() & {"verdict", "escalation"}):
        print("  NO OUTCOMES. The log records the routing DECISION (which")
        print("  agent/model) but nothing about how it went, so it cannot tell")
        print("  a good route from a bad one or train a learned router.")
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

    # [6] OUTCOMES (0.2.0) — the three numbers that were unmeasurable in 0.1.x
    print("\n[6] OUTCOMES (0.2.0)  ← fallback rate, verdicts, escalations")

    # 6a. HARD fallback rate, straight from the 0.2.0 fields (no inference)
    has_new = [r for r in rows if "fallback" in r and "is_named_tier" in r]
    n = len(has_new)
    if n:
        fb = sum(1 for r in has_new if r.get("fallback") is True)
        named = sum(1 for r in has_new if r.get("is_named_tier") is True)
        other = n - fb - named
        print(f"  fallback rate (hard, {n} 0.2.0 line(s)):")
        print(f"    named tier (gearbox:*) : {named:>4}  {pct(named, n):>7}  "
              f"{bar(named, n)}")
        print(f"    proxy fallback         : {fb:>4}  {pct(fb, n):>7}  "
              f"{bar(fb, n)}")
        print(f"    neither                : {other:>4}  {pct(other, n):>7}  "
              f"{bar(other, n)}")
    else:
        print("  fallback rate: no 0.2.0 lines yet — restart the session so the")
        print("    updated PostToolUse hook loads, then run a delegation.")
    old = total - n
    if old:
        inferred = sum(1 for r in rows if "fallback" not in r
                       and role_of(r.get("subagent_type")) in
                       ("explore (fallback)", "general-purpose"))
        print(f"  ({old} pre-0.2.0 line(s) lack the field; ~{inferred} look like "
              f"proxies by name — soft estimate, not counted above)")

    # 6b. verifier verdicts
    verdict_rows = [r for r in events if r.get("event") == "verdict"]
    verdicts = Counter(r.get("verdict") for r in verdict_rows)
    vtot = sum(verdicts.values())
    if vtot:
        appr, rej = verdicts.get("approve", 0), verdicts.get("reject", 0)
        print(f"  verifier verdicts: {vtot} total — "
              f"approve {appr} ({pct(appr, vtot)}), reject {rej} ({pct(rej, vtot)})")
        # join coverage (0.2.1): what fraction of *joinable* verdicts attach to
        # a delegation. A verdict with a non-null delegation_id was correlated
        # (heuristically — see log-verdict.py join_method); reward can only be
        # trained on the joined slice. Pre-0.2.1 verdicts are excluded from the
        # denominator, not scored as misses — see join_coverage().
        joined, joinable = join_coverage(verdict_rows)
        if joinable:
            print(f"  join coverage: {joined}/{joinable} = {pct(joined, joinable)} "
                  f"of post-0.2.1 verdicts attach to a delegation")
            methods = Counter(r.get("join_method") or "(unlabelled)"
                              for r in verdict_rows if "delegation_id" in r)
            print(f"    by method: "
                  f"{', '.join(f'{k}:{v}' for k, v in methods.most_common())}")
            if vtot > joinable:
                print(f"    ({vtot - joinable} pre-0.2.1 verdict(s) excluded — "
                      f"no delegation_id key, structurally unjoinable)")
        else:
            print("  join coverage: n/a (0 post-0.2.1 verdicts)")
            if vtot:
                print(f"    all {vtot} verdict(s) predate 0.2.1 — the join has "
                      f"not been exercised yet, which is not the same as 0%")
    else:
        print("  verifier verdicts: none logged (SubagentStop verdict capture "
              "inactive on this version, or no verifier runs yet)")

    # 6c. escalations
    esc = [r for r in events if r.get("event") == "escalation"]
    if esc:
        trans = Counter(f"{r.get('from_tier', '?')}->{r.get('to_tier', '?')}"
                        for r in esc)
        detail = ", ".join(f"{k}:{v}" for k, v in trans.most_common())
        print(f"  escalations: {len(esc)} ({detail})")
        down = sum(1 for r in esc
                   if TIER_RANK.get(str(r.get("from_tier")).upper(), -1)
                   > TIER_RANK.get(str(r.get("to_tier")).upper(), 99))
        if down:
            print(f"    ! {down} go DOWN a tier — not an escalation; check the "
                  f"[GEARBOX-ESCALATE from=.. to=..] marker order")
    else:
        print("  escalations: none logged (orchestrator records these manually; "
              "see routing.md rule 3)")

    # 7. cost signal — reward-per-cost is the 0.3.0 objective, so report both
    # whether cost is being captured at all and what it sums to.
    print("\n[7] COST SIGNAL  ← reward-per-cost input for 0.3.0")
    priced = [r for r in rows if isinstance(r.get("cost"), dict)]
    sources = Counter(r.get("cost_source") for r in rows
                      if "cost_source" in r)
    if not sources:
        print("  no delegation carries cost_source — pre-0.2.1 data only")
    else:
        have = sum(1 for r in rows
                   if r.get("cost_source") not in (None, "unavailable"))
        n = sum(sources.values())
        print(f"  cost captured: {have}/{n} = {pct(have, n)} of 0.2.1 delegations")
        print(f"  by source    : "
              f"{', '.join(f'{k}:{v}' for k, v in sources.most_common())}")
    if priced:
        by_tier = defaultdict(int)
        for r in priced:
            by_tier[MODEL_TIER.get(norm(r.get("model")), "(unknown)")] += \
                total_tokens(r["cost"])
        grand = sum(by_tier.values())
        print(f"  total tokens : {grand:,} across {len(priced)} priced delegation(s)")
        for tier, tok in sorted(by_tier.items(), key=lambda kv: -kv[1]):
            print(f"    {tier:18s} {tok:>12,}  {pct(tok, grand)}")
        print("  NOTE: summed from input+output+cache_creation+cache_read — "
              "tool_response.usage has NO total_tokens key.")

    # [8] sessions — only measurable from 0.2.4 session_start records
    started = {r.get("session_id") for r in events
               if r.get("event") == "session_start" and r.get("session_id")}
    print("\n[8] SESSIONS WITH ROUTING ACTIVE")
    if started:
        delegating = {r.get("session_id") for r in rows}
        idle = started - delegating
        print(f"  sessions started (0.2.4+) : {len(started)}")
        print(f"  ...with >=1 delegation    : {len(started) - len(idle)}")
        print(f"  ...with zero delegations  : {len(idle)}  "
              f"({pct(len(idle), len(started))})")
    else:
        print("  no session_start records — needs 0.2.4+ (written by the "
              "SessionStart hook)")

    # self-check: independent recount must agree, or the report is not trustworthy
    rc_total, rc_models, rc_events = independent_recount(paths)
    keys = set(models) | set(rc_models)
    ok = (rc_total == total) and (rc_events == len(events)) and all(
        rc_models.get(k, 0) == models.get(k, 0) for k in keys)
    print("\n[self-check] independent recount", end=" ")
    if ok:
        print(f"OK — {rc_total} delegations + {rc_events} event(s); "
              f"model tallies match primary pass.")
        return 0
    print("FAILED — primary vs recount disagree:")
    print(f"  delegations: primary={total} recount={rc_total}")
    print(f"  events:      primary={len(events)} recount={rc_events}")
    for k in sorted(keys):
        if models.get(k, 0) != rc_models.get(k, 0):
            print(f"  {k}: primary={models.get(k, 0)} recount={rc_models.get(k, 0)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
