#!/usr/bin/env python3
"""Gearbox baseline capture.

PreToolUse hook for the Task/Agent tool. Fires before a T1/T2 implementer
(builder/architect) is dispatched and writes a git status snapshot to
.claude/gearbox-baseline.txt in the project directory. The verifier reads
this file as the pre-edit BASELINE to diff the working tree against.

Silent allow: this hook never prints a permission decision or blocks dispatch.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def should_capture(tool_input: dict) -> bool:
    """Return True if this dispatch warrants a baseline capture.

    Fires for builder/architect (T1/T2 implementers) and for the general-purpose
    fallback proxy when paired with a T1/T2 model (sonnet/opus). Never fires for
    verifier dispatches — that would clobber the baseline we're protecting.
    """
    subagent_type = str(tool_input.get("subagent_type", "")).lower()
    model = str(tool_input.get("model", "")).lower()

    if "verifier" in subagent_type:
        return False  # never clobber the baseline on a verifier dispatch

    if "builder" in subagent_type or "architect" in subagent_type:
        return True

    if model in {"sonnet", "opus"}:
        return True  # covers the fallback proxy (general-purpose/claude + explicit T1/T2 model)

    return False  # scout/grunt/haiku/anything else


def resolve_base_dir(payload: dict) -> Path:
    """Resolve the project root directory.

    Priority: CLAUDE_PROJECT_DIR env (if valid dir) > payload cwd (if valid dir) > cwd.
    Mirrors log-routing.py's resolution pattern.
    """
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if env_dir and os.path.isdir(env_dir):
        return Path(env_dir)
    cwd_field = payload.get("cwd", "")
    if cwd_field and os.path.isdir(cwd_field):
        return Path(cwd_field)
    return Path(os.getcwd())


def git_status(root: Path) -> str:
    """Run `git -C <root> status --short` and return its stdout.

    Returns a single-line error message if git is missing, errors, or the
    directory is not a git repository.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return "(baseline unavailable: not a git repository)"
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "(baseline unavailable: not a git repository)"


def build_body(subagent_type: str, status_output: str) -> str:
    """Build the full baseline file contents."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    header = f"# gearbox baseline | {ts} | subagent_type={subagent_type}"
    return header + "\n\n" + status_output


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never block on parse failure

    tool_input = payload.get("tool_input", {}) or {}

    if not should_capture(tool_input):
        sys.exit(0)

    try:
        root = resolve_base_dir(payload)
        status_output = git_status(root)
        subagent_type = str(tool_input.get("subagent_type", ""))

        body = build_body(subagent_type, status_output)

        baseline_path = root / ".claude" / "gearbox-baseline.txt"
        # ponytail: single baseline file; interleaved parallel T1/T2 dispatches can race — key by session/dispatch id if parallel verification becomes common
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(body, encoding="utf-8")
    except Exception:
        pass  # robustness: never block or delay a Task dispatch

    sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        # --- should_capture: named agent cases ---
        assert should_capture({"subagent_type": "gearbox:builder"}) is True, \
            "gearbox:builder must capture"
        assert should_capture({"subagent_type": "gearbox:architect"}) is True, \
            "gearbox:architect must capture"
        assert should_capture({"subagent_type": "gearbox:verifier"}) is False, \
            "gearbox:verifier must NOT capture"
        assert should_capture({"subagent_type": "gearbox:verifier", "model": "opus"}) is False, \
            "gearbox:verifier with opus must NOT capture"
        assert should_capture({"subagent_type": "gearbox:scout"}) is False, \
            "gearbox:scout must NOT capture"
        assert should_capture({"subagent_type": "gearbox:grunt"}) is False, \
            "gearbox:grunt must NOT capture"

        # --- should_capture: general-purpose fallback proxy ---
        assert should_capture({"subagent_type": "general-purpose", "model": "sonnet"}) is True, \
            "general-purpose+sonnet must capture (T1 fallback proxy)"
        assert should_capture({"subagent_type": "general-purpose", "model": "haiku"}) is False, \
            "general-purpose+haiku must NOT capture (T0 fallback proxy)"

        # --- should_capture: empty/missing fields ---
        assert should_capture({}) is False, "empty tool_input must NOT capture"
        assert should_capture({"subagent_type": "", "model": ""}) is False, \
            "empty strings must NOT capture"

        # --- build_body: file format ---
        sample_status = " M hooks/hooks.json\n?? hooks/scripts/capture-baseline.py\n"
        body = build_body("gearbox:builder", sample_status)
        lines = body.split("\n")
        assert lines[0].startswith("# gearbox baseline | "), \
            f"header must start with '# gearbox baseline | ': {lines[0]!r}"
        assert "subagent_type=gearbox:builder" in lines[0], \
            f"header must include subagent_type: {lines[0]!r}"
        assert lines[1] == "", f"second line must be blank: {lines[1]!r}"
        assert sample_status in body, "git status output must appear verbatim in body"

        # --- resolve_base_dir: falls back to cwd when env unset ---
        import os as _os
        orig_env = _os.environ.pop("CLAUDE_PROJECT_DIR", None)
        try:
            result_path = resolve_base_dir({"cwd": "/tmp"})
            assert result_path == Path("/tmp"), \
                f"payload cwd='/tmp' must resolve to /tmp, got {result_path}"
            result_no_cwd = resolve_base_dir({})
            assert result_no_cwd == Path(_os.getcwd()), \
                f"empty payload must fall back to os.getcwd(), got {result_no_cwd}"
        finally:
            if orig_env is not None:
                _os.environ["CLAUDE_PROJECT_DIR"] = orig_env

        print("selfcheck OK")
        sys.exit(0)

    main()
