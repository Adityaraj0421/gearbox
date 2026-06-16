#!/usr/bin/env python3
"""Gearbox budget enforcer.

PreToolUse hook for Task/Agent. Enforces the SESSION cap only — if the current
session has already consumed >= the configured cap, it returns a permissionDecision
of "ask" so the user can approve or decline each overrun dispatch.

Per-task ceiling is a post-hoc warning handled by budget-warn.py, not this hook.

Fail-open: any exception → allow the dispatch silently (no output, no block).
No cap configured → silent no-op.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import budget_common as bc


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return  # fail-open: can't parse event → allow

    try:
        session_id = event.get("session_id", "")
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()

        cfg = bc.resolve_budget_config(os.environ, cwd)
        if cfg["session_cap"] is None:
            return  # no session cap configured → no-op

        if not session_id:
            return  # can't sum without session_id; fail-open

        so_far = bc.session_total(bc.read_rows(), session_id, cfg["unit"])
        cap = cfg["session_cap"]

        if so_far >= cap:
            reason = (
                f"Gearbox budget: this session has used {bc.fmt(so_far, cfg['unit'])} "
                f"of your {bc.fmt(cap, cfg['unit'])} cap. "
                f"Approve to allow this dispatch, or decline and down-tier "
                f"(set GEARBOX_PROFILE=cost-conscious) or wrap up."
            )
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }))
    except Exception:
        pass  # fail-open: any error → allow silently


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        import io
        import tempfile
        import json as _json

        # Patch bc.read_rows to return a controlled rows list
        _orig_read_rows = bc.read_rows

        def _fake_rows():
            return [
                {"session_id": "sess-over", "cost_usd": 0.5, "total_tokens": 500000},
                {"session_id": "sess-under", "cost_usd": 0.001, "total_tokens": 1000},
            ]

        bc.read_rows = _fake_rows

        with tempfile.TemporaryDirectory() as tmpdir:
            import os as _os
            from pathlib import Path

            budget_dir = Path(tmpdir) / ".claude"
            budget_dir.mkdir()
            budget_file = budget_dir / "gearbox-budget.json"

            # --- Case 1: cap exceeded → should emit "ask" ---
            # sess-over has cost_usd=0.5; cap=0.1 USD → exceeded
            budget_file.write_text(_json.dumps({"session_cap": 0.1, "unit": "usd"}))

            event_over = _json.dumps({"session_id": "sess-over", "cwd": tmpdir})
            captured = io.StringIO()
            _orig_stdout = sys.stdout
            sys.stdout = captured
            sys.stdin = io.StringIO(event_over)
            main()
            sys.stdout = _orig_stdout
            out = captured.getvalue().strip()
            assert out, f"cap exceeded must emit output, got empty"
            parsed = _json.loads(out)
            decision = parsed["hookSpecificOutput"]["permissionDecision"]
            assert decision == "ask", f"expected 'ask', got {decision!r}"

            # --- Case 2: no cap configured → no output ---
            budget_file.write_text(_json.dumps({}))
            captured2 = io.StringIO()
            sys.stdout = captured2
            sys.stdin = io.StringIO(event_over)
            main()
            sys.stdout = _orig_stdout
            out2 = captured2.getvalue().strip()
            assert not out2, f"no cap must emit nothing, got {out2!r}"

            # --- Case 3: cap not yet exceeded → no output ---
            # sess-under has cost_usd=0.001; cap=0.1 USD → not exceeded
            budget_file.write_text(_json.dumps({"session_cap": 0.1, "unit": "usd"}))
            event_under = _json.dumps({"session_id": "sess-under", "cwd": tmpdir})
            captured3 = io.StringIO()
            sys.stdout = captured3
            sys.stdin = io.StringIO(event_under)
            main()
            sys.stdout = _orig_stdout
            out3 = captured3.getvalue().strip()
            assert not out3, f"under-cap must emit nothing, got {out3!r}"

        bc.read_rows = _orig_read_rows

        print("enforce-budget selfcheck: OK")
        sys.exit(0)

    main()
