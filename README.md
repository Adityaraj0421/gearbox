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

Something not working? Run `/gearbox:doctor` first — it checks the eleven most common failure modes and tells you the fix. Paste its output into any issue you file.

## Known limitations

- **Dirty-file blind spot (mitigated):** The verifier requires a BASELINE snapshot, but the orchestrator must remember to capture and pass it before each T1/T2 delegation. If omitted, the verifier falls back to full-diff scope-checking, which can false-reject in repos with pre-existing uncommitted changes.
- **Agents load on session start:** If you add or update agent files, restart your Claude Code session before the new definitions take effect.
- **Effort propagation untested:** The `ultrathink` directive in T2 prompts has not been verified to propagate to subagents across all surfaces. Treat it as experimental.
- **SessionStart hook injection:** The routing policy is injected via a SessionStart hook. Some Claude Code surfaces may handle hook output differently — if routing rules seem absent, run `/gearbox:init` to create a project-local copy at `.claude/routing.md`, which the hook will prefer over the plugin default.
- **Routing policy context cost:** The routing policy is injected each session start (~2.5KB context cost).
- **Agent namespacing:** Gearbox agents install as `gearbox:scout`, `gearbox:grunt`, `gearbox:builder`, `gearbox:architect`, and `gearbox:verifier`. Reference them by these full names in prompts and routing rules.

## Changelog / Roadmap

- **0.2.4 (current)** — Measure before building the reward. A real telemetry export (987 delegations, 15 logs) showed 0.2.3 fixed named-agent dispatch (21% → 91% named, no proxy since early September) but left the 0.3.0 reward signal unusable: every verdict is joined to its delegation by a heuristic, 74% of post-0.2.3 delegations carry no token data (0% for background agents), and there is one escalation in 394 T1/T2 delegations. The fixes for those depend on hook payload facts nobody had checked, so 0.2.4 ships the instrument first: an opt-in **payload probe** (`touch .claude/gearbox-probe.on`) that records the shape of every hook payload — key names, types, hashed ids, never prompt or response text — to `.claude/gearbox-probe.jsonl`. The SessionStart hook now writes a `{"event":"session_start"}` record, so sessions that never delegated become countable (analyzer section [8]). `bench/analyze-log.py` drops records duplicated across files (worktrees copy the project log, which inflated one real report by 20 records), treats any `event` record as an event, lists event kinds in section [5] instead of a stale field scan, shows which model the verifier ran on, and flags "escalations" that go down a tier. `/gearbox:doctor` CHECK 8 passes the plugin root as an argument, because `${CLAUDE_PLUGIN_ROOT}` is expanded in the command text but not exported to the shell.
- **0.2.3** — Named-agent enforcement. Telemetry showed 28 consecutive real delegations running as `general-purpose` while all 5 `gearbox:` agents were installed — the tier *shape* was followed but the named agents were not dispatched, so `is_named_tier` was false, no verdict records were written, and nothing could be joined to a reward. Root cause was policy phrasing: routing.md named the agents in a reference *table* but its only imperative was "pass `model` explicitly", so a proxy dispatch violated no stated rule. Rule 1 now mandates the namespaced `subagent_type` alongside the explicit model and states the telemetry consequence; rule 8's proxy fallback is gated on a genuine agent-not-found error rather than convenience; rule 9 spells out that `gearbox:verifier` is load-bearing because the `SubagentStop` logger self-filters on it. `/gearbox:doctor` gains CHECK 11, which warns when recent delegations ran through proxies *while the named agents were available*. `bench/analyze-log.py` adds `total_tokens()` and a `[7] COST SIGNAL` section — `tool_response.usage` has no `total_tokens` key, so cost must be summed from `input + output + cache_creation + cache_read` or a log full of real token data reports zero.
- **0.2.2** — Instrument honesty. Two reporting fixes, no behaviour change to routing or the hooks. (1) `bench/analyze-log.py` join coverage now counts only verdicts carrying a `delegation_id` key — pre-0.2.1 verdicts have no such key, are structurally unjoinable, and were being scored as misses, so a join that had never run once reported "0.0% coverage". With no post-0.2.1 verdicts it prints `n/a (0 post-0.2.1 verdicts)`. A `--selftest` flag asserts the denominator. (2) `/gearbox:doctor` CHECK 10 reports `INSUFFICIENT_DATA` instead of `PASS` below the T1/T2 threshold: a vacuous pass claimed escalation logging worked when it had simply never been exercised.
- **0.2.1** — Reward attribution + honest instrument. `bench/analyze-log.py` auto-discovery now de-duplicates by realpath and skips `~/Library/Containers` (macOS mirrors `~/Downloads` into every app sandbox, so the old `~/**` glob multi-counted one throwaway into ~95% of all "traffic"). Every delegation carries a `delegation_id` and a best-effort `cost`/`cost_source`; the `SubagentStop` verdict logger echoes the `delegation_id` it judged (heuristic, labelled via `join_method`), and the analyzer reports verdict→delegation join coverage. Escalation logging moves into a `PreToolUse` hook triggered by a `[GEARBOX-ESCALATE from=Tx to=Ty]` marker (routing.md rule 3), and `/gearbox:doctor` gains CHECK 10, which warns when a log has T1/T2 volume but zero escalations.
- **0.2.0** — Outcome logging. Every delegation now records `is_named_tier` (a `gearbox:` tier agent handled it) and `fallback` (a generic `general-purpose`/`Explore` proxy handled it instead). A `SubagentStop` hook logs `gearbox:verifier` `approve`/`reject` verdicts, and the orchestrator logs tier escalations. `bench/analyze-log.py` now reports a hard fallback rate, the verifier approve/reject ratio, and escalation frequency; `/gearbox:doctor` gains CHECK 9 to confirm the new schema is live.
- **Next (0.2.5, shaped by probe data)** — A completion record written when each subagent finishes, carrying tokens read from the agent's own transcript (fixing the background-agent cost gap) and `files_modified` from a `git status` snapshot taken at dispatch. That snapshot is also the BASELINE the verifier needs, captured by a hook instead of by instruction. Verdicts then join to the most recent *finished, file-modifying* delegation instead of the most recent T1/T2 one, which stops read-only architect reviews absorbing builder verdicts; an exact id join follows if the probe shows the hooks can carry one. Escalation events gain a `delegation_id`.
- **0.3.0** — Learned router trained on `gearbox-log.jsonl` outcomes: a contextual bandit over `{task-type × model}` pairs, replacing the static rubric with a policy that improves with use. The 0.2.0 outcome fields are the reward signal it needs.

## Telemetry

Each Task delegation appends one JSONL line to `.claude/gearbox-log.jsonl` in your project. Delegation fields: `ts`, `delegation_id`, `session_id`, `tool_name`, `subagent_type`, `is_named_tier`, `fallback`, `model`, `prompt_head` (first 200 chars), `cwd`, `cost_source`, and `cost` (the subagent's token usage, when the hook payload carries it).

As of 0.2.0 the log also records **outcome events** on their own lines:
- `{"event":"verdict","verdict":"approve"|"reject", ...}` — written by a `SubagentStop` hook when `gearbox:verifier` finishes.
- `{"event":"escalation","from_tier","to_tier","reason", ...}` — written by the orchestrator each time it escalates a tier.

As of 0.2.4 the SessionStart hook also writes `{"event":"session_start","source":...}` once per session start, so sessions with routing active but zero delegations can be counted.

The log stays in your project — it is not sent anywhere.

### Payload probe (0.2.4, opt-in)

To help answer what Claude Code's hooks actually receive on your version — which ids, whether a background agent reports token usage — turn the probe on in one project:

```bash
touch .claude/gearbox-probe.on     # off again: rm .claude/gearbox-probe.on
```

No restart is needed. Run a few delegations, including at least one background agent, then look at `.claude/gearbox-probe.jsonl`. Each line holds the payload's key names and value types, a short allowlist of non-content values (hook, tool, agent and model names, `run_in_background`), ids as 8-character hashes so they can be matched across hooks, and whether `*_path` files exist. Prompt, response, path and message text are never written. Read it before sharing it anyway.

## Measuring your routing

`bench/analyze-log.py` aggregates your `gearbox-log.jsonl` files and reports the tier split (haiku/sonnet/opus), the agent distribution, verifier coverage, the date range, and — as of 0.2.0 — an **outcomes** section: a hard fallback rate (named `gearbox:` tier vs generic proxy, counted from the `fallback`/`is_named_tier` fields rather than guessed), the verifier approve/reject ratio, and escalation frequency. It also reports verdict→delegation join coverage and token cost per tier (0.2.1+), and, from 0.2.4, the model the verifier ran on, "escalations" that went down a tier, and sessions that started with routing active but never delegated. Records copied across files — a worktree keeps its own copy of the project log — are counted once, and the header says how many were dropped. An independent recount asserts its own totals before printing; `--selftest` checks the analyzer's own arithmetic.

```bash
python3 bench/analyze-log.py          # walks ~ for every .claude/gearbox-log.jsonl
# or pass explicit paths:  python3 bench/analyze-log.py path/to/.claude/gearbox-log.jsonl
```

Two caveats, both honest gaps: verdict capture depends on your Claude Code version surfacing the verifier's output to `SubagentStop` (if no `{"event":"verdict"}` lines ever appear, it is inactive on your version, and the verdict stays a manual field); escalation logging depends on the orchestrator adding the `[GEARBOX-ESCALATE]` marker (routing.md rule 3), so escalation counts are a floor; and token cost is only captured when the `PostToolUse` payload carries usage, which appears not to happen for background agents (0.2.5 targets this — see the payload probe above). The new fields only appear after you restart the session so the updated hook loads — confirm with `/gearbox:doctor` (CHECK 9).

## License

MIT — see [LICENSE](LICENSE).
