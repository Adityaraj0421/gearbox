#!/usr/bin/env python3
"""Gearbox session-start context injector.

SessionStart hook. Injects the gearbox routing policy into every session's
context window so the orchestrator has the tier table and routing rules
available automatically, without requiring project-level CLAUDE.md changes.

If the project has a .claude/routing.md (placed by /gearbox:init), that file
takes precedence — it may be a customised local copy. Falls back to the plugin
copy.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ponytail: discard routing-prior artifact if older than this many days
FRESH_DAYS = 30

_PRIOR_PATH = os.path.expanduser("~/.claude/gearbox-recommendations.md")

_KNOWN_PROFILES = {
    "balanced",
    "cost-conscious",
    "quality-first",
    "always-t0",
    "always-t1",
    "always-t2",
    "always-opus-build",
}


def resolve_profile(env: dict, cwd: str) -> str:
    raw = env.get("GEARBOX_PROFILE")
    if raw is None:
        profile_file = Path(cwd) / ".claude" / "gearbox-profile"
        try:
            raw = profile_file.read_text(encoding="utf-8")
        except Exception:
            return "balanced"
    name = raw.strip().lower()
    if name not in _KNOWN_PROFILES:
        return "balanced"
    return name


def profile_block(name: str) -> str:
    if name == "balanced":
        return ""

    if name == "cost-conscious":
        return (
            "\n\n---\n\n"
            "## Active routing profile: cost-conscious\n\n"
            "Shift the tier thresholds DOWN one notch (bias cheaper). Route on the maximum across the three dimensions — "
            "**max 1-3 → T0, max 4 → T1, max 5 → T2**. When a task sits on a threshold boundary, pick the cheaper tier; "
            "the verifier loop catches a misroute and the escalation ladder recovers it. The **hard floors are UNCHANGED** — "
            "auth / payments / migrations / concurrency / secrets still start at T1, production-breaking risk at T2 — "
            "and the circuit breaker is unchanged."
        )

    if name == "quality-first":
        return (
            "\n\n---\n\n"
            "## Active routing profile: quality-first\n\n"
            "Shift the tier thresholds UP one notch (bias stronger). Route on the maximum across the three dimensions — "
            "**max 1 → T0, max 2-3 → T1, max 4-5 → T2**. When a task sits on a threshold boundary, pick the stronger tier. "
            "The hard floors and the circuit breaker are unchanged."
        )

    tier_map = {
        "always-t0": (0, "gearbox:scout / gearbox:grunt", "haiku"),
        "always-t1": (1, "gearbox:builder", "sonnet"),
        "always-t2": (2, "gearbox:architect", "opus"),
        # Benchmark-only: an edit-capable opus baseline. always-t2 routes to the
        # read-only architect (can't complete editing tasks under a forced
        # profile), so the measured "always-Opus" policy uses the builder on opus.
        "always-opus-build": (2, "gearbox:builder", "opus"),
    }
    if name in tier_map:
        n, agent, model = tier_map[name]
        return (
            "\n\n---\n\n"
            f"## BENCHMARK MODE: always-T{n}\n\n"
            f"Route EVERY task to **Tier {n}** ({agent}, model `{model}`) regardless of the classification score. "
            "This OVERRIDES both the max-dimension classification rule AND the hard floors. "
            "For benchmark baseline measurement only — never use this profile for real work."
        )

    return ""


def main() -> None:
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    # Prefer a project-local copy (placed by /gearbox:init), then plugin copy.
    candidates = [
        Path(cwd) / ".claude" / "routing.md",
        Path(plugin_root) / "routing" / "routing.md",
    ]
    routing_file = next((p for p in candidates if p.exists()), None)

    if routing_file is None:
        return  # never block session startup

    try:
        content = routing_file.read_text(encoding="utf-8")

        content = content + profile_block(resolve_profile(os.environ, cwd))

        # Append routing-prior artifact if it exists and is fresh.
        try:
            prior_path = Path(_PRIOR_PATH)
            age_seconds = time.time() - prior_path.stat().st_mtime
            if age_seconds <= FRESH_DAYS * 86400:
                prior_text = prior_path.read_text(encoding="utf-8")
                content = content + "\n" + prior_text
        except Exception:
            pass  # silently fall back to policy-only

        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": content,
            }
        }
        print(json.dumps(output))
    except Exception:
        pass  # never block session startup


def _selfcheck() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        dot_claude = tmp_path / ".claude"
        dot_claude.mkdir()
        profile_file = dot_claude / "gearbox-profile"
        profile_file.write_text("cost-conscious\n", encoding="utf-8")

        # env var beats file
        assert resolve_profile({"GEARBOX_PROFILE": "quality-first"}, tmp) == "quality-first"

        # file used when no env var
        assert resolve_profile({}, tmp) == "cost-conscious"

    with tempfile.TemporaryDirectory() as tmp:
        # default when neither
        assert resolve_profile({}, tmp) == "balanced"

        # unknown → balanced
        assert resolve_profile({"GEARBOX_PROFILE": "bogus"}, tmp) == "balanced"

        # case/whitespace normalization
        assert resolve_profile({"GEARBOX_PROFILE": "  Always-T2 "}, tmp) == "always-t2"

    # balanced produces empty string
    assert profile_block("balanced") == ""

    # cost-conscious content
    cc = profile_block("cost-conscious")
    assert "max 1-3 → T0" in cc

    # quality-first content
    qf = profile_block("quality-first")
    assert "max 2-3 → T1" in qf

    # benchmark mode blocks
    t1 = profile_block("always-t1")
    assert "BENCHMARK MODE: always-T1" in t1
    assert "`sonnet`" in t1

    t2 = profile_block("always-t2")
    assert "BENCHMARK MODE: always-T2" in t2
    assert "opus" in t2

    t0 = profile_block("always-t0")
    assert "BENCHMARK MODE: always-T0" in t0
    assert "haiku" in t0

    ob = profile_block("always-opus-build")
    assert "BENCHMARK MODE: always-T2" in ob
    assert "gearbox:builder" in ob and "opus" in ob

    print("inject-routing selfcheck: OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
        sys.exit(0)
    main()
