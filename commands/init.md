# /gearbox:init

Creates a project-local copy of the Gearbox routing policy. Optional — only
needed if you want to customize the policy for this repo.

## What it does

Copies the Gearbox routing policy to `.claude/routing.md` in this project.
The SessionStart hook will then inject this local copy instead of the plugin
default, so your edits take effect on every session restart.

Skips the copy with a notice if `.claude/routing.md` already exists.

## Instructions

Execute the following steps exactly, using Bash.

**Step 1 — ensure .claude/ directory exists:**

```bash
mkdir -p .claude
```

**Step 2 — copy routing policy (skip if already present):**

```bash
if [ -f ".claude/routing.md" ]; then
  echo "Notice: .claude/routing.md already exists — skipping copy. Edit it directly to customize."
else
  cp "${CLAUDE_PLUGIN_ROOT}/routing/routing.md" ".claude/routing.md"
  echo "Local routing override created. The SessionStart hook will now inject this copy instead of the plugin default. Restart the session."
fi
```
