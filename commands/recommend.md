---
name: recommend
description: "Regenerate the routing-prior table and print it"
---

Run the following steps IN ORDER via Bash.

---

## Step 1 — regenerate the routing-prior artifact

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bench/recommend.py"
```

- If the script exits 0 → continue to Step 2.
- If it exits non-zero or `CLAUDE_PLUGIN_ROOT` is unset → print the error
  verbatim and stop. (Run `/gearbox:doctor` CHECK 0 to diagnose the plugin
  root issue.)

---

## Step 2 — read and print the artifact

```bash
cat ~/.claude/gearbox-recommendations.md
```

Print the full contents to the user exactly as returned.

---

## Note

The table above is **advisory** — a routing prior and tie-breaker derived from
benchmark results. It does not override the hard tier floors or the
max-dimension routing rules in the active policy. When a task clearly maps to a
tier by those rules, follow the rules; use the table only to break ties or
calibrate confidence.
