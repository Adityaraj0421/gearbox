---
name: doctor
description: "Self-check the Gearbox installation and print a PASS/WARN/FAIL report"
---

Run the following checks IN ORDER. Execute each step via Bash (or by
introspection where noted). Do NOT print results as you go — collect each
result silently, then print only the final table at the end.

For each check, record one of: **PASS**, **WARN**, or **FAIL**, plus a brief
evidence/fix string.

---

## CHECK 0 — PLUGIN ROOT

```bash
echo "${CLAUDE_PLUGIN_ROOT}"
```

- Non-empty output AND the path exists on disk → **PASS** (record the path)
- Empty or path does not exist → **FAIL**: "plugin variable not resolving — reinstall the plugin"

---

## CHECK 1 — DEPENDENCIES

```bash
command -v python3 && echo "python3 ok" || echo "python3 missing"
command -v git && echo "git ok" || echo "git missing"
```

- Both present → **PASS**
- python3 missing → **FAIL**: "Gearbox hooks are python3 scripts; install python3 — both hooks are currently failing silently"
- git missing only → **WARN**: "verifier baseline checks need git"

---

## CHECK 2 — POLICY INJECTION

Introspect without reading any files: is the Gearbox tier table (T0/T1/T2
with gearbox:scout / gearbox:grunt / gearbox:builder / gearbox:architect)
present in your context right now?

- Yes → **PASS**
- No → **FAIL**: "SessionStart hook not firing — check /plugin detail view for hook load errors, then restart the session"

---

## CHECK 3 — AGENT REGISTRY

Introspect your available subagent types. Check for all five:
`gearbox:scout`, `gearbox:grunt`, `gearbox:builder`, `gearbox:architect`,
`gearbox:verifier`.

- 5/5 present → **PASS**
- Fewer than 5 → **FAIL** listing the missing names: "restart the session; agents only load at session start"

---

## CHECK 4 — INSTALL SCOPE

```bash
python3 - <<'EOF'
import json, pathlib, sys
p = pathlib.Path.home() / ".claude/plugins/installed_plugins.json"
if not p.exists():
    print(f"MISSING:{p}")
    sys.exit(0)
data = json.loads(p.read_text())
entries = []
for key, installs in data.get("plugins", {}).items():
    if "gearbox" in key.lower():
        entries.extend(installs)
if not entries:
    print("NOT_FOUND")
    sys.exit(0)
scopes = [e.get("scope","?") for e in entries]
print("SCOPES:" + ",".join(scopes))
EOF
```

- Output contains "user" → **PASS**
- Output contains only "project" or "local" (no "user") → **WARN**: "Gearbox only routes in one folder — reinstall and choose user scope at the prompt"
- Output is `MISSING:...` or `NOT_FOUND` → **WARN** with the path checked: "installed_plugins.json not found or no gearbox entry"

---

## CHECK 5 — LOG WRITABILITY

```bash
mkdir -p .claude && touch .claude/.gearbox-doctor-test && rm .claude/.gearbox-doctor-test && echo "ok" || echo "fail"
```

- Output "ok" → **PASS**
- Any error → **FAIL**: "cannot write to .claude/ — check directory permissions"

---

## CHECK 6 — LIVE DISPATCH + TELEMETRY

This is the only token-spending check.

**Step A** — note the current line count of `.claude/gearbox-log.jsonl`:

```bash
python3 -c "
import pathlib
p = pathlib.Path('.claude/gearbox-log.jsonl')
print(sum(1 for _ in p.open()) if p.exists() else 0)
"
```

Record this as BEFORE_COUNT.

**Step B** — delegate to `gearbox:scout` (model: haiku) with exactly this
prompt: `Reply with exactly: GEARBOX DOCTOR OK. Use no tools.`

**Step C** — re-read the log and count lines again (AFTER_COUNT).

```bash
python3 -c "
import json, pathlib
p = pathlib.Path('.claude/gearbox-log.jsonl')
if not p.exists():
    print('NO_LOG')
    exit()
lines = [json.loads(l) for l in p if l.strip()]
print(len(lines))
if lines:
    last = lines[-1]
    print('tool_name=' + repr(last.get('tool_name','')))
    print('subagent_type=' + repr(last.get('subagent_type','')))
    print('model=' + repr(last.get('model','')))
"
```

Evaluate:
- AFTER_COUNT > BEFORE_COUNT, last entry has non-empty `tool_name`, `subagent_type` contains "scout", `model` is "haiku" → **PASS**
- AFTER_COUNT > BEFORE_COUNT but fields wrong → **WARN**: "dispatch worked but log fields unexpected — check hook schema against your Claude Code version"
- AFTER_COUNT == BEFORE_COUNT (no new line) → **FAIL**: "PostToolUse hook not matching — check /plugin detail view for hook errors; if your Claude Code names the dispatch tool something other than Task or Agent, file an issue with this report"
- Dispatch threw an error → **FAIL** quoting the error verbatim

---

## CHECK 7 — CONFLICTING LEGACY INSTALL

```bash
# Check for pre-plugin agent files
test -f ".claude/agents/scout.md" && echo "AGENTS_DIR_FOUND" || echo "agents_dir_clean"
# Check for @.claude/routing.md reference in CLAUDE.md
grep -l "@.claude/routing.md" CLAUDE.md 2>/dev/null && echo "CLAUDE_MD_FOUND" || echo "claude_md_clean"
```

- Neither found → **PASS**
- Either found → **WARN**: "pre-plugin Gearbox files detected in this repo — project agents shadow plugin agents and the policy may load twice; remove the old copies"
  - If `.claude/agents/scout.md` exists, note it
  - If `CLAUDE.md` contains the reference, note that too

---

## CHECK 8 — VERSION FRESHNESS

**Step A** — read installed version:

```bash
python3 -c "
import json, os, pathlib
root = os.environ.get('CLAUDE_PLUGIN_ROOT','')
if not root:
    print('NO_PLUGIN_ROOT')
    exit()
p = pathlib.Path(root) / '.claude-plugin' / 'plugin.json'
if not p.exists():
    print('NO_PLUGIN_JSON')
    exit()
d = json.loads(p.read_text())
print(d.get('version','unknown'))
"
```

**Step B** — fetch latest from GitHub (5-second timeout; skip on failure):

```bash
curl -s --max-time 5 "https://raw.githubusercontent.com/Adityaraj0421/gearbox/main/.claude-plugin/plugin.json" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('version','unknown'))" \
  2>/dev/null || echo "NETWORK_FAIL"
```

Evaluate:
- Network failed or timed out → **SKIP** (never FAIL on offline)
- Installed == latest → **PASS**
- Installed < latest → **WARN** with both versions: "update with: `/plugin install gearbox@gearbox` then restart"

---

## CHECK 9 — OUTCOME SCHEMA (0.2.0)

Confirms the 0.2.0 outcome-logging hook is actually live by checking that the
newest delegation line carries the new fields.

```bash
python3 -c "
import json, pathlib
p = pathlib.Path('.claude/gearbox-log.jsonl')
if not p.exists():
    print('NO_LOG'); raise SystemExit
delegs = []
for line in p.open():
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    if 'event' not in r:          # skip verdict/escalation event records
        delegs.append(r)
if not delegs:
    print('NO_DELEGATIONS'); raise SystemExit
last = delegs[-1]
print('HAS_NEW' if ('fallback' in last and 'is_named_tier' in last) else 'OLD_SCHEMA')
"
```

- `HAS_NEW` → **PASS**: "0.2.0 outcome logging active (fallback / is_named_tier present)"
- `OLD_SCHEMA` → **WARN**: "newest delegation predates 0.2.0 — restart the session so the updated PostToolUse hook loads, then run one delegation"
- `NO_LOG` or `NO_DELEGATIONS` → **WARN**: "no delegations logged yet — run one delegation, then re-check"

---

## FINAL OUTPUT

After completing all checks, print this table and nothing else before it:

```
Gearbox doctor report
─────────────────────────────────────────────────────────────────────────
 #  | Check                    | Result | Evidence / fix
────|──────────────────────────|────────|──────────────────────────────────
 0  | Plugin root              | ...    | ...
 1  | Dependencies             | ...    | ...
 2  | Policy injection         | ...    | ...
 3  | Agent registry           | ...    | ...
 4  | Install scope            | ...    | ...
 5  | Log writability          | ...    | ...
 6  | Live dispatch+telemetry  | ...    | ...
 7  | Legacy install conflict  | ...    | ...
 8  | Version freshness        | ...    | ...
 9  | Outcome schema (0.2.0)   | ...    | ...
─────────────────────────────────────────────────────────────────────────
```

Then on the next line, print exactly one of:
- `Gearbox healthy` — if there are zero FAILs
- `N issue(s) found — fixes above. If filing a GitHub issue, paste this entire table.` — where N is the count of FAILs
