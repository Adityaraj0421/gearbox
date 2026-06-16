# Gearbox benchmark template

Fill in 10 real tasks from YOUR repo. Run each task twice in fresh sessions:
once with Gearbox installed, once without (baseline, default model for
everything). Record cost from `/cost` (or ccusage for subscription quota)
and whether the result was acceptable without rework.

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

## Record per task

| # | tier routed | escalations | cost (router) | cost (baseline) | acceptable? | notes |
|---|-------------|-------------|---------------|-----------------|-------------|-------|

## What success looks like (v0)

- **Trivial tasks:** 60%+ cost reduction, zero quality loss
- **Medium:** within ~10% of baseline cost, equal quality
- **Hard:** zero quality regression (cost may rise slightly — fine)
- **Misroutes:** every "needs escalation" event logged, none silently failed

## Outcome-labeling runner

Once you've run real tasks with Gearbox installed, use `python3 bench/label.py`
to walk `~/.claude/gearbox-log.jsonl` and label each delegation acceptable or not.
Labeled rows are appended to `bench/training-data.jsonl` immediately, so you can
quit and resume at any time — already-labeled records are skipped. Run with
`--selfcheck` to verify the helper logic before labeling real data.
