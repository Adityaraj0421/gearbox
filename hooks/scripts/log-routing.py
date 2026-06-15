#!/usr/bin/env python3
"""Gearbox routing logger.

PostToolUse hook for the Task tool. Reads the hook event JSON from stdin and
appends one line per delegation to .claude/gearbox-log.jsonl in the PROJECT
directory (cwd), not the plugin directory — the telemetry belongs to the repo
being worked on.

This log is the seed data for a future learned router (contextual bandit over
{model x tier} with reward = success/cost). Verify the exact hook input schema
against your Claude Code version's hooks docs if fields come back empty.

tool_response schema (empirically confirmed 2026-06 across 15 Task dispatches,
subagent models claude-haiku-4-5 / claude-sonnet-4-6):
  Top-level keys: totalTokens, totalToolUseCount, totalDurationMs,
                  plus a nested "usage" dict (input_tokens, output_tokens, …).
  No cost field exists — cost is always estimated from token counts.
Legacy key names are retained as cross-version fallback.
"""
import json
import re
import sys
import time
from pathlib import Path

# ponytail: approximate blended USD-per-million-tokens rates; refine per
# input/output token split if the hook ever exposes it.
_BLENDED_RATES = {
    "haiku": 0.8,
    "sonnet": 9.0,
    "opus": 45.0,
}
_DEFAULT_RATE = _BLENDED_RATES["sonnet"]

_USAGE_BLOCK_RE = re.compile(r"<usage>(.*?)</usage>", re.DOTALL)
_USAGE_LINE_RE = re.compile(r"(\w+):\s*(\d+(?:\.\d+)?)")


def _model_rate(model: str) -> float:
    m = (model or "").lower()
    for key, rate in _BLENDED_RATES.items():
        if key in m:
            return rate
    return _DEFAULT_RATE


def _parse_usage_string(text: str) -> dict:
    """Extract metrics from a <usage>...</usage> block in a string."""
    result: dict = {}
    match = _USAGE_BLOCK_RE.search(text)
    if not match:
        return result
    for name, val in _USAGE_LINE_RE.findall(match.group(1)):
        result[name] = val
    return result


def _first(d: dict, *keys):
    """Return the value of the first key found in d, or None."""
    for k in keys:
        if k in d:
            return d[k]
    return None


def _coalesce(a, b):
    """Return a if it is not None, else b. Unlike `a or b`, preserves 0/False."""
    return a if a is not None else b


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_metrics(tool_response) -> dict:
    """Defensively extract usage metrics from tool_response.

    tool_response may be a dict, a string, or anything else — never raise.
    Returns a dict with keys: total_tokens, num_turns, duration_ms,
    cost_usd, cost_estimated.
    """
    total_tokens = None
    num_turns = None
    duration_ms = None
    cost_usd = None
    cost_estimated = False

    try:
        if isinstance(tool_response, dict):
            tr = tool_response
            # look for a nested usage sub-dict first
            usage = tr.get("usage") if isinstance(tr.get("usage"), dict) else {}

            raw_tokens = _coalesce(
                _first(tr, "totalTokens", "total_tokens", "subagent_tokens", "tokens"),
                _first(usage, "totalTokens", "total_tokens", "subagent_tokens", "tokens"),
            )
            if raw_tokens is None:
                # No aggregate token field; fall back to summing split usage.
                in_tok = _int_or_none(_coalesce(
                    _first(tr, "input_tokens", "inputTokens"),
                    _first(usage, "input_tokens", "inputTokens"),
                ))
                out_tok = _int_or_none(_coalesce(
                    _first(tr, "output_tokens", "outputTokens"),
                    _first(usage, "output_tokens", "outputTokens"),
                ))
                if in_tok is not None and out_tok is not None:
                    raw_tokens = in_tok + out_tok

            raw_turns = _coalesce(
                _first(tr, "totalToolUseCount", "num_turns", "tool_uses", "toolUses", "turns"),
                _first(usage, "totalToolUseCount", "num_turns", "tool_uses", "toolUses", "turns"),
            )

            raw_duration = _coalesce(
                _first(tr, "totalDurationMs", "duration_ms", "durationMs", "duration"),
                _first(usage, "totalDurationMs", "duration_ms", "durationMs", "duration"),
            )

            raw_cost = _coalesce(
                _first(tr, "total_cost_usd", "cost_usd", "costUSD", "total_cost"),
                _first(usage, "total_cost_usd", "cost_usd", "costUSD", "total_cost"),
            )

            total_tokens = _int_or_none(raw_tokens)
            num_turns = _int_or_none(raw_turns)
            duration_ms = _int_or_none(raw_duration)
            direct_cost = _float_or_none(raw_cost)

            if direct_cost is not None:
                cost_usd = direct_cost
                cost_estimated = False

            # also try string fallback on any string values inside the dict
            for v in tr.values():
                if isinstance(v, str) and "<usage>" in v:
                    parsed = _parse_usage_string(v)
                    if total_tokens is None:
                        total_tokens = _int_or_none(
                            parsed.get("subagent_tokens") or parsed.get("total_tokens")
                        )
                    if num_turns is None:
                        num_turns = _int_or_none(
                            parsed.get("tool_uses") or parsed.get("num_turns")
                        )
                    if duration_ms is None:
                        duration_ms = _int_or_none(parsed.get("duration_ms"))
                    break

        elif isinstance(tool_response, str):
            parsed = _parse_usage_string(tool_response)
            total_tokens = _int_or_none(
                parsed.get("subagent_tokens") or parsed.get("total_tokens")
            )
            num_turns = _int_or_none(
                parsed.get("tool_uses") or parsed.get("num_turns")
            )
            duration_ms = _int_or_none(parsed.get("duration_ms"))
            # no direct cost in the rendered string form
    except Exception:
        pass  # best-effort; never raise

    return {
        "total_tokens": total_tokens,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
        "cost_estimated": cost_estimated,
    }


def build_record(event: dict) -> dict:
    """Build the log record from a hook event dict. Pure function."""
    tool_input = event.get("tool_input", {}) or {}
    model = tool_input.get("model", "(not passed)") or "(not passed)"

    metrics = _extract_metrics(event.get("tool_response"))

    # Estimate cost from tokens if no direct cost was reported.
    if metrics["cost_usd"] is None and metrics["total_tokens"] is not None:
        rate = _model_rate(model)
        metrics["cost_usd"] = round(metrics["total_tokens"] / 1e6 * rate, 8)
        metrics["cost_estimated"] = True

    return {
        "ts": int(time.time()),
        "session_id": event.get("session_id", ""),
        "tool_name": event.get("tool_name", ""),
        "subagent_type": tool_input.get("subagent_type", ""),
        "model": model,
        "prompt_head": (tool_input.get("prompt", "") or "")[:200],
        "cwd": event.get("cwd", ""),
        "total_tokens": metrics["total_tokens"],
        "num_turns": metrics["num_turns"],
        "duration_ms": metrics["duration_ms"],
        "cost_usd": metrics["cost_usd"],
        "cost_estimated": metrics["cost_estimated"],
    }


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return  # never block the session on logger failure

    record = build_record(event)

    log_path = Path(event.get("cwd") or ".") / ".claude" / "gearbox-log.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the session


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        # Synthetic event with a dict tool_response
        event_dict = {
            "session_id": "test-session",
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "builder",
                "model": "claude-sonnet",
                "prompt": "Do something",
            },
            "cwd": "/tmp",
            "tool_response": {
                "total_tokens": 500,
                "tool_uses": 3,
                "duration_ms": 4000,
            },
        }
        r1 = build_record(event_dict)
        assert r1["total_tokens"] == 500, f"expected 500, got {r1['total_tokens']}"
        assert r1["num_turns"] == 3, f"expected 3, got {r1['num_turns']}"
        assert r1["duration_ms"] == 4000, f"expected 4000, got {r1['duration_ms']}"
        assert r1["cost_estimated"] is True, "expected cost_estimated=True"
        assert r1["cost_usd"] is not None and r1["cost_usd"] > 0, "expected cost_usd > 0"

        # Synthetic event with a string tool_response
        usage_str = "<usage>subagent_tokens: 100\ntool_uses: 5\nduration_ms: 2000</usage>"
        event_str = {
            "session_id": "test-session-2",
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "grunt",
                "model": "claude-haiku",
                "prompt": "Do something else",
            },
            "cwd": "/tmp",
            "tool_response": usage_str,
        }
        r2 = build_record(event_str)
        assert r2["total_tokens"] == 100, f"expected 100, got {r2['total_tokens']}"
        assert r2["num_turns"] == 5, f"expected 5, got {r2['num_turns']}"
        assert r2["duration_ms"] == 2000, f"expected 2000, got {r2['duration_ms']}"
        assert r2["cost_estimated"] is True, "expected cost_estimated=True"
        assert r2["cost_usd"] is not None and r2["cost_usd"] > 0, "expected cost_usd > 0"

        # Real captured shape (15 dispatches, 2026-06, claude-haiku-4-5 / claude-sonnet-4-6)
        real_shape = {
            "session_id": "s3",
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "gearbox:scout",
                "model": "claude-haiku-4-5",
                "prompt": "probe",
            },
            "cwd": "/tmp",
            "tool_response": {
                "status": "completed",
                "agentType": "gearbox:scout",
                "resolvedModel": "claude-haiku-4-5",
                "totalTokens": 6294,
                "totalToolUseCount": 0,
                "totalDurationMs": 1275,
                "usage": {"input_tokens": 3, "output_tokens": 10},
            },
        }
        r3 = build_record(real_shape)
        assert r3["total_tokens"] == 6294, f"expected 6294, got {r3['total_tokens']}"
        assert r3["num_turns"] == 0, f"expected 0, got {r3['num_turns']}"  # falsy-coalescing guard
        assert r3["duration_ms"] == 1275, f"expected 1275, got {r3['duration_ms']}"
        assert r3["cost_estimated"] is True, "expected cost_estimated=True"
        assert r3["cost_usd"] is not None and r3["cost_usd"] > 0, "expected cost_usd > 0"

        # Split-usage fallback: no aggregate token field, only input/output → summed
        split_shape = {
            "session_id": "s4",
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "gearbox:scout",
                "model": "claude-haiku-4-5",
                "prompt": "probe",
            },
            "cwd": "/tmp",
            "tool_response": {
                "status": "completed",
                "usage": {"input_tokens": 120, "output_tokens": 30},
            },
        }
        r4 = build_record(split_shape)
        assert r4["total_tokens"] == 150, f"expected 150, got {r4['total_tokens']}"

        print("selfcheck OK")
        sys.exit(0)

    main()
