#!/usr/bin/env python3
"""Outcome-labeling runner for Gearbox delegation logs.

Consumes .claude/gearbox-log.jsonl (post-issue-#5 schema: ts, session_id,
tool_name, subagent_type, model, prompt_head, cwd, total_tokens, num_turns,
duration_ms, cost_usd, cost_estimated) and emits labeled training data for
the future learned router (reward = success/cost).
"""
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _record_id(record: dict) -> str:
    """Stable sha1 key from the fields that identify a unique delegation.

    Includes the delegation discriminators (tool, agent, model) so two
    distinct delegations sharing a timestamp + session + prompt_head don't
    collide and silently drop one another during resumable dedup.
    """
    parts = [
        record.get("ts", ""),
        record.get("session_id", ""),
        record.get("tool_name", ""),
        record.get("subagent_type", ""),
        record.get("model", ""),
        record.get("prompt_head", ""),
    ]
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()


def build_training_row(record: dict, acceptable: bool) -> dict:
    """Build a labeled training row from a log record and a human label.

    Pure function — no I/O.
    """
    cost_usd = record.get("cost_usd")
    try:
        cost_float = float(cost_usd)
        if cost_float > 0:
            # ponytail: simplistic success/cost reward; upgrade to graded
            # quality + escalation penalty when those signals exist.
            reward = (1.0 if acceptable else 0.0) / cost_float
        else:
            reward = None
    except (TypeError, ValueError):
        reward = None

    return {
        "id": _record_id(record),
        "subagent_type": record.get("subagent_type"),
        "model": record.get("model"),
        "total_tokens": record.get("total_tokens"),
        "num_turns": record.get("num_turns"),
        "duration_ms": record.get("duration_ms"),
        "cost_usd": record.get("cost_usd"),
        "cost_estimated": record.get("cost_estimated"),
        "prompt_head": record.get("prompt_head"),
        "acceptable": acceptable,
        "reward": reward,
    }


def load_labeled_keys(out_path: Path) -> set:
    """Return the set of ids already present in the output file."""
    keys = set()
    if not out_path.exists():
        return keys
    with out_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if "id" in row:
                    keys.add(row["id"])
            except json.JSONDecodeError:
                pass
    return keys


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------

def _run_selfcheck() -> None:
    """Assert-based tests on pure helpers only. Exits 0 on success."""
    # build_training_row: acceptable=True, cost_usd=0.1 → reward=10.0
    rec = {
        "ts": 1700000000,
        "session_id": "s1",
        "prompt_head": "Do something useful",
        "subagent_type": "builder",
        "model": "claude-sonnet",
        "total_tokens": 5000,
        "num_turns": 3,
        "duration_ms": 4000,
        "cost_usd": 0.1,
        "cost_estimated": False,
    }
    row_true = build_training_row(rec, acceptable=True)
    assert row_true["reward"] == 10.0, f"expected 10.0, got {row_true['reward']}"
    assert row_true["acceptable"] is True
    assert row_true["id"], "id must be non-empty"

    # acceptable=False → reward=0.0
    row_false = build_training_row(rec, acceptable=False)
    assert row_false["reward"] == 0.0, f"expected 0.0, got {row_false['reward']}"
    assert row_false["acceptable"] is False

    # cost_usd=None → reward is None
    rec_no_cost = dict(rec, cost_usd=None)
    row_none = build_training_row(rec_no_cost, acceptable=True)
    assert row_none["reward"] is None, f"expected None, got {row_none['reward']}"

    # id stability: same record → same id
    id1 = _record_id(rec)
    id2 = _record_id(rec)
    assert id1 == id2, "id must be stable"

    # id differs for different prompt_head
    rec_alt = dict(rec, prompt_head="Something completely different")
    assert _record_id(rec) != _record_id(rec_alt), "different prompt_head must differ"

    # id differs when only a delegation discriminator changes (collision guard)
    rec_disc = dict(rec, subagent_type="scout")
    assert _record_id(rec) != _record_id(rec_disc), "different subagent_type must differ"

    # load_labeled_keys round-trip via tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        tmp = Path(f.name)
        row_a = build_training_row(rec, acceptable=True)
        rec_b = dict(rec, ts=1700000001, prompt_head="Another prompt")
        row_b = build_training_row(rec_b, acceptable=False)
        f.write(json.dumps(row_a) + "\n")
        f.write(json.dumps(row_b) + "\n")
    try:
        keys = load_labeled_keys(tmp)
        assert row_a["id"] in keys, "row_a id not found after round-trip"
        assert row_b["id"] in keys, "row_b id not found after round-trip"
    finally:
        tmp.unlink(missing_ok=True)

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Interactive labeling loop
# ---------------------------------------------------------------------------

def _fmt_cost(val) -> str:
    if val is None:
        return "null"
    try:
        return f"${float(val):.6f}"
    except (TypeError, ValueError):
        return str(val)


def _print_summary(record: dict) -> None:
    subagent = record.get("subagent_type") or "(unknown)"
    model = record.get("model") or "(unknown)"
    prompt = (record.get("prompt_head") or "")[:120]
    tokens = record.get("total_tokens")
    turns = record.get("num_turns")
    cost = _fmt_cost(record.get("cost_usd"))
    estimated = record.get("cost_estimated", False)
    cost_str = f"{cost}" + (" [est]" if estimated else "")
    print(f"\n  type={subagent}  model={model}")
    print(f"  tokens={tokens}  turns={turns}  cost={cost_str}")
    print(f"  prompt: {prompt!r}")


def run_labeling(log_path: Path, out_path: Path) -> None:
    if not log_path.exists():
        print(f"Log file not found: {log_path}")
        sys.exit(0)

    labeled = load_labeled_keys(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open(encoding="utf-8") as log_f, \
         out_path.open("a", encoding="utf-8") as out_f:

        for raw_line in log_f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue  # malformed line — skip silently

            rec_id = _record_id(record)
            if rec_id in labeled:
                continue  # already labeled — skip

            _print_summary(record)
            while True:
                try:
                    answer = input("  label [y=acceptable / n=not / s=skip / q=quit]: ").strip().lower()
                except EOFError:
                    print("\nEOF — quitting.")
                    return

                if answer == "q":
                    print("Quit. Progress saved.")
                    return
                if answer == "s":
                    break  # leave unlabeled
                if answer in ("y", "n"):
                    acceptable = answer == "y"
                    row = build_training_row(record, acceptable)
                    out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out_f.flush()
                    labeled.add(rec_id)
                    break
                print("  Please enter y, n, s, or q.")

    print("\nAll records processed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Label gearbox delegation log records as acceptable/not for router training."
    )
    parser.add_argument(
        "--log",
        default=".claude/gearbox-log.jsonl",
        metavar="PATH",
        help="Input delegation log (default: .claude/gearbox-log.jsonl)",
    )
    parser.add_argument(
        "--out",
        default="bench/training-data.jsonl",
        metavar="PATH",
        help="Labeled output file, appended/resumed (default: bench/training-data.jsonl)",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run assert-based self-tests on pure helpers and exit.",
    )
    args = parser.parse_args()

    if args.selfcheck:
        _run_selfcheck()

    run_labeling(Path(args.log), Path(args.out))


if __name__ == "__main__":
    main()
