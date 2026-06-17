# Gearbox benchmark tasks

Two complementary evaluation paths:

1. **Offline (label.py + eval.py):** label real delegations from your own
   `~/.claude/gearbox-log.jsonl` and score them — no second Claude run needed.
2. **Measured (run-live.py):** run the committed fixture task set under each
   routing policy via headless `claude -p` to capture real cost and
   acceptability for cost-at-equal-quality comparison.

---

## Offline path (label + score)

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
cost, and cost-saved %.  Three baselines are **modeled offline** using the
same token counts the router recorded × per-tier blended rates (2026-06:
haiku $1.5/M, sonnet $5.0/M, opus $8.0/M):

- **always-sonnet** — every task dispatched to sonnet regardless of complexity
  (measured via the `always-t1` profile → `gearbox:builder`).
- **always-opus** — every task dispatched to opus (quality ceiling). Measured via
  the `always-opus-build` profile (`gearbox:builder` on opus), **not** `always-t2`:
  `always-t2` routes to the read-only `gearbox:architect`, which can't complete
  editing tasks under a forced profile, so it would understate opus on a structural
  technicality. `always-opus-build` uses the edit-capable builder so the baseline is
  faithful.
- **escalate-on-fail** — starts at T0, escalates on failure; pays wasted
  cheaper attempts.  This is the router's core value prop: skip straight to
  the right tier.

TV (verifier) dispatches are excluded from all baselines.  Token counts are
assumed policy-invariant.  These are rough estimates, NOT measured
counterfactuals.

---

## Measured path (R1-live)

### Fixture: `bench/fixtures/toy-cli/`

A committed minimal Python CLI project used as a deterministic benchmark
harness:

| File | Purpose |
|------|---------|
| `app.py` | Argparse CLI with known bugs (ZeroDivisionError, unlocked file append) |
| `test_app.py` | Pytest tests; some fail on the unmodified fixture by design |
| `README.md` | Short project description (gives T0 something to summarize) |
| `tasks.jsonl` | 3 tasks — one per tier |

### Tasks

| ID | Tier | Description | Accept grader |
|----|------|-------------|---------------|
| `t0-summarize` | T0 | Summarize every function in app.py | `true` (always-accept; read-only task — value is routing cost, not a diff) |
| `t1-divide-fix` | T1 | Fix `divide(a,0)` uncaught ZeroDivisionError → return None; add a test | `python3 -m pytest -q` |
| `t2-state-race` | T2 | Diagnose + fix the concurrency race in `append_state` (no file locking) | `grep -qE 'import fcntl\|flock\(\|...' app.py` |

The T1 and T2 accept graders FAIL on the unmodified fixture and PASS on a
correct solution.  The T0 grader is always-accept because the task is
read-only; the measured value is cost, and binding (did haiku run?) is checked
via `modelUsage`.

### Running the benchmark

```bash
# Dry-run: print what would execute
python3 bench/run-live.py

# Live run (spends real money — LOCAL ONLY, never in CI)
python3 bench/run-live.py --live

# Limit tasks or budget
python3 bench/run-live.py --live --scale 1 --max-cost 0.50

# Choose specific policies
python3 bench/run-live.py --live --policies live always-sonnet
```

**Cost floor:** each `claude -p` run costs ~$0.04 minimum (the SessionStart
injection adds ~20 k cache-creation tokens).  The harness uses
`PER_RUN_EST = $0.12/run` as the budget estimate.  Default max-cost is $2.00
(9 runs × $0.12 = $1.08 estimated).

**SAFETY: `--permission-mode bypassPermissions`** — this flag bypasses all
permission prompts.  Run only locally in the temp workdir the harness
creates.  Never add a `--live` CI step — it spends real money and bypasses
permission gates.

### modelUsage binding check

After each run, the harness checks `modelUsage` keys in the claude -p JSON
envelope (e.g. `"claude-haiku-4-5-20251001"`, `"claude-sonnet-4-..."`) to
verify the routing policy was obeyed:

- T0 expected → haiku family must be present
- T1 expected → sonnet family must be present
- T2 expected → opus family must be present

`bound=False` rows are written to `bench/training-data.jsonl` with
`subagent_type=(unbound)`, which eval.py resolves to `(unknown)` and excludes
from all cost totals.  A live-policy run with many unbound rows signals
under-delegation.

### Aggregation

After a live run, the harness imports `eval.py`'s `compute_policy_totals` and
`print_policy_comparison` directly — the math is not duplicated.  It also
prints a compact per-policy summary (bound count, mean cost, accept rate) so
you can see real cost-at-equal-acceptability and any routing drift.

The ledger summary line (written to `bench/last-run-summary.txt`) has the
format used by the agent-research cost ledger:

```
total_cost_usd=<sum>  duration_ms=<sum>  num_turns=<sum>
```

### Selfcheck

```
python3 bench/run-live.py --selfcheck
```

Runs offline assert tests on all pure helpers (parse_envelope, model_families,
policy_bound, scrape_verdict, load_tasks, build_row, estimate_cost,
summary_line) and cross-imports eval.py to prove schema compatibility.  No
`claude -p`, no network.  This step runs in CI.
