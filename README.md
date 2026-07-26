# Gearbox

[![Latest release](https://img.shields.io/github/v/release/Adityaraj0421/gearbox?sort=semver&color=8A63D2)](https://github.com/Adityaraj0421/gearbox/releases/latest)
[![License: MIT](https://img.shields.io/github/license/Adityaraj0421/gearbox?color=blue)](LICENSE)
[![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A63D2)](#install)
[![Routing telemetry](https://img.shields.io/badge/routing-JSONL%20telemetry-2ea44f)](#measuring-your-routing)

Gearbox is a Claude Code plugin that automatically routes subagent delegations to the cheapest model tier that can handle the work — haiku for search and mechanical edits, sonnet for standard implementation, opus for hard architectural problems. It adds an escalation ladder so a cheap agent that gets stuck hands off to a more expensive one, and a verifier gate that catches gaming patterns (like reward-hacking an impossible test) before bad results are accepted. JSONL telemetry logs every delegation and its outcomes — which tier ran it, whether a named agent or a generic proxy handled it, verifier verdicts, and escalations — so you can measure how your routing actually behaves.

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

## Changelog / Roadmap

- **0.2.2 (current)** — Instrument honesty. Two reporting fixes, no behaviour change to routing or the hooks. (1) `bench/analyze-log.py` join coverage now counts only verdicts carrying a `delegation_id` key — pre-0.2.1 verdicts have no such key, are structurally unjoinable, and were being scored as misses, so a join that had never run once reported "0.0% coverage". With no post-0.2.1 verdicts it prints `n/a (0 post-0.2.1 verdicts)`. A `--selftest` flag asserts the denominator. (2) `/gearbox:doctor` CHECK 10 reports `INSUFFICIENT_DATA` instead of `PASS` below the T1/T2 threshold: a vacuous pass claimed escalation logging worked when it had simply never been exercised.
- **0.2.1** — Reward attribution + honest instrument. `bench/analyze-log.py` auto-discovery now de-duplicates by realpath and skips `~/Library/Containers` (macOS mirrors `~/Downloads` into every app sandbox, so the old `~/**` glob multi-counted one throwaway into ~95% of all "traffic"). Every delegation carries a `delegation_id` and a best-effort `cost`/`cost_source`; the `SubagentStop` verdict logger echoes the `delegation_id` it judged (heuristic, labelled via `join_method`), and the analyzer reports verdict→delegation join coverage. Escalation logging moves into a `PreToolUse` hook triggered by a `[GEARBOX-ESCALATE from=Tx to=Ty]` marker (routing.md rule 3), and `/gearbox:doctor` gains CHECK 10, which warns when a log has T1/T2 volume but zero escalations.
- **0.2.0** — Outcome logging. Every delegation now records `is_named_tier` (a `gearbox:` tier agent handled it) and `fallback` (a generic `general-purpose`/`Explore` proxy handled it instead). A `SubagentStop` hook logs `gearbox:verifier` `approve`/`reject` verdicts, and the orchestrator logs tier escalations. `bench/analyze-log.py` now reports a hard fallback rate, the verifier approve/reject ratio, and escalation frequency; `/gearbox:doctor` gains CHECK 9 to confirm the new schema is live.
- **Planned** — PreToolUse hook auto-captures `git status --short` BASELINE before every T1/T2 delegation, so the verifier always receives it (guaranteed rather than instructed).
- **0.3.0** — Learned router trained on `gearbox-log.jsonl` outcomes: a contextual bandit over `{task-type × model}` pairs, replacing the static rubric with a policy that improves with use. The 0.2.0 outcome fields are the reward signal it needs.

## Telemetry

Each Task delegation appends one JSONL line to `.claude/gearbox-log.jsonl` in your project. Delegation fields: `ts`, `session_id`, `tool_name`, `subagent_type`, `is_named_tier`, `fallback`, `model`, `prompt_head` (first 200 chars), `cwd`.

As of 0.2.0 the log also records **outcome events** on their own lines:
- `{"event":"verdict","verdict":"approve"|"reject", ...}` — written by a `SubagentStop` hook when `gearbox:verifier` finishes.
- `{"event":"escalation","from_tier","to_tier","reason", ...}` — written by the orchestrator each time it escalates a tier.

The log stays in your project — it is not sent anywhere.

## Measuring your routing

`bench/analyze-log.py` aggregates your `gearbox-log.jsonl` files and reports the tier split (haiku/sonnet/opus), the agent distribution, verifier coverage, the date range, and — as of 0.2.0 — an **outcomes** section: a hard fallback rate (named `gearbox:` tier vs generic proxy, counted from the `fallback`/`is_named_tier` fields rather than guessed), the verifier approve/reject ratio, and escalation frequency. An independent recount asserts its own totals before printing.

```bash
python3 bench/analyze-log.py          # globs ~ for every .claude/gearbox-log.jsonl
# or pass explicit paths:  python3 bench/analyze-log.py path/to/.claude/gearbox-log.jsonl
```

Two caveats, both honest gaps: verdict capture depends on your Claude Code version surfacing the verifier's output to `SubagentStop` (if no `{"event":"verdict"}` lines ever appear, it is inactive on your version, and the verdict stays a manual field); escalation logging is instructed in the routing policy, not enforced, so escalation counts are a floor. The new fields only appear after you restart the session so the updated hook loads — confirm with `/gearbox:doctor` (CHECK 9).

## License

MIT — see [LICENSE](LICENSE).
