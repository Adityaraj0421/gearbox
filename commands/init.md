# /gearbox:init

Sets up Gearbox routing in the current project. Run once after installing the plugin.

## What it does

1. Copies the Gearbox routing policy to `.claude/routing.md` in this project.
2. Ensures `.claude/routing.md` is imported at the top of `CLAUDE.md` (creates `CLAUDE.md` if missing).
3. Prints a summary of what changed.

## Instructions

Execute the following steps exactly, using Bash. Do not skip any step.

**Step 1 — ensure .claude/ directory exists:**

```bash
mkdir -p .claude
```

**Step 2 — copy routing policy (idempotent):**

```bash
cp "${CLAUDE_PLUGIN_ROOT}/routing/routing.md" ".claude/routing.md"
echo "Copied routing policy to .claude/routing.md"
```

**Step 3 — idempotently prepend the import to CLAUDE.md:**

Check whether `@.claude/routing.md` is already the first line of `CLAUDE.md`.
If it is, do nothing and print "CLAUDE.md already imports routing.md — no change."
If it is not (whether CLAUDE.md exists or not), prepend it:

```bash
# Read current content (empty string if file doesn't exist)
EXISTING=$(cat CLAUDE.md 2>/dev/null || echo "")

# Check if first line is already the import
FIRST_LINE=$(head -1 CLAUDE.md 2>/dev/null || echo "")

if [ "$FIRST_LINE" = "@.claude/routing.md" ]; then
  echo "CLAUDE.md already imports routing.md — no change."
else
  printf '@.claude/routing.md\n%s' "$EXISTING" > CLAUDE.md
  echo "Prepended @.claude/routing.md to CLAUDE.md"
fi
```

**Step 4 — confirm:**

```bash
echo "--- .claude/routing.md head ---"
head -5 .claude/routing.md
echo "--- CLAUDE.md head ---"
head -3 CLAUDE.md
```

After all steps complete, print: "Gearbox initialized. Restart your Claude Code session to activate routing."
