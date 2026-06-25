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

## Troubleshooting

Something not working? Run `/gearbox:doctor` first — it checks the ten most common failure modes and tells you the fix. Paste its output into any issue you file.

## Known limitations

- **Dirty-file blind spot (mitigated):** The verifier requires a BASELINE snapshot, but the orchestrator must remember to capture and pass it before each T1/T2 delegation. If omitted, the verifier falls back to full-diff scope-checking, which can false-reject in repos with pre-existing uncommitted changes.
- **Agents load on session start:** If you add or update agent files, restart your Claude Code session before the new definitions take effect.
- **Effort propagation untested:** The `ultrathink` directive in T2 prompts has not been verified to propagate to subagents across all surfaces. Treat it as experimental.
- **SessionStart hook injection:** The routing policy is injected via a SessionStart hook. Some Claude Code surfaces may handle hook output differently — if routing rules seem absent, run `/gearbox:init` to create a project-local copy at `.claude/routing.md`, which the hook will prefer over the plugin default.
- **Routing policy context cost:** The routing policy is injected each session start (~2.5KB context cost).
- **Agent namespacing:** Gearbox agents install as `gearbox:scout`, `gearbox:grunt`, `gearbox:builder`, `gearbox:architect`, and `gearbox:verifier`. Reference them by these full names in prompts and routing rules.

## Roadmap

- **0.2.0** — PreToolUse hook auto-captures `git status --short` BASELINE before every T1/T2 delegation; verifier always receives it, guaranteed rather than instructed.
- **0.3.0** — Learned router trained on `gearbox-log.jsonl` outcomes: a contextual bandit over `{task-type × model}` pairs, replacing the static rubric with a policy that improves with use.

## Telemetry

Each Task delegation appends one JSONL line to `.claude/gearbox-log.jsonl` in your project. Fields: `ts`, `session_id`, `subagent_type`, `model`, `prompt_head` (first 200 chars), `cwd`. The log stays in your project — it is not sent anywhere.

## Measuring your routing

`bench/analyze-log.py` aggregates your `gearbox-log.jsonl` files and reports the tier split (haiku/sonnet/opus), the agent distribution, verifier coverage, and date range — with an independent recount that asserts its own totals before printing.

```bash
python3 bench/analyze-log.py          # globs ~ for every .claude/gearbox-log.jsonl
# or pass explicit paths:  python3 bench/analyze-log.py path/to/.claude/gearbox-log.jsonl
```

Note: the log currently captures routing **decisions** (which agent, which model) but not **outcomes** — there is no escalation, verifier-verdict, or fallback field yet, so the analyzer can show *how* work was routed but not whether each route was *right*. Capturing outcomes is the 0.2 work that turns this from a description into a measurement.

## License

MIT — see [LICENSE](LICENSE).
