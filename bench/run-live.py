#!/usr/bin/env python3
"""R1-live measured benchmark harness for gearbox routing.

Runs a fixed task set (bench/fixtures/toy-cli/tasks.jsonl) under each routing
policy via headless `claude -p`, captures real cost + a binary acceptability
grade, and appends labeled rows to bench/training-data.jsonl for eval.py to
score.

Usage:
  python3 bench/run-live.py --live               # run all 3 tasks × 3 policies
  python3 bench/run-live.py                      # dry-run: print what would run
  python3 bench/run-live.py --selfcheck          # offline assert tests, exit 0

SAFETY NOTE: --live uses --permission-mode bypassPermissions. Run ONLY locally
in a throw-away workdir. Never run in CI — it spends real money and bypasses
permission prompts.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PER_RUN_EST = 0.12  # USD; ~$0.041 grounded injection floor + ~$0.08 task body

DEFAULT_MAX_COST = 2.00

# Maps --policies flag value → GEARBOX_PROFILE env-var value understood by
# inject-routing.py.  "always-t0" is excluded: T0 (haiku inline) is not a
# useful benchmark target for task-level delegation.
POLICIES: dict = {
    "live":          "balanced",
    "always-sonnet": "always-t1",
    # always-t2 routes to the read-only architect (can't edit under a forced
    # profile); use the edit-capable builder@opus profile so always-Opus is a
    # faithful measured baseline on editing tasks.
    "always-opus":   "always-opus-build",
}

# Verdict regex — matches inject-routing.py / log-routing.py _VERDICT_RE.
_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REJECT)", re.IGNORECASE)

# Prepended to every task prompt when running headless.  Instructs the
# orchestrator agent to delegate via Task tool and end with a verdict line.
DELEGATE_DIRECTIVE = (
    "You are running inside a gearbox routing benchmark (R1-live). "
    "Your ONLY job is:\n"
    "1. Dispatch the task below to a subagent via the Task tool, "
    "following the active GEARBOX_PROFILE routing policy exactly "
    "(do NOT handle the task inline even if it seems small).\n"
    "2. Operate ONLY within the current working directory — do not "
    "read or write outside it.\n"
    "3. After the subagent completes, run gearbox:verifier on the result.\n"
    "4. End your reply with exactly one line: "
    "VERDICT: APPROVE  or  VERDICT: REJECT\n\n"
    "--- TASK ---\n"
)

# Fixture directory relative to repo root (resolved at runtime).
_FIXTURE_SUBDIR = "bench/fixtures/toy-cli"

# Output paths relative to repo root.
_TRAINING_DATA = "bench/training-data.jsonl"
_LAST_RUN_SUMMARY = "bench/last-run-summary.txt"


# ---------------------------------------------------------------------------
# Pure helpers  (exercised by --selfcheck)
# ---------------------------------------------------------------------------

def load_tasks(fixture_dir: Path) -> list:
    """Read tasks.jsonl from fixture_dir; validate required fields.

    Returns a list of dicts, each with id/prompt/tier/accept.
    Raises ValueError on missing/invalid fields.
    """
    tasks_path = fixture_dir / "tasks.jsonl"
    tasks = []
    with tasks_path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"tasks.jsonl line {lineno}: {exc}") from exc
            for field in ("id", "prompt", "tier", "accept"):
                if field not in row:
                    raise ValueError(
                        f"tasks.jsonl line {lineno}: missing field {field!r}"
                    )
            if row["tier"] not in ("T0", "T1", "T2"):
                raise ValueError(
                    f"tasks.jsonl line {lineno}: tier must be T0/T1/T2, "
                    f"got {row['tier']!r}"
                )
            tasks.append(row)
    return tasks


def parse_envelope(stdout_str: str) -> dict:
    """Parse a `claude -p --output-format json` stdout string.

    Extracts the fields documented in the R1-live grounded-facts block:
    total_cost_usd, num_turns, duration_ms, is_error, result, usage,
    modelUsage.  Returns a dict; missing fields default to None/{}.
    """
    obj = json.loads(stdout_str)
    return {
        "total_cost_usd": obj.get("total_cost_usd"),
        "num_turns":      obj.get("num_turns"),
        "duration_ms":    obj.get("duration_ms"),
        "is_error":       obj.get("is_error", False),
        "result":         obj.get("result", ""),
        "usage":          obj.get("usage", {}),
        "modelUsage":     obj.get("modelUsage", {}),
    }


def model_families(model_usage: dict) -> set:
    """Map modelUsage keys → set of family names.

    Each key is a model-id string (e.g. "claude-haiku-4-5-20251001").
    Returns a subset of {"haiku", "sonnet", "opus"} by substring match.
    """
    families = set()
    for model_id in model_usage:
        lower = model_id.lower()
        for family in ("haiku", "sonnet", "opus"):
            if family in lower:
                families.add(family)
    return families


# Maps tier → expected model family for policy-binding checks.
_TIER_FAMILY: dict = {
    "T0": "haiku",
    "T1": "sonnet",
    "T2": "opus",
}

# A forced policy pins every task to one tier regardless of the task's natural
# tier, so the binding check (and the row's effective tier) must use the FORCED
# tier, not the task tier.  `live` (balanced) routes naturally → use task tier.
_POLICY_FORCED_TIER: dict = {
    "always-sonnet": "T1",   # always-t1 → builder (sonnet)
    "always-opus":   "T2",   # always-opus-build → builder on opus
}


def expected_tier(policy: str, task_tier: str) -> str:
    """The tier a row should bind to under `policy`.

    Forced policies pin a tier (always-sonnet→T1, always-opus→T2); the live
    router routes naturally, so its expected tier is the task's own tier.
    """
    return _POLICY_FORCED_TIER.get(policy, task_tier)


def policy_bound(model_usage: dict, expected_tier: str) -> bool:
    """Return True if the expected tier's model family ran.

    T0 → haiku present; T1 → sonnet present; T2 → opus present.
    A bound=False row means the routing policy was NOT followed (e.g. the
    live policy routed a T1 task to T0), so the row is excluded from
    eval totals (marked with subagent_type=(unbound)).
    """
    expected_family = _TIER_FAMILY.get(expected_tier, "")
    return expected_family in model_families(model_usage)


def scrape_verdict(result_text: str) -> "str | None":
    """Extract APPROVE/REJECT from the agent's result text.

    Returns "APPROVE", "REJECT", or None if no verdict line found.
    Case-insensitive match; last match wins if multiple lines present.
    """
    verdict = None
    for m in _VERDICT_RE.finditer(result_text or ""):
        verdict = m.group(1).upper()
    return verdict


def tier_to_agent(tier: str) -> str:
    """Map T0/T1/T2 → gearbox subagent name.

    Matches eval.py's _SUBAGENT_TIER mapping (after stripping the gearbox: prefix):
      T0 → gearbox:scout
      T1 → gearbox:builder
      T2 → gearbox:architect
    """
    return {
        "T0": "gearbox:scout",
        "T1": "gearbox:builder",
        "T2": "gearbox:architect",
    }[tier]


def build_row(
    task: dict,
    policy: str,
    env_data: dict,
    accept_ok: bool,
    bound: bool,
) -> dict:
    """Build a training-data row consumable by eval.py.

    When bound=True, sets subagent_type so eval.py derives the task's
    intended tier.  When bound=False, sets subagent_type to "(unbound)"
    which eval.py's _derive_tier resolves to "(unknown)", excluding the
    row from all cost totals (consistent with eval's skip-unknown logic).

    eval.py reads: subagent_type (for tier), model (fallback tier),
    cost_usd (exact; null→0), acceptable (bool), total_tokens (optional).
    Extra fields (policy, bound, ts, etc.) are preserved but eval ignores them.
    """
    # Effective tier = the tier this policy actually exercised (forced tier for
    # always-*, the task's own tier for live), not the task's natural tier.
    tier = expected_tier(policy, task["tier"])

    if bound:
        subagent_type = tier_to_agent(tier)
        # model string: include family name so eval's model-fallback also works
        model_map = {"T0": "claude-haiku", "T1": "claude-sonnet", "T2": "claude-opus"}
        model = model_map.get(tier, "")
    else:
        # eval skips rows whose _derive_tier returns "(unknown)"
        subagent_type = "(unbound)"
        model = ""

    usage = env_data.get("usage") or {}
    total_tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "output_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens")
    ) or None

    return {
        # --- fields eval.py scores ---
        "subagent_type": subagent_type,
        "model":         model,
        "cost_usd":      env_data.get("total_cost_usd"),
        "total_tokens":  total_tokens,
        "acceptable":    accept_ok,
        "cost_estimated": False,
        # --- extra fields eval ignores ---
        "policy":           policy,
        "task_id":          task["id"],
        "tier_expected":    tier,
        "bound":            bound,
        "verifier_verdict": env_data.get("verifier_verdict"),
        "num_turns":        env_data.get("num_turns"),
        "duration_ms":      env_data.get("duration_ms"),
        "is_error":         env_data.get("is_error"),
        "ts":               datetime.now(timezone.utc).isoformat(),
    }


def estimate_cost(n_runs: int) -> float:
    """Return the estimated total cost for n_runs × PER_RUN_EST."""
    return n_runs * PER_RUN_EST


def summary_line(rows: list) -> str:
    """Return the R3 cost-ledger summary line.

    Format: total_cost_usd=<sum>  duration_ms=<sum>  num_turns=<sum>
    Matches the agent-research cost-ledger extractor format.
    """
    total_cost = 0.0
    total_dur = 0
    total_turns = 0
    for row in rows:
        try:
            total_cost += float(row.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_dur += int(row.get("duration_ms") or 0)
        except (TypeError, ValueError):
            pass
        try:
            total_turns += int(row.get("num_turns") or 0)
        except (TypeError, ValueError):
            pass
    return (
        f"total_cost_usd={total_cost}"
        f"  duration_ms={total_dur}"
        f"  num_turns={total_turns}"
    )


# ---------------------------------------------------------------------------
# Impure helpers  (NOT exercised by --selfcheck)
# ---------------------------------------------------------------------------

def prepare_workdir(fixture_dir: Path) -> Path:
    """Copy fixture to a temp dir, git-init, commit; return the Path."""
    workdir = Path(tempfile.mkdtemp(prefix="gearbox-bench-"))
    shutil.copytree(str(fixture_dir), str(workdir), dirs_exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=workdir, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=bench@gearbox", "-c", "user.name=bench",
         "commit", "-q", "-m", "fixture: initial state"],
        cwd=workdir, check=True,
    )
    return workdir


def run_one(task: dict, policy: str, workdir: Path, timeout: int = 600) -> dict:
    """Run one task under the given policy; return parse_envelope output."""
    prompt = DELEGATE_DIRECTIVE + task["prompt"]
    env = {
        **os.environ,
        "GEARBOX_PROFILE": POLICIES[policy],
        "CLAUDE_PROJECT_DIR": str(workdir),
    }
    # Load the repo-under-test's plugin (overrides any same-named installed copy),
    # so the benchmark measures THIS checkout's routing/profiles — including the
    # benchmark-only always-opus-build profile that need not be installed.
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            "claude", "-p", prompt,
            "--model", "haiku",
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            "--add-dir", str(workdir),
            "--plugin-dir", str(repo_root),
        ],
        cwd=str(workdir),
        env=env,
        timeout=timeout,
        capture_output=True,
        text=True,
    )
    envelope = parse_envelope(result.stdout)
    envelope["verifier_verdict"] = scrape_verdict(envelope.get("result", ""))
    return envelope


def run_accept(accept_cmd: str, workdir: Path) -> bool:
    """Run accept_cmd in workdir; return True iff exit code is 0."""
    result = subprocess.run(
        accept_cmd,
        shell=True,
        cwd=str(workdir),
    )
    return result.returncode == 0


def aggregate(out_path: Path, rows: list) -> None:
    """Import eval's aggregation functions and print per-policy summary."""
    # Resolve bench/ relative to this file's location.
    bench_dir = Path(__file__).parent
    if str(bench_dir) not in sys.path:
        sys.path.insert(0, str(bench_dir))
    # Use the parent package dir so `import eval` works.
    repo_root = bench_dir.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "eval", bench_dir / "eval.py"
    )
    eval_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_mod)

    # --- per-policy measured summary ---
    from collections import defaultdict
    by_policy: dict = defaultdict(list)
    for row in rows:
        by_policy[row.get("policy", "?")].append(row)

    print("\n--- Per-policy measured summary ---")
    for pol, pol_rows in sorted(by_policy.items()):
        bound_rows = [r for r in pol_rows if r.get("bound")]
        n_bound = len(bound_rows)
        n_total = len(pol_rows)
        n_unbound = n_total - n_bound

        costs = []
        for r in pol_rows:
            try:
                costs.append(float(r.get("cost_usd") or 0))
            except (TypeError, ValueError):
                pass
        mean_cost = sum(costs) / len(costs) if costs else 0.0

        accept_rows = [r for r in pol_rows if r.get("acceptable")]
        accept_rate = len(accept_rows) / n_total if n_total else 0.0

        print(
            f"  {pol}: {n_total} runs | bound={n_bound} unbound={n_unbound}"
            f" | mean_cost=${mean_cost:.4f} | accept={accept_rate:.0%}"
        )

    # --- eval.py MODELED scorecard, scored on the LIVE-policy rows only ---
    # eval.py models always-X baselines as (tokens × per-tier rate) from a single
    # router trace.  Feeding it the mixed forced-policy rows would be nonsense, so
    # score only the `live` rows (the real router) — then the modeled always-sonnet
    # / always-opus numbers can be cross-checked against the MEASURED per-policy
    # means above (the credibility point of R1-live: do the models match reality?).
    live_rows = [r for r in eval_mod.load_labeled_rows(out_path)
                 if r.get("policy") == "live"]
    if live_rows:
        print("\n--- eval.py MODELED baselines (from live-policy rows; "
              "cross-check vs MEASURED means above) ---")
        eval_mod.print_policy_comparison(eval_mod.compute_policy_totals(live_rows))
    else:
        print("\n(no live-policy rows — skipping modeled cross-check)")


# ---------------------------------------------------------------------------
# --selfcheck
# ---------------------------------------------------------------------------

def selfcheck() -> None:
    """Assert-based offline tests on pure helpers only.  No network, no claude -p."""

    # --- parse_envelope ---
    sample_envelope = json.dumps({
        "total_cost_usd": 0.041,
        "num_turns": 3,
        "duration_ms": 12000,
        "is_error": False,
        "result": "Done. VERDICT: APPROVE",
        "session_id": "sess-abc",
        "permission_denials": [],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_input_tokens": 18000,
            "cache_creation_input_tokens": 500,
        },
        "modelUsage": {
            "claude-sonnet-4-6-20251001": {
                "inputTokens": 800,
                "outputTokens": 150,
                "costUSD": 0.030,
            },
            "claude-haiku-4-5-20251001": {
                "inputTokens": 200,
                "outputTokens": 50,
                "costUSD": 0.011,
            },
        },
    })
    env = parse_envelope(sample_envelope)
    assert env["total_cost_usd"] == 0.041, f"cost: {env['total_cost_usd']}"
    assert env["num_turns"] == 3, f"turns: {env['num_turns']}"
    assert env["duration_ms"] == 12000, f"dur: {env['duration_ms']}"
    assert env["is_error"] is False
    assert "VERDICT: APPROVE" in env["result"]

    # --- model_families ---
    mu_sonnet_haiku = {
        "claude-sonnet-4-6-20251001": {},
        "claude-haiku-4-5-20251001": {},
    }
    assert model_families(mu_sonnet_haiku) == {"sonnet", "haiku"}, \
        f"families: {model_families(mu_sonnet_haiku)}"

    mu_opus = {"claude-opus-4-20251001": {}}
    assert model_families(mu_opus) == {"opus"}

    mu_haiku_only = {"claude-haiku-4-5-20251001": {}}
    assert model_families(mu_haiku_only) == {"haiku"}

    # --- policy_bound ---
    # opus key → T2 bound True
    assert policy_bound(mu_opus, "T2") is True
    # opus key → T1 bound False (sonnet not present)
    assert policy_bound(mu_opus, "T1") is False
    # haiku-only → T2 bound False
    assert policy_bound(mu_haiku_only, "T2") is False
    # haiku-only → T0 bound True
    assert policy_bound(mu_haiku_only, "T0") is True

    # --- scrape_verdict ---
    assert scrape_verdict("Done.\nVERDICT: APPROVE\n") == "APPROVE"
    assert scrape_verdict("result VERDICT: reject here") == "REJECT"
    assert scrape_verdict("no verdict line") is None
    assert scrape_verdict("") is None
    # last match wins
    assert scrape_verdict("VERDICT: APPROVE\nVERDICT: REJECT") == "REJECT"

    # --- load_tasks: fixture ---
    fixture_dir = Path(__file__).parent / "fixtures" / "toy-cli"
    tasks = load_tasks(fixture_dir)
    assert len(tasks) == 3, f"expected 3 tasks, got {len(tasks)}"
    tiers = {t["tier"] for t in tasks}
    assert tiers == {"T0", "T1", "T2"}, f"tiers: {tiers}"
    for t in tasks:
        assert "id" in t and t["id"]
        assert "prompt" in t and t["prompt"]
        assert "accept" in t and t["accept"]

    # --- tier_to_agent ---
    assert tier_to_agent("T0") == "gearbox:scout"
    assert tier_to_agent("T1") == "gearbox:builder"
    assert tier_to_agent("T2") == "gearbox:architect"

    # --- build_row: cross-import eval.py to prove schema compatibility ---
    bench_dir = Path(__file__).parent
    import importlib.util
    spec = importlib.util.spec_from_file_location("eval", bench_dir / "eval.py")
    eval_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_mod)

    t1_task = {"id": "t1-test", "tier": "T1", "prompt": "...", "accept": "true"}
    env_data = {
        "total_cost_usd": 0.08,
        "num_turns": 2,
        "duration_ms": 9000,
        "is_error": False,
        "verifier_verdict": "APPROVE",
    }

    # bound=True row: eval should derive T1
    row_bound = build_row(t1_task, "live", env_data, accept_ok=True, bound=True)
    assert row_bound["cost_usd"] == 0.08
    assert row_bound["acceptable"] is True
    tier_derived = eval_mod._derive_tier(row_bound)
    assert tier_derived == "T1", \
        f"bound T1 row derives to {tier_derived!r}, expected 'T1'"

    # bound=False row: eval should derive (unknown) and exclude from totals
    row_unbound = build_row(t1_task, "live", env_data, accept_ok=False, bound=False)
    tier_unbound = eval_mod._derive_tier(row_unbound)
    assert tier_unbound == "(unknown)", \
        f"unbound row derives to {tier_unbound!r}, expected '(unknown)'"

    # prove compute_policy_totals excludes the unbound row
    totals = eval_mod.compute_policy_totals([row_bound, row_unbound])
    assert totals["task_n"] == 1, \
        f"task_n should be 1 (unbound excluded), got {totals['task_n']}"
    assert totals["acceptable_count"] == 1

    # T2 bound row
    t2_task = {"id": "t2-test", "tier": "T2", "prompt": "...", "accept": "true"}
    row_t2 = build_row(t2_task, "always-opus", env_data, accept_ok=True, bound=True)
    assert eval_mod._derive_tier(row_t2) == "T2"

    # --- expected_tier: forced policies pin the tier; live uses the task tier ---
    assert expected_tier("always-sonnet", "T2") == "T1"
    assert expected_tier("always-opus", "T0") == "T2"
    assert expected_tier("live", "T2") == "T2"
    assert expected_tier("live", "T0") == "T0"

    # A forced policy labels the FORCED tier, not the task's natural tier:
    # a T0 task under always-opus is an opus (T2) run.  (Regression for the
    # bound false-negative the first live pass exposed.)
    t0_task = {"id": "t0-x", "tier": "T0", "prompt": "...", "accept": "true"}
    row_forced = build_row(t0_task, "always-opus", env_data, accept_ok=True, bound=True)
    assert eval_mod._derive_tier(row_forced) == "T2", \
        f"always-opus T0-task should label T2, got {eval_mod._derive_tier(row_forced)!r}"

    # always-sonnet on a T2 task → bind on sonnet (the forced tier), not opus
    assert policy_bound(mu_sonnet_haiku, expected_tier("always-sonnet", "T2")) is True
    assert policy_bound(mu_opus, expected_tier("always-sonnet", "T2")) is False

    # total_tokens summed from the envelope usage split
    env_tok = {**env_data, "usage": {
        "input_tokens": 1000, "output_tokens": 200,
        "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}}
    row_tok = build_row(t1_task, "live", env_tok, accept_ok=True, bound=True)
    assert row_tok["total_tokens"] == 1260, f"total_tokens: {row_tok['total_tokens']}"

    # --- estimate_cost ---
    assert abs(estimate_cost(9) - 1.08) < 1e-9, \
        f"estimate_cost(9): {estimate_cost(9)}"
    assert estimate_cost(50) > DEFAULT_MAX_COST, \
        f"estimate_cost(50)={estimate_cost(50)} should exceed {DEFAULT_MAX_COST}"

    # --- summary_line ---
    test_rows = [
        {"cost_usd": 0.10, "duration_ms": 5000,  "num_turns": 2},
        {"cost_usd": 0.20, "duration_ms": 8000,  "num_turns": 3},
        {"cost_usd": None, "duration_ms": None,   "num_turns": None},
    ]
    sl = summary_line(test_rows)
    assert sl == "total_cost_usd=0.30000000000000004  duration_ms=13000  num_turns=5", \
        f"summary_line: {sl!r}"

    print("selfcheck OK")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "R1-live measured benchmark: run gearbox tasks under each routing "
            "policy and write labeled rows to bench/training-data.jsonl."
        )
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually run claude -p. Without this flag: dry-run only.",
    )
    parser.add_argument(
        "--policies",
        nargs="+",
        choices=list(POLICIES),
        default=list(POLICIES),
        metavar="POLICY",
        help=f"Policies to run (default: all). Choices: {list(POLICIES)}",
    )
    parser.add_argument(
        "-n", "--scale",
        type=int,
        default=None,
        metavar="N",
        help="Run only the first N tasks per policy (default: all tasks).",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=DEFAULT_MAX_COST,
        metavar="USD",
        help=f"Budget ceiling in USD (default: {DEFAULT_MAX_COST}). "
             "Refuses to start if estimated cost exceeds this.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Do not delete temporary workdirs after each run.",
    )
    parser.add_argument(
        "--selfcheck",
        action="store_true",
        help="Run offline assert tests and exit 0.",
    )
    args = parser.parse_args()

    if args.selfcheck:
        selfcheck()

    # Resolve fixture and output paths relative to this file's location.
    repo_root = Path(__file__).parent.parent
    fixture_dir = repo_root / _FIXTURE_SUBDIR
    out_path = repo_root / _TRAINING_DATA
    summary_path = repo_root / _LAST_RUN_SUMMARY

    tasks = load_tasks(fixture_dir)
    if args.scale is not None:
        tasks = tasks[: args.scale]

    policies = args.policies
    n_runs = len(tasks) * len(policies)
    est = estimate_cost(n_runs)

    print(f"Fixture: {fixture_dir}")
    print(f"Tasks: {len(tasks)}, Policies: {policies}, Runs: {n_runs}")
    print(f"Estimated cost: ${est:.2f} (${PER_RUN_EST}/run × {n_runs})")

    if est > args.max_cost:
        print(
            f"REFUSED: estimated ${est:.2f} exceeds --max-cost ${args.max_cost:.2f}. "
            f"Use --max-cost {est:.2f} or reduce with --scale."
        )
        sys.exit(1)

    if not args.live:
        print("\nDry-run: pass --live to actually execute. Plan:")
        for policy in policies:
            for task in tasks:
                print(
                    f"  [{policy}] {task['id']} ({task['tier']}) — "
                    f"accept: {task['accept']!r}"
                )
        sys.exit(0)

    # --- live run ---
    written_rows: list = []
    spent = 0.0
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("a", encoding="utf-8") as out_f:
        for policy in policies:
            for task in tasks:
                # Budget gate: check before each run.
                if spent + PER_RUN_EST > args.max_cost:
                    print(
                        f"\nhalted: budget ceiling (spent=${spent:.4f}, "
                        f"next estimated ${PER_RUN_EST:.2f} would exceed "
                        f"--max-cost ${args.max_cost:.2f})"
                    )
                    print(f"Completed {len(written_rows)} runs. Writing rows.")
                    break

                workdir = prepare_workdir(fixture_dir)
                print(
                    f"\n[{policy}] {task['id']} ({task['tier']}) "
                    f"workdir={workdir} ...",
                    flush=True,
                )

                try:
                    env_data = run_one(task, policy, workdir)
                except subprocess.TimeoutExpired:
                    print(f"  TIMEOUT after 600s — skipping.")
                    if not args.keep_temp:
                        shutil.rmtree(workdir, ignore_errors=True)
                    continue
                except Exception as exc:
                    print(f"  ERROR: {exc} — skipping.")
                    if not args.keep_temp:
                        shutil.rmtree(workdir, ignore_errors=True)
                    continue

                run_cost = env_data.get("total_cost_usd") or PER_RUN_EST
                spent += run_cost

                accept_ok = run_accept(task["accept"], workdir)
                bound = policy_bound(
                    env_data.get("modelUsage", {}),
                    expected_tier(policy, task["tier"]),
                )

                row = build_row(task, policy, env_data, accept_ok, bound)
                out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                out_f.flush()
                written_rows.append(row)

                verdict = env_data.get("verifier_verdict", "?")
                print(
                    f"  cost=${run_cost:.4f} turns={env_data.get('num_turns')} "
                    f"verdict={verdict} accept={accept_ok} bound={bound}"
                )

                if not args.keep_temp:
                    shutil.rmtree(workdir, ignore_errors=True)
            else:
                # inner loop completed without budget-halt break
                continue
            # inner loop hit the budget-halt break — break outer too
            break

    # --- summary ---
    sl = summary_line(written_rows)
    print(f"\n{sl}")
    summary_path.write_text(sl + "\n", encoding="utf-8")
    print(f"Summary written to {summary_path}")

    if written_rows:
        aggregate(out_path, written_rows)


if __name__ == "__main__":
    main()
