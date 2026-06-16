#!/usr/bin/env python3
"""Gearbox budget: shared helpers.

Pure functions for weighted-token math, log reading, budget config resolution,
and threshold-band tracking. Imported by enforce-budget.py and budget-warn.py.
Stdlib only; all callers are fail-open (catch any exception at their boundary).
"""
import json
import os
import sys
from pathlib import Path

# Unit reference: mirrors log-routing.py _TOKEN_RATES["haiku"]["input"] == 1.00
# (USD per million tokens). Used as the denominator in weighted_tokens so that a
# pure-input Haiku dispatch of N tokens equals N weighted tokens. Re-pin together
# with log-routing.py whenever pricing changes. Treated as a tunable proxy for the
# unpublished per-model subscription weighting.
HAIKU_REF = 1.00


def weighted_tokens(cost_usd) -> "float | None":
    """Convert a cost_usd amount to weighted tokens (Haiku-equivalent).

    Returns None if cost_usd is None; otherwise rounds to 3 decimal places.
    A Haiku dispatch at input rate costs 1.00 USD/M, so N haiku input tokens
    → N weighted tokens. Opus at 5.00 USD/M → ~5× weighted tokens.
    """
    if cost_usd is None:
        return None
    return round(cost_usd * 1e6 / HAIKU_REF, 3)


def row_value(row: dict, unit: str) -> "float | None":
    """Extract the budget-relevant value from a log row for the given unit.

    unit: "wtok" → weighted_tokens(cost_usd)
          "tok"  → total_tokens (raw int or None)
          "usd"  → cost_usd
          anything else → treated as "wtok"
    """
    if unit == "tok":
        v = row.get("total_tokens")
        return float(v) if v is not None else None
    if unit == "usd":
        v = row.get("cost_usd")
        return float(v) if v is not None else None
    # "wtok" or any unknown unit
    return weighted_tokens(row.get("cost_usd"))


def session_total(rows: list, session_id: str, unit: str) -> float:
    """Sum row_value over all rows matching session_id, skipping None values.

    Pure function. Returns 0.0 if no matching non-None rows exist.
    """
    total = 0.0
    for row in rows:
        if row.get("session_id") != session_id:
            continue
        v = row_value(row, unit)
        if v is not None:
            total += v
    return total


def default_log_path() -> Path:
    return Path.home() / ".claude" / "gearbox-log.jsonl"


def read_rows(log_path=None) -> list:
    """Read the JSONL log and return a list of dicts. Fail-open: returns [] on any error."""
    path = log_path if log_path is not None else default_log_path()
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def resolve_budget_config(env: dict, cwd: str) -> dict:
    """Resolve budget caps and unit from project file + env overrides.

    Resolution order (later wins per-key):
      1. Defaults: session_cap=None, task_cap=None, unit="wtok"
      2. Project file: <cwd>/.claude/gearbox-budget.json
      3. Env vars: GEARBOX_SESSION_CAP, GEARBOX_TASK_CAP, GEARBOX_BUDGET_UNIT

    All failures are fail-open: bad parse / missing keys → keep prior value.
    Unit is normalized to one of {"wtok","tok","usd"}; anything else → "wtok".
    Returns dict with keys: session_cap (float|None), task_cap (float|None), unit (str).
    """
    session_cap = None
    task_cap = None
    unit = "wtok"

    # 1. Project file
    try:
        cfg_path = Path(cwd) / ".claude" / "gearbox-budget.json"
        if cfg_path.exists():
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            raw_sc = data.get("session_cap")
            if isinstance(raw_sc, (int, float)) and not isinstance(raw_sc, bool):
                session_cap = float(raw_sc)
            raw_tc = data.get("task_cap")
            if isinstance(raw_tc, (int, float)) and not isinstance(raw_tc, bool):
                task_cap = float(raw_tc)
            raw_unit = data.get("unit")
            if isinstance(raw_unit, str):
                unit = raw_unit
    except Exception:
        pass

    # 2. Env overrides
    raw_env_sc = env.get("GEARBOX_SESSION_CAP")
    if raw_env_sc is not None:
        try:
            session_cap = float(raw_env_sc)
        except (TypeError, ValueError):
            pass  # unparseable → keep file value

    raw_env_tc = env.get("GEARBOX_TASK_CAP")
    if raw_env_tc is not None:
        try:
            task_cap = float(raw_env_tc)
        except (TypeError, ValueError):
            pass  # unparseable → keep file value

    raw_env_unit = env.get("GEARBOX_BUDGET_UNIT")
    if raw_env_unit is not None:
        unit = str(raw_env_unit)

    # Normalize unit
    if unit not in {"wtok", "tok", "usd"}:
        unit = "wtok"

    return {"session_cap": session_cap, "task_cap": task_cap, "unit": unit}


def is_active(cfg: dict) -> bool:
    """Return True if any cap is configured (budget enforcement is on)."""
    return cfg["session_cap"] is not None or cfg["task_cap"] is not None


def band_to_warn(so_far: float, cap, last_band: int) -> "int | None":
    """Return the threshold band (80 or 100) that was just crossed, or None.

    Bands: 80% and 100% of cap.
    last_band: the highest band already warned (0 if none).
    Returns None if cap is falsy, no band is newly crossed, or already warned.
    """
    if not cap:
        return None
    pct = so_far / cap * 100
    if pct >= 100 and last_band < 100:
        return 100
    if pct >= 80 and last_band < 80:
        return 80
    return None


def fmt(value: float, unit: str) -> str:
    """Human-readable label for a budget value."""
    if unit == "tok":
        return f"{int(value):,} tokens"
    if unit == "usd":
        return f"${value:.4f}"
    # "wtok" or unknown
    return f"{int(value):,} weighted tokens"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        import tempfile

        # --- weighted_tokens ---
        assert weighted_tokens(0.009504) == 9504.0, \
            f"expected 9504.0, got {weighted_tokens(0.009504)}"
        assert weighted_tokens(None) is None, "weighted_tokens(None) must be None"

        # --- row_value ---
        sample_row = {"cost_usd": 0.009504, "total_tokens": 25114}
        assert row_value(sample_row, "wtok") == 9504.0, \
            f"wtok: expected 9504.0, got {row_value(sample_row, 'wtok')}"
        assert row_value(sample_row, "tok") == 25114.0, \
            f"tok: expected 25114.0, got {row_value(sample_row, 'tok')}"
        assert row_value(sample_row, "usd") == 0.009504, \
            f"usd: expected 0.009504, got {row_value(sample_row, 'usd')}"
        # unknown unit → wtok
        assert row_value(sample_row, "unknown") == 9504.0, \
            f"unknown unit must behave as wtok, got {row_value(sample_row, 'unknown')}"

        # --- session_total ---
        rows = [
            {"session_id": "s1", "cost_usd": 0.009504, "total_tokens": 25114},  # matches s1
            {"session_id": "s1", "cost_usd": None,     "total_tokens": 100},    # cost_usd None → wtok skip
            {"session_id": "s2", "cost_usd": 0.001,    "total_tokens": 500},    # different session
            {"session_id": "s1", "cost_usd": 0.001,    "total_tokens": 200},    # matches s1
        ]
        # wtok: s1 rows with non-None cost_usd: 0.009504 → 9504.0, 0.001 → 1000.0 → total 10504.0
        st_wtok = session_total(rows, "s1", "wtok")
        assert abs(st_wtok - 10504.0) < 0.01, f"session_total wtok s1: expected 10504.0, got {st_wtok}"
        # tok: s1 rows: 25114 + 100 + 200 = 25414
        st_tok = session_total(rows, "s1", "tok")
        assert st_tok == 25414.0, f"session_total tok s1: expected 25414.0, got {st_tok}"
        # usd: s1 rows with non-None: 0.009504 + 0.001 = 0.010504
        st_usd = session_total(rows, "s1", "usd")
        assert abs(st_usd - 0.010504) < 1e-9, f"session_total usd s1: expected 0.010504, got {st_usd}"
        # s2 only: 1 row
        st_s2 = session_total(rows, "s2", "wtok")
        assert abs(st_s2 - 1000.0) < 0.01, f"session_total wtok s2: expected 1000.0, got {st_s2}"

        # --- resolve_budget_config: empty env + empty tmpdir → all None, wtok, not active ---
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = resolve_budget_config({}, tmpdir)
            assert cfg["session_cap"] is None, f"no file/env → session_cap must be None, got {cfg['session_cap']}"
            assert cfg["task_cap"] is None, f"no file/env → task_cap must be None, got {cfg['task_cap']}"
            assert cfg["unit"] == "wtok", f"default unit must be wtok, got {cfg['unit']}"
            assert not is_active(cfg), "no caps → is_active must be False"

            # with a budget file
            import os as _os
            budget_dir = Path(tmpdir) / ".claude"
            budget_dir.mkdir()
            budget_file = budget_dir / "gearbox-budget.json"
            budget_file.write_text(json.dumps({"session_cap": 500000, "unit": "tok"}))

            cfg2 = resolve_budget_config({}, tmpdir)
            assert cfg2["session_cap"] == 500000.0, \
                f"file session_cap: expected 500000.0, got {cfg2['session_cap']}"
            assert cfg2["unit"] == "tok", f"file unit: expected tok, got {cfg2['unit']}"
            assert cfg2["task_cap"] is None, "no task_cap in file → None"
            assert is_active(cfg2), "session_cap set → is_active True"

            # env overrides file's session_cap
            cfg3 = resolve_budget_config({"GEARBOX_SESSION_CAP": "250000"}, tmpdir)
            assert cfg3["session_cap"] == 250000.0, \
                f"env override session_cap: expected 250000.0, got {cfg3['session_cap']}"
            assert cfg3["unit"] == "tok", "unit should still come from file"

            # bad unit in file → normalized to wtok
            budget_file.write_text(json.dumps({"session_cap": 100, "unit": "magic"}))
            cfg4 = resolve_budget_config({}, tmpdir)
            assert cfg4["unit"] == "wtok", f"bad unit must normalize to wtok, got {cfg4['unit']}"

            # non-numeric GEARBOX_TASK_CAP → ignored
            cfg5 = resolve_budget_config({"GEARBOX_TASK_CAP": "abc"}, tmpdir)
            assert cfg5["task_cap"] is None, \
                f"non-numeric GEARBOX_TASK_CAP must be ignored, got {cfg5['task_cap']}"

        # --- band_to_warn ---
        assert band_to_warn(81, 100, 0) == 80, f"81/100 from 0 → 80, got {band_to_warn(81,100,0)}"
        assert band_to_warn(81, 100, 80) is None, \
            f"81/100 already at 80 → None, got {band_to_warn(81,100,80)}"
        assert band_to_warn(100, 100, 80) == 100, \
            f"100/100 from 80 → 100, got {band_to_warn(100,100,80)}"
        assert band_to_warn(100, 100, 100) is None, \
            f"100/100 already at 100 → None, got {band_to_warn(100,100,100)}"
        assert band_to_warn(50, 100, 0) is None, \
            f"50/100 from 0 → None, got {band_to_warn(50,100,0)}"
        assert band_to_warn(5, None, 0) is None, \
            f"no cap → None, got {band_to_warn(5,None,0)}"

        print("budget_common selfcheck: OK")
        sys.exit(0)

    # When imported as a module (not run directly), nothing executes here.
