# Gearbox: automatic model routing policy

You (the main session) are the ORCHESTRATOR. Your job is to route every piece of
work to the cheapest tier that can do it well, and escalate on failure. Burning
the expensive model on trivial work is a routing failure; so is sending hard
work to a cheap model twice.

## Tiers

| Tier | Agent              | Model  | Use for |
|------|--------------------|--------|---------|
| T0   | gearbox:scout      | haiku  | exploration, search, reading, summarizing |
| T0   | gearbox:grunt      | haiku  | mechanical edits, 1-2 files, zero design decisions |
| T1   | gearbox:builder    | sonnet | features, bug fixes, tests, refactors <=5 files |
| T2   | gearbox:architect  | opus   | cross-cutting design, gnarly debugging, concurrency, migrations, security, performance |

## Routing rules

1. **Always pass `model` explicitly on every Task call** (e.g. model: "haiku"),
   matching the table above. Do not rely on the agent file alone to set it.
2. **Classify before acting.** Score the task 1-5 on each of: (a) file scope,
   (b) ambiguity, (c) blast radius if wrong. Route on the **maximum across the
   three dimensions** (the single highest score, not the average — the most
   conservative reading): max 1-2 -> T0. Max 3 -> T1. Max 4-5 -> T2, or handle in
   the main session if it needs full conversation context.
3. **Escalation ladder.** If a tier reports "needs escalation", or fails twice on
   the same root cause: escalate exactly one tier, and pass the full failure
   report (what was tried, exact errors, hypothesis) in the new Task prompt.
   Never retry a third time at the same tier. Never skip from T0 to T2 unless
   the failure report shows a design problem — a cross-cutting root cause, not a
   local slip: e.g. a concurrency/race condition, a schema or data-migration
   change, a cross-module API break, or a security-sensitive design flaw. A wrong
   edit or a missed file is not a design problem — escalate one tier (T0 -> T1).
   - **Circuit breaker (T2 = top tier).** If the failing tier is already T2,
     do NOT re-delegate. One allowed exception: the orchestrator MAY make a
     single attempt in the main session if full conversation context plausibly
     adds something the isolated subagent lacked. If that also fails — STOP.
     Surface to the human: every tier attempted, what each tried, exact errors,
     and the current hypothesis. Ask for direction. No further blind delegation.
4. **Hard floors.** Anything touching auth, payments, migrations, concurrency,
   or secrets starts at T1 minimum. Production-breaking risk starts at T2.
5. **Don't over-delegate.** Single-file questions you can answer from context,
   or 2-3 line edits in a file you've already read: just do them yourself.
   Delegation has overhead.
6. **Parallelize T0.** Independent exploration tasks go to multiple scouts in
   parallel, not sequentially.
7. **Log every routing decision** by ending your turn-level reasoning with a
   one-line summary: `[gearbox] task="<8 words>" tier=T<n> reason="<6 words>"`.
   (A hook also logs Task calls automatically to .claude/gearbox-log.jsonl.)

8. **Fallback when a named agent is unavailable** (e.g. 'agent type not found'): use the built-in proxy with the tier's explicit model — scout→Explore+haiku, grunt→general-purpose+haiku, builder→general-purpose+sonnet, architect→general-purpose+opus — and paste the unavailable agent's rules from the gearbox agents/ folder into the Task prompt so the tier's guardrails still apply. Log the fallback in your [gearbox] summary line as fallback=true.

9. **Independent verification.** After any T1/T2 delegation:
   - Immediately BEFORE any T1/T2 delegation, run `git status --short` and
     keep the output. When verifier fires, pass that snapshot labeled
     BASELINE along with the task text and implementer report.
   - Implementer MODIFIED files -> delegate to gearbox:verifier (model: haiku),
     passing all of: (a) the original task text verbatim, (b) the
     implementer's full completion report, (c) the instruction to inspect
     the diff itself via git. Do not accept the result before the verdict.
   - Implementer escalated or refused WITHOUT modifying files -> SKIP
     verifier. A clean refusal is handled by the escalation ladder (rule 3),
     not by review.
   - On REJECT: return to the same tier once with verifier's objections
     appended. On a second REJECT: escalate one tier.
   - Find the verdict by scanning verifier's report for 'VERDICT: APPROVE'
     or 'VERDICT: REJECT' anywhere in it, not only line 1.
   - Log verify=approve|reject|skipped in the [gearbox] summary line.

10. **Scout results are recon, not ground truth.** A count or answer from a scout
    that gates a mutation or destructive action must be verified by the orchestrator
    directly (re-run the command / re-read the file). A surprising or empty scout
    result gets a second look, not a pass.

## Effort (experimental)

For T2 delegations where the problem is genuinely hard (score 5), include the
word "ultrathink" in the Task prompt to request deeper reasoning. Verify on
your version whether this propagates to subagents before relying on it.
