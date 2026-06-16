#!/usr/bin/env python3
"""Gearbox budget warning emitter.

PostToolUse hook for Task/Agent. Runs AFTER log-routing.py so the just-completed
dispatch row is already in the log. Emits two kinds of user-visible warnings:

  1. Threshold warnings: when the session crosses 80% or 100% of the session cap
     (each band warned once per session via a small state file).

  2. Per-task warnings: when a single dispatch exceeds the configured task_cap
     (post-hoc; cannot block after the fact).

Fail-open: any exception → no output (never block the session on warning failure).
No active cap → silent no-op.
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
        return  # fail-open

    try:
        session_id = event.get("session_id", "")
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()

        cfg = bc.resolve_budget_config(os.environ, cwd)
        if not bc.is_active(cfg):
            return  # no caps configured → silent no-op

        rows = bc.read_rows()
        so_far = bc.session_total(rows, session_id, cfg["unit"])

        task_warning = None
        threshold_warning = None

        # --- Per-task warning ---
        # ponytail: "last matching row" is best-effort — parallel dispatches landing
        # simultaneously mean the "last" row in the file may not be this exact call.
        # Acceptable for a post-hoc warning; not used for enforcement.
        if cfg["task_cap"] is not None:
            last_row = None
            for row in rows:
                if row.get("session_id") == session_id:
                    last_row = row
            if last_row is not None:
                task_val = bc.row_value(last_row, cfg["unit"])
                if task_val is not None and task_val > cfg["task_cap"]:
                    task_warning = (
                        f"⚠ Gearbox: last dispatch used {bc.fmt(task_val, cfg['unit'])}, "
                        f"over your per-task cap of {bc.fmt(cfg['task_cap'], cfg['unit'])}."
                    )

        # --- Threshold warning ---
        if cfg["session_cap"] is not None:
            from pathlib import Path
            state_path = Path(cwd) / ".claude" / "gearbox-budget-state.json"
            state = {}
            try:
                if state_path.exists():
                    with open(state_path, encoding="utf-8") as f:
                        state = json.load(f)
            except Exception:
                state = {}

            last_band = state.get(session_id, 0)
            band = bc.band_to_warn(so_far, cfg["session_cap"], last_band)
            if band is not None:
                pct = int(so_far / cfg["session_cap"] * 100)
                threshold_warning = (
                    f"⚠ Gearbox budget: {pct}% of session cap — "
                    f"{bc.fmt(so_far, cfg['unit'])} of {bc.fmt(cfg['session_cap'], cfg['unit'])}. "
                    f"Consider GEARBOX_PROFILE=cost-conscious or wrapping up."
                )
                state[session_id] = band
                try:
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(state, f)
                except Exception:
                    pass  # fail-open on state write

        if task_warning or threshold_warning:
            parts = [w for w in [threshold_warning, task_warning] if w]
            combined = " ".join(parts)
            short = combined[:200]  # abbreviated for additionalContext
            print(json.dumps({
                "systemMessage": combined,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": short,
                },
            }))

    except Exception:
        pass  # fail-open: no warning is better than a broken session


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        import io
        import tempfile
        import json as _json
        from pathlib import Path

        _orig_read_rows = bc.read_rows

        with tempfile.TemporaryDirectory() as tmpdir:
            budget_dir = Path(tmpdir) / ".claude"
            budget_dir.mkdir()
            budget_file = budget_dir / "gearbox-budget.json"
            state_file = budget_dir / "gearbox-budget-state.json"

            # --- Case 1: crossing 80% threshold emits a systemMessage ---
            # session cap = 100 wtok; so_far after this dispatch = ~85 wtok
            # Means cost_usd rows totaling 0.000085 USD for session "sc-sess"
            # (wtok = cost_usd * 1e6 / 1.00 → 0.000085 * 1e6 = 85 wtok)
            budget_file.write_text(_json.dumps({"session_cap": 100, "unit": "wtok"}))

            rows_80 = [
                {"session_id": "sc-sess", "cost_usd": 0.000085, "total_tokens": 85},
            ]

            def _fake_rows_80():
                return rows_80

            bc.read_rows = _fake_rows_80

            _orig_stdout = sys.stdout
            captured = io.StringIO()
            sys.stdout = captured
            sys.stdin = io.StringIO(_json.dumps({"session_id": "sc-sess", "cwd": tmpdir}))
            main()
            sys.stdout = _orig_stdout
            out = captured.getvalue().strip()
            assert out, f"80% crossing must emit output, got empty"
            parsed = _json.loads(out)
            assert "systemMessage" in parsed, f"must have systemMessage, got {parsed}"
            assert "80%" in parsed["systemMessage"] or "85%" in parsed["systemMessage"], \
                f"systemMessage must mention percentage: {parsed['systemMessage']!r}"

            # State file should now record band=80 for this session
            assert state_file.exists(), "state file must be created after threshold warning"
            state = _json.loads(state_file.read_text())
            assert state.get("sc-sess") == 80, \
                f"state must record band=80 for sc-sess, got {state}"

            # --- Case 2: same session, same data → no repeat warning (band already 80) ---
            captured2 = io.StringIO()
            sys.stdout = captured2
            sys.stdin = io.StringIO(_json.dumps({"session_id": "sc-sess", "cwd": tmpdir}))
            main()
            sys.stdout = _orig_stdout
            out2 = captured2.getvalue().strip()
            assert not out2, \
                f"second run at same band must emit nothing, got {out2!r}"

            # --- Case 3: per-task overage emits warning ---
            # task_cap = 50 wtok; last dispatch used 85 wtok → overage
            budget_file.write_text(_json.dumps({"task_cap": 50, "unit": "wtok"}))

            captured3 = io.StringIO()
            sys.stdout = captured3
            sys.stdin = io.StringIO(_json.dumps({"session_id": "sc-sess", "cwd": tmpdir}))
            main()
            sys.stdout = _orig_stdout
            out3 = captured3.getvalue().strip()
            assert out3, f"per-task overage must emit output, got empty"
            parsed3 = _json.loads(out3)
            assert "systemMessage" in parsed3, f"must have systemMessage, got {parsed3}"
            assert "per-task cap" in parsed3["systemMessage"], \
                f"systemMessage must mention per-task cap: {parsed3['systemMessage']!r}"

        bc.read_rows = _orig_read_rows

        print("budget-warn selfcheck: OK")
        sys.exit(0)

    main()
