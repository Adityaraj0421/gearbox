# Gearbox

Gearbox is a Claude Code plugin that automatically routes subagent delegations to the cheapest model tier that can handle the work — haiku for search and mechanical edits, sonnet for standard implementation, opus for hard architectural problems. It adds an escalation ladder so a cheap agent that gets stuck hands off to a more expensive one, and a verifier gate that catches gaming patterns (like reward-hacking an impossible test) before bad results are accepted. JSONL telemetry logs every delegation for future analysis.

## Install

**In your terminal:**

```bash
claude
```

**Inside the Claude Code session** (slash commands — these do not work in your shell):

```text
/plugin marketplace add Adityaraj0421/gearbox
/plugin install gearbox@gearbox
```

**At the scope prompt, choose user (all projects). If you accept the default, Gearbox only routes in the folder you installed from.**

Restart the session. The SessionStart hook activates routing automatically on every session start — no per-project setup required.

**Recommended:** set your session model to sonnet (`/model sonnet`) — this is the orchestrator tier. Gearbox controls subagent models; it does not override your main session model.

## Tier table

| Tier | Agent     | Model  | Use for |
|------|-----------|--------|---------|
| T0   | scout     | haiku  | exploration, search, reading, summarizing |
| T0   | grunt     | haiku  | mechanical edits, 1-2 files, zero design decisions |
| T1   | builder   | sonnet | features, bug fixes, tests, refactors ≤5 files |
| T2   | architect | opus   | cross-cutting design, concurrency, migrations, performance, security |

## Escalation ladder

When a cheaper tier reports "needs escalation" or fails twice on the same root cause, the orchestrator escalates exactly one tier and passes the full failure report. Hard floors apply regardless of classification: auth, payments, migrations, and concurrency start at T1 minimum; production-breaking risk starts at T2.

## Independent verifier

After any T1/T2 delegation that modified files, a verifier agent (haiku) reviews the diff before the result is accepted. It checks intent vs. letter, gaming patterns, and scope. Importantly: it receives a BASELINE git status snapshot taken before the delegation, so pre-existing uncommitted files are not misattributed to the implementer.

Verdict outcomes:
- **APPROVE** — change matches intent, no gaming, in scope
- **REJECT** — gaming pattern found or out-of-scope file touched; sends back to same tier once, then escalates
- **SKIPPED** — implementer escalated with no file changes; escalation ladder handles it

## Customizing the routing policy (optional)

Run `/gearbox:init` inside a project to create a local copy of the routing policy at `.claude/routing.md`. The SessionStart hook will inject your local copy instead of the plugin default. Edit `.claude/routing.md` to adjust tier thresholds, add project-specific hard floors, or extend the escalation rules.

## Routing prior (optional)

Run `/gearbox:recommend` to mine your `~/.claude/gearbox-log.jsonl` into a `{task-class × tier}` win-rate table — which tier historically earned the verifier's approval, at what cost, per kind of task. It writes `~/.claude/gearbox-recommendations.md`, and while that file is fresh the SessionStart hook injects it after the routing policy so the orchestrator can weigh it as a *prior*. It is advisory only: a tie-breaker, never an override of the hard floors, max-dimension routing, or the circuit breaker. Cells below a minimum sample count are flagged `low-n` and earn no recommendation, so a thin log cannot skew routing.

## Integrating with an existing CLAUDE.md

If you already have delegation, agent, or model-selection rules in your CLAUDE.md, reconcile them before first use:

- **Trim duplicate tier/routing rules.** Gearbox injects its full routing policy (tier table, classification, escalation ladder, verifier protocol) at SessionStart. If your CLAUDE.md already covers any of this, remove it — duplicate or conflicting instructions confuse the orchestrator. Keep only what Gearbox doesn't cover: project-specific hard floors (e.g. "never delegate auth changes below T2") and house rules. The right home for project-specific routing overrides is `.claude/routing.md` via `/gearbox:init`, not CLAUDE.md.
- **Remove colliding user-level agent files.** Legacy files in `~/.claude/agents/` whose names match Gearbox agents (scout, grunt, builder, architect, verifier) shadow the plugin agents across every project. Delete or rename any conflicts. Run `/gearbox:doctor` (CHECK 7) to detect both user-level and project-level collisions.
- **Use namespaced agent names.** In any prompts or rules you keep, reference agents by their full names: `gearbox:scout`, `gearbox:grunt`, `gearbox:builder`, `gearbox:architect`, `gearbox:verifier`.
- **Set your main session model to sonnet.** Run `/model sonnet` — Gearbox routes subagents but does not change your orchestrator model.

## Troubleshooting

Something not working? Run `/gearbox:doctor` first — it checks the ten most common failure modes and tells you the fix. Paste its output into any issue you file.

## Known limitations

- **Dirty-file blind spot (mitigated):** The verifier requires a BASELINE snapshot, but the orchestrator must remember to capture and pass it before each T1/T2 delegation. If omitted, the verifier falls back to full-diff scope-checking, which can false-reject in repos with pre-existing uncommitted changes.
- **Agents load on session start:** If you add or update agent files, restart your Claude Code session before the new definitions take effect.
- **Effort propagation untested:** The `ultrathink` directive in T2 prompts has not been verified to propagate to subagents across all surfaces. Treat it as experimental.
- **SessionStart hook injection:** The routing policy is injected via a SessionStart hook. Some Claude Code surfaces may handle hook output differently — if routing rules seem absent, run `/gearbox:init` to create a project-local copy at `.claude/routing.md`, which the hook will prefer over the plugin default.
- **Routing policy context cost:** The routing policy is injected each session start (~2.5KB context cost).
- **Agent namespacing:** Gearbox agents install as `gearbox:scout`, `gearbox:grunt`, `gearbox:builder`, `gearbox:architect`, and `gearbox:verifier`. Reference them by these full names in prompts and routing rules.
- **Routing prior is reward-sparse:** the `{task-class × tier}` prior only earns a recommendation where verifier verdicts exist (T1/T2 edits), so early on most cells are `low-n` and unrecommended. It sharpens as the log accumulates verdicts; until then it mostly confirms the cheap-tier defaults.

## Roadmap

- **0.2.0** — PreToolUse hook auto-captures `git status --short` BASELINE before every T1/T2 delegation; verifier always receives it, guaranteed rather than instructed.
- **0.3.0** — Observability & data quality: the routing log consolidates to a single global `~/.claude/gearbox-log.jsonl`; `bench/dashboard.py` reports per-tier rollups (verdict-reject rate, cost); `bench/eval.py` scores routing against a modeled baseline offline; blended cost rates are date-pinned; doctor CHECK 4 gives concrete scope-fix steps.
- **0.4.0** — Learned router (shipped): a *static win-rate prior* mined from `gearbox-log.jsonl` outcomes. `bench/recommend.py` builds a min-sample-guarded `{task-class × tier}` table (verifier approve-rate + mean cost); `/gearbox:recommend` regenerates and prints it; the SessionStart hook injects it (while fresh) after the policy as an advisory prior — a tie-breaker that never overrides the hard floors, max-dimension routing, or the circuit breaker.
- **Post-1.0.0** — Revisit once the static prior proves the data is worth modeling: a contextual bandit over `{task-class × model}` with online exploration (plus mined session-transcript signals), auto-regeneration of the prior, and per-project tables keyed by `cwd`.

## Telemetry

Each Task delegation appends one JSONL line to a single global log at `~/.claude/gearbox-log.jsonl`. Fields: `ts`, `session_id`, `tool_name`, `subagent_type`, `model`, `prompt_head` (first 200 chars), `cwd`, plus post-completion metrics parsed from the tool response — `total_tokens`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `num_turns`, `duration_ms`, `cost_usd`, and `cost_estimated` (false when `cost_usd` is computed exactly from the per-component token split; true when derived from a blended per-model rate because the split was absent). Missing metrics are recorded as null. Each record keeps its `cwd`, so per-project views are a `group by cwd` over one global corpus. The log stays on your machine — it is not sent anywhere.

## License

MIT — see [LICENSE](LICENSE).
