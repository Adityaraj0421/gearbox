# Gearbox benchmark template

Fill in 10 real tasks from YOUR repo.  Run them **normally with Gearbox
installed** (interactive, as usual — NOT `claude -p`), then label and score
them with the two commands below.  No second "without Gearbox" run is needed:
the baseline is modeled offline (see "Baseline" below).

## Trivial (expect T0 — should be much cheaper than baseline)

1. e.g. "Where is the retry logic for [the key module in your codebase]?"
2. e.g. "Fix the typo in [a specific error message]"
3. e.g. "Summarize what routes/endpoints exist in [your main router file]"

## Medium (expect T1 — cost roughly equal to baseline, quality equal)

4. e.g. "Add input validation to [a specific endpoint] + tests"
5. e.g. "Refactor [a repeated pattern] into a shared utility"
6. e.g. "Write tests for [a specific pure function]"
7. e.g. "Add pagination to [a list API]"

## Hard (expect T2 — the test is that quality does NOT drop vs baseline)

8. e.g. "Concurrency: two workers write the same record — find and fix the race"
9. e.g. "Design migration from [current schema] to [new schema]"
10. e.g. "P95 latency doubled after last deploy — investigate root cause"

## What success looks like (v0)

- **Trivial tasks:** 60%+ cost reduction, zero quality loss
- **Medium:** within ~10% of baseline cost, equal quality
- **Hard:** zero quality regression (cost may rise slightly — fine)
- **Misroutes:** every "needs escalation" event logged, none silently failed

## How to evaluate

After running your tasks with Gearbox:

**Step 1 — label dispatches:**

```
python3 bench/label.py
```

Walks `~/.claude/gearbox-log.jsonl` and asks y/n/s/q for each delegation.
Labels are appended to `bench/training-data.jsonl`; already-labeled records
are skipped so you can quit and resume at any time.

**Step 2 — score:**

```
python3 bench/eval.py
```

Prints a per-tier scorecard: acceptability rate, router cost, modeled baseline
cost, and cost-saved %.  Run `--selfcheck` to verify the aggregation logic
before scoring real data.

## Baseline

Three baselines are **modeled offline** — no second Claude run, no `claude -p`.
All use the same token counts the router recorded, multiplied by per-tier
blended rates (2026-06: haiku $1.5/M, sonnet $5.0/M, opus $8.0/M):

- **always-sonnet** — every task dispatched to sonnet regardless of complexity.
- **always-opus** — every task dispatched to opus (quality ceiling).
- **escalate-on-fail** — starts at the cheapest tier (T0) and escalates one
  tier on failure until reaching the tier the router chose, paying for the
  wasted cheaper attempts.  This is the router's core value prop: skip straight
  to the right tier.

TV (verifier) dispatches are excluded from all baselines — they aren't routing
decisions.  Token counts are assumed policy-invariant.  These are rough
estimates, NOT measured counterfactuals.

## Outcome-labeling runner

Once you've run real tasks with Gearbox installed, use `python3 bench/label.py`
to walk `~/.claude/gearbox-log.jsonl` and label each delegation acceptable or not.
Labeled rows are appended to `bench/training-data.jsonl` immediately, so you can
quit and resume at any time — already-labeled records are skipped.  Run with
`--selfcheck` to verify the helper logic before labeling real data.
