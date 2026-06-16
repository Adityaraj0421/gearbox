#!/usr/bin/env python3
"""One-time idempotent importer: consolidate per-project gearbox logs into the
global ~/.claude/gearbox-log.jsonl.

Scans $HOME for .claude/gearbox-log.jsonl files in subdirectories, skipping
the global log itself, and appends any not-already-present records.

Dedup key:
  - Records that have a `uid` field: use uid directly.
  - Older records without uid: SHA1 of the full record JSON (canonical key
    order via sort_keys=True), so they are neither dropped nor re-imported.

Source files are never modified or deleted.
"""
import argparse
import glob
import hashlib
import json
import sys
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------

def _dedup_key(record: dict) -> str:
    uid = record.get("uid")
    if uid:
        return str(uid)
    # Legacy records without uid: stable hash of full JSON.
    return hashlib.sha1(json.dumps(record, sort_keys=True).encode()).hexdigest()


def _load_present_keys(global_log: Path) -> set:
    """Return the set of dedup keys already in the global log."""
    keys = set()
    if not global_log.exists():
        return keys
    with global_log.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(_dedup_key(json.loads(line)))
            except (json.JSONDecodeError, Exception):
                pass
    return keys


def _find_source_logs(home: Path, global_log: Path) -> list:
    """Return all per-project log paths under home, excluding global_log."""
    pattern = str(home / "**" / ".claude" / "gearbox-log.jsonl")
    paths = []
    for p in glob.glob(pattern, recursive=True):
        resolved = Path(p).resolve()
        if resolved != global_log.resolve():
            paths.append(Path(p))
    return paths


# ---------------------------------------------------------------------------
# Core import logic
# ---------------------------------------------------------------------------

def migrate(home: Path, global_log: Path, dry_run: bool) -> tuple:
    """Import records from per-project logs into global_log.

    Returns (files_scanned, records_imported, records_skipped).
    """
    source_logs = _find_source_logs(home, global_log)
    present_keys = _load_present_keys(global_log)

    files_scanned = len(source_logs)
    records_imported = 0
    records_skipped = 0

    if not dry_run:
        global_log.parent.mkdir(parents=True, exist_ok=True)

    append_target = global_log.open("a", encoding="utf-8") if not dry_run else None
    try:
        for src in source_logs:
            try:
                with src.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        key = _dedup_key(record)
                        if key in present_keys:
                            records_skipped += 1
                        else:
                            if append_target is not None:
                                append_target.write(json.dumps(record, ensure_ascii=False) + "\n")
                            present_keys.add(key)
                            records_imported += 1
            except OSError:
                pass  # skip unreadable source files
    finally:
        if append_target is not None:
            append_target.close()

    return files_scanned, records_imported, records_skipped


# ---------------------------------------------------------------------------
# Selfcheck
# ---------------------------------------------------------------------------

def selfcheck() -> None:
    """Assert-based tests of dedup/import logic against synthetic records in a
    temp dir. Exits 0 on success, non-zero on assertion failure."""
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)

        home = tmp / "home"
        home.mkdir()
        global_log = home / ".claude" / "gearbox-log.jsonl"

        # Build two per-project log dirs.
        proj_a = home / "projects" / "alpha" / ".claude"
        proj_b = home / "projects" / "beta" / ".claude"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)

        rec1 = {"uid": "pid1-100", "ts": 1, "cwd": "/alpha", "tool_name": "Agent"}
        rec2 = {"uid": "pid2-200", "ts": 2, "cwd": "/alpha", "tool_name": "Agent"}
        rec3 = {"uid": "pid3-300", "ts": 3, "cwd": "/beta",  "tool_name": "Agent"}
        # Legacy record — no uid; dedup by content hash.
        rec_legacy = {"ts": 99, "cwd": "/old", "tool_name": "Task", "model": "haiku"}

        (proj_a / "gearbox-log.jsonl").write_text(
            json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n" + json.dumps(rec_legacy) + "\n",
            encoding="utf-8",
        )
        (proj_b / "gearbox-log.jsonl").write_text(
            json.dumps(rec3) + "\n",
            encoding="utf-8",
        )

        # --- First import: all 4 records should be imported, 0 skipped.
        files, imported, skipped = migrate(home, global_log, dry_run=False)
        assert files == 2, f"expected 2 files scanned, got {files}"
        assert imported == 4, f"expected 4 imported, got {imported}"
        assert skipped == 0, f"expected 0 skipped, got {skipped}"
        assert global_log.exists(), "global log must exist after import"
        with global_log.open(encoding="utf-8") as f:
            written_lines = [l.strip() for l in f if l.strip()]
        assert len(written_lines) == 4, f"expected 4 lines in global log, got {len(written_lines)}"

        # --- Second import: all 4 already present → 0 imported, 4 skipped.
        files2, imported2, skipped2 = migrate(home, global_log, dry_run=False)
        assert files2 == 2, f"expected 2 files, got {files2}"
        assert imported2 == 0, f"expected 0 imported on re-run, got {imported2}"
        assert skipped2 == 4, f"expected 4 skipped on re-run, got {skipped2}"

        # --- Dry-run: add a new record to proj_a and confirm dry-run doesn't write.
        rec4 = {"uid": "pid4-400", "ts": 4, "cwd": "/alpha", "tool_name": "Agent"}
        with (proj_a / "gearbox-log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec4) + "\n")

        files3, imported3, skipped3 = migrate(home, global_log, dry_run=True)
        assert imported3 == 1, f"dry-run should report 1 new record, got {imported3}"
        assert skipped3 == 4, f"dry-run should report 4 skipped, got {skipped3}"
        # Global log must still have only 4 lines (dry-run wrote nothing).
        with global_log.open(encoding="utf-8") as f:
            lines_after_dry = [l.strip() for l in f if l.strip()]
        assert len(lines_after_dry) == 4, \
            f"dry-run must not write; expected 4 lines, got {len(lines_after_dry)}"

        # --- Global log itself is excluded from source scan.
        global_log_in_sources = any(
            Path(p).resolve() == global_log.resolve()
            for p in _find_source_logs(home, global_log)
        )
        assert not global_log_in_sources, "global log must not appear as a source"

        # --- Dedup key: uid present → uid used (not hash).
        k1 = _dedup_key(rec1)
        assert k1 == "pid1-100", f"expected uid as key, got {k1!r}"

        # --- Dedup key: no uid → SHA1 of JSON (stable across calls).
        k_leg_a = _dedup_key(rec_legacy)
        k_leg_b = _dedup_key(dict(rec_legacy))  # same content, new dict
        assert k_leg_a == k_leg_b, "legacy dedup key must be content-stable"
        # Different content → different key.
        k_leg_diff = _dedup_key(dict(rec_legacy, ts=0))
        assert k_leg_a != k_leg_diff, "different content must yield different legacy key"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import per-project gearbox logs into the global ~/.claude/gearbox-log.jsonl."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing anything.",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run assert-based self-tests in a temp dir and exit.",
    )
    args = parser.parse_args()

    if args.selfcheck:
        selfcheck()

    home = Path.home()
    global_log = home / ".claude" / "gearbox-log.jsonl"

    files, imported, skipped = migrate(home, global_log, dry_run=args.dry_run)

    mode = "[dry-run] " if args.dry_run else ""
    print(f"{mode}files scanned: {files}")
    print(f"{mode}records imported: {imported}")
    print(f"{mode}records skipped (duplicates): {skipped}")


if __name__ == "__main__":
    main()
