#!/usr/bin/env python3
"""Gearbox routing logger.

PostToolUse hook for the Task tool. Reads the hook event JSON from stdin and
appends one line per delegation to the canonical global log at
~/.claude/gearbox-log.jsonl. Each record keeps its project `cwd`, so per-project
views are a group-by-cwd over the single global corpus.

This log is the seed data for a future learned router (contextual bandit over
{model x tier} with reward = success/cost). Verify the exact hook input schema
against your Claude Code version's hooks docs if fields come back empty.

tool_response schema (empirically confirmed 2026-06 across 15 Task dispatches,
subagent models claude-haiku-4-5 / claude-sonnet-4-6):
  Top-level keys: totalTokens, totalToolUseCount, totalDurationMs,
                  plus a nested "usage" dict with the full per-component token
                  split: input_tokens, output_tokens, cache_read_input_tokens,
                  cache_creation_input_tokens, and a cache_creation sub-dict
                  with ephemeral_5m_input_tokens / ephemeral_1h_input_tokens.
  Cost is computed exactly per-component when the split is present; falls back
  to a blended per-model rate estimate only when the split is absent.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

# Mirrors routing/routing.md tier assignments. Keyed by bare agent name
# (no "gearbox:" prefix). Used to derive model/tier when not explicitly passed.
_AGENT_ROUTING: dict = {
    "scout":    {"tier": "T0", "model": "haiku"},
    "grunt":    {"tier": "T0", "model": "haiku"},
    # TV = verifier meta-tier; verifier is not a routing tier (T0/T1/T2) —
    # it is a post-delegation quality gate applied by the lead.
    "verifier": {"tier": "TV", "model": "haiku"},
    "builder":  {"tier": "T1", "model": "sonnet"},
    "architect": {"tier": "T2", "model": "opus"},
}

_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVE|REJECT)", re.IGNORECASE)

# Per-component USD-per-million-tokens rates, 2026-06 Anthropic rate card.
# Re-pin date and values when pricing changes.
# Keys: input, output, cache_read, cache_write_5m, cache_write_1h.
_TOKEN_RATES: dict = {
    "haiku":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write_5m": 1.25, "cache_write_1h":  2.00},
    "sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h":  6.00},
    "opus":   {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write_5m": 6.25, "cache_write_1h": 10.00},
}
_DEFAULT_TOKEN_RATES = _TOKEN_RATES["sonnet"]

# ponytail: rough blended USD-per-million-tokens fallback rates, 2026-06 rate
# card. Used only when the per-component split is absent (degenerate/old payload).
# Re-pin date and values when pricing changes.
_BLENDED_RATES = {
    "haiku": 1.5,
    "sonnet": 5.0,
    "opus": 8.0,
}
_DEFAULT_RATE = _BLENDED_RATES["sonnet"]

# ponytail: regex best-effort secret scrubber — not a full secret scanner;
# misses obfuscated/encoded credentials and multi-line values split across the
# 200-char truncation boundary.  Good enough to stop accidental paste-in leaks.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_AWS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_KV_SECRET_RE = re.compile(
    r"(?i)(?P<key>(?:secret|token|password|passwd|api[_-]?key|access[_-]?key|bearer|authorization))"
    r"(?P<sep>\s*[:=]\s*)(?P<val>[^\s,;\"\']+)",
)
# Hex or base64-ish opaque token of >= 32 chars (no whitespace, mostly alphanum/+/=/-/_)
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")


def _scrub_secrets(text: str) -> str:
    """Best-effort redaction of common credential shapes. Returns scrubbed text."""
    text = _PEM_RE.sub("[REDACTED]", text)
    text = _AWS_KEY_RE.sub("[REDACTED]", text)
    text = _KV_SECRET_RE.sub(lambda m: m.group("key") + m.group("sep") + "[REDACTED]", text)
    text = _LONG_TOKEN_RE.sub("[REDACTED]", text)
    return text


def _model_rate(model: str) -> float:
    m = (model or "").lower()
    for key, rate in _BLENDED_RATES.items():
        if key in m:
            return rate
    return _DEFAULT_RATE


def _model_token_rates(model: str) -> dict:
    """Return the per-component rate dict for model. Default: sonnet."""
    m = (model or "").lower()
    for key, rates in _TOKEN_RATES.items():
        if key in m:
            return rates
    return _DEFAULT_TOKEN_RATES


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
    if isinstance(v, bool):
        return None
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

    tool_response is always a dict (empirically confirmed across 15+ dispatches).
    Returns a dict with keys: total_tokens, input_tokens, output_tokens,
    cache_read_tokens, cache_creation_tokens, num_turns, duration_ms,
    cost_usd, cost_estimated.
    """
    total_tokens = None
    input_tokens = None
    output_tokens = None
    cache_read_tokens = None
    cache_creation_tokens = None
    cc_5m = None
    cc_1h = None
    num_turns = None
    duration_ms = None
    cost_usd = None
    cost_estimated = False

    try:
        if isinstance(tool_response, dict):
            tr = tool_response
            # look for a nested usage sub-dict first
            usage = tr.get("usage") if isinstance(tr.get("usage"), dict) else {}

            # Empirically confirmed keys (2026-06, 15+ dispatches):
            #   aggregate: totalTokens / totalToolUseCount / totalDurationMs
            #   split: usage.input_tokens / usage.output_tokens /
            #          usage.cache_read_input_tokens / usage.cache_creation_input_tokens
            #          usage.cache_creation.ephemeral_5m_input_tokens
            #          usage.cache_creation.ephemeral_1h_input_tokens
            raw_tokens = _first(tr, "totalTokens")
            if raw_tokens is None:
                raw_tokens = _first(usage, "totalTokens")

            # Extract the per-component token split from the usage sub-dict.
            input_tokens = _int_or_none(_coalesce(
                _first(usage, "input_tokens"),
                _first(tr, "input_tokens"),
            ))
            output_tokens = _int_or_none(_coalesce(
                _first(usage, "output_tokens"),
                _first(tr, "output_tokens"),
            ))
            cache_read_tokens = _int_or_none(_first(usage, "cache_read_input_tokens"))
            cache_creation_tokens = _int_or_none(_first(usage, "cache_creation_input_tokens"))

            # Extract the 5m/1h cache-write sub-breakdown if present.
            cc_sub = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else None
            cc_5m = _int_or_none(_first(cc_sub, "ephemeral_5m_input_tokens")) if cc_sub is not None else None
            cc_1h = _int_or_none(_first(cc_sub, "ephemeral_1h_input_tokens")) if cc_sub is not None else None
            # ponytail: if sub-breakdown absent but cache_creation_input_tokens present,
            # bill the whole cache-creation amount at the 5m write rate (the common case).
            if cc_5m is None and cc_1h is None and cache_creation_tokens is not None:
                cc_5m = cache_creation_tokens

            if raw_tokens is None:
                # No aggregate token field; fall back to summing split usage.
                # Sum whichever sides are present; both absent → None (not 0).
                if input_tokens is not None or output_tokens is not None:
                    raw_tokens = (
                        (input_tokens or 0)
                        + (output_tokens or 0)
                        + (cache_read_tokens or 0)
                        + (cache_creation_tokens or 0)
                    )

            raw_turns = _coalesce(
                _first(tr, "totalToolUseCount"),
                _first(usage, "totalToolUseCount"),
            )

            raw_duration = _coalesce(
                _first(tr, "totalDurationMs"),
                _first(usage, "totalDurationMs"),
            )

            raw_cost = _coalesce(
                _first(tr, "total_cost_usd"),
                _first(usage, "total_cost_usd"),
            )

            total_tokens = _int_or_none(raw_tokens)
            num_turns = _int_or_none(raw_turns)
            duration_ms = _int_or_none(raw_duration)
            direct_cost = _float_or_none(raw_cost)

            if direct_cost is not None:
                cost_usd = direct_cost
                cost_estimated = False

    except Exception:
        pass  # best-effort; never raise

    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        # _cc_5m/_cc_1h: internal; used by _exact_cost; not written to the log record
        "_cc_5m": cc_5m,
        "_cc_1h": cc_1h,
        "num_turns": num_turns,
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
        "cost_estimated": cost_estimated,
    }


def _exact_cost(metrics: dict, rates: dict) -> float | None:
    """Compute exact cost from per-component token split and rates dict.

    Returns cost in USD (rounded to 8 decimals), or None if no split component
    is present at all.
    """
    # "split present" = at least one component key exists
    in_t = metrics.get("input_tokens")
    out_t = metrics.get("output_tokens")
    cr_t = metrics.get("cache_read_tokens")
    cc_t = metrics.get("cache_creation_tokens")
    # cache_creation sub-breakdown (5m / 1h), passed through separately
    cc_5m = metrics.get("_cc_5m")
    cc_1h = metrics.get("_cc_1h")

    if in_t is None and out_t is None and cr_t is None and cc_t is None:
        return None

    cost = (
        (in_t or 0) * rates["input"]
        + (out_t or 0) * rates["output"]
        + (cr_t or 0) * rates["cache_read"]
        + (cc_5m or 0) * rates["cache_write_5m"]
        + (cc_1h or 0) * rates["cache_write_1h"]
    ) / 1e6
    return round(cost, 8)


def _tool_response_text(tool_response) -> str:
    """Return a flat text blob from tool_response for pattern matching."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        parts = []
        for v in tool_response.values():
            if isinstance(v, str):
                parts.append(v)
        return " ".join(parts)
    return ""


def resolve_routing(subagent_type: str, tool_input: dict, tool_response) -> dict:
    """Resolve model, model_source, tier, and verdict for a delegation.

    Pure function — no I/O. Returns a dict with keys:
      model, model_source, tier, verdict
    """
    # Strip optional "gearbox:" namespace prefix.
    bare = (subagent_type or "").removeprefix("gearbox:")

    mapping = _AGENT_ROUTING.get(bare)

    # --- model + model_source ---
    raw_model = (tool_input or {}).get("model") or ""
    if raw_model:
        model = raw_model
        model_source = "passed"
    elif mapping:
        model = mapping["model"]
        model_source = "derived"
    else:
        model = "(not passed)"
        model_source = "absent"

    # --- tier ---
    tier = mapping["tier"] if mapping else None

    # --- verdict (verifier only) ---
    verdict = None
    if bare == "verifier":
        text = _tool_response_text(tool_response)
        m = _VERDICT_RE.search(text)
        if m:
            verdict = m.group(1).lower()

    return {"model": model, "model_source": model_source, "tier": tier, "verdict": verdict}


def build_record(event: dict) -> dict:
    """Build the log record from a hook event dict. Pure function."""
    tool_input = event.get("tool_input", {}) or {}
    tool_response = event.get("tool_response")
    subagent_type = tool_input.get("subagent_type", "")

    routing = resolve_routing(subagent_type, tool_input, tool_response)
    model = routing["model"]

    metrics = _extract_metrics(tool_response)

    # Cost precedence:
    #   1. direct total_cost_usd reported → use it (already in metrics by _extract_metrics)
    #   2. per-component token split present → exact cost via _TOKEN_RATES
    #   3. only total_tokens present (no split) → blended estimate via _BLENDED_RATES
    if metrics["cost_usd"] is None:
        rates = _model_token_rates(model)
        exact = _exact_cost(metrics, rates)
        if exact is not None:
            metrics["cost_usd"] = exact
            metrics["cost_estimated"] = False
        elif metrics["total_tokens"] is not None:
            blended_rate = _model_rate(model)
            metrics["cost_usd"] = round(metrics["total_tokens"] / 1e6 * blended_rate, 8)
            metrics["cost_estimated"] = True

    return {
        "ts": int(time.time()),
        "uid": f"{os.getpid()}-{time.time_ns()}",
        "session_id": event.get("session_id", ""),
        "tool_name": event.get("tool_name", ""),
        "subagent_type": subagent_type,
        "model": model,
        "model_source": routing["model_source"],
        "tier": routing["tier"],
        "verdict": routing["verdict"],
        "prompt_head": _scrub_secrets((tool_input.get("prompt", "") or ""))[:200],
        "cwd": event.get("cwd", ""),
        "total_tokens": metrics["total_tokens"],
        "input_tokens": metrics["input_tokens"],
        "output_tokens": metrics["output_tokens"],
        "cache_read_tokens": metrics["cache_read_tokens"],
        "cache_creation_tokens": metrics["cache_creation_tokens"],
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

    # Canonical global log; cwd is retained per-record so consumers can do
    # per-project rollups via `group by cwd`.
    log_path = Path.home() / ".claude" / "gearbox-log.jsonl"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the session


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        # --- Real captured shape: full cache split (2026-06, claude-haiku-4-5) ---
        # totalTokens == input(5) + output(1344) + cache_read(23415) + cache_creation(350) = 25114
        # Exact cost (haiku rates, 2026-06):
        #   input:        5 * 1.00 / 1e6 =  0.000005
        #   output:    1344 * 5.00 / 1e6 =  0.00672
        #   cache_read: 23415 * 0.10 / 1e6 = 0.0023415
        #   cache_5m:    350 * 1.25 / 1e6 =  0.0004375
        #   total: 9504.00 / 1e6 = 0.009504   (full USD)
        # Recomputed: (5*1.00 + 1344*5.00 + 23415*0.10 + 350*1.25) / 1e6
        #           = (5 + 6720 + 2341.5 + 437.5) / 1e6 = 9504 / 1e6 = 0.009504
        _EXPECTED_COST_HAIKU_REAL = round(
            (5 * 1.00 + 1344 * 5.00 + 23415 * 0.10 + 350 * 1.25) / 1e6, 8
        )
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
                "totalTokens": 25114,
                "totalToolUseCount": 0,
                "totalDurationMs": 1275,
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 1344,
                    "cache_read_input_tokens": 23415,
                    "cache_creation_input_tokens": 350,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 350,
                        "ephemeral_1h_input_tokens": 0,
                    },
                },
            },
        }
        r3 = build_record(real_shape)
        assert r3["total_tokens"] == 25114, f"expected 25114, got {r3['total_tokens']}"
        assert r3["num_turns"] == 0, f"expected 0, got {r3['num_turns']}"  # falsy-coalescing guard
        assert r3["duration_ms"] == 1275, f"expected 1275, got {r3['duration_ms']}"
        assert r3["cost_estimated"] is False, "expected cost_estimated=False (split present → exact)"
        assert r3["cost_usd"] == _EXPECTED_COST_HAIKU_REAL, \
            f"expected {_EXPECTED_COST_HAIKU_REAL}, got {r3['cost_usd']}"
        assert r3["input_tokens"] == 5, f"expected input_tokens=5, got {r3['input_tokens']}"
        assert r3["output_tokens"] == 1344, f"expected output_tokens=1344, got {r3['output_tokens']}"
        assert r3["cache_read_tokens"] == 23415, f"expected cache_read_tokens=23415, got {r3['cache_read_tokens']}"
        assert r3["cache_creation_tokens"] == 350, f"expected cache_creation_tokens=350, got {r3['cache_creation_tokens']}"

        # --- 1h cache-creation path: exercise ephemeral_1h_input_tokens rate ---
        # haiku: 100 input + 200 output + 0 cache_read + 0 5m + 400 1h cache_write
        # cost = (100*1.00 + 200*5.00 + 0*0.10 + 0*1.25 + 400*2.00) / 1e6
        #      = (100 + 1000 + 0 + 0 + 800) / 1e6 = 1900 / 1e6 = 0.0019
        _EXPECTED_COST_1H = round((100 * 1.00 + 200 * 5.00 + 400 * 2.00) / 1e6, 8)
        shape_1h = {
            "session_id": "s1h",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "gearbox:scout", "model": "claude-haiku-4-5", "prompt": "p"},
            "cwd": "/tmp",
            "tool_response": {
                "totalTokens": 700,
                "totalToolUseCount": 0,
                "totalDurationMs": 500,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 200,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 400,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 400,
                    },
                },
            },
        }
        r_1h = build_record(shape_1h)
        assert r_1h["cost_estimated"] is False, "expected cost_estimated=False for 1h path"
        assert r_1h["cost_usd"] == _EXPECTED_COST_1H, \
            f"expected {_EXPECTED_COST_1H}, got {r_1h['cost_usd']}"

        # --- cost precedence: direct total_cost_usd wins over split ---
        shape_direct = {
            "session_id": "sdirect",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "gearbox:builder", "model": "claude-sonnet-4-6", "prompt": "p"},
            "cwd": "/tmp",
            "tool_response": {
                "totalTokens": 1000,
                "total_cost_usd": 0.00042,
                "usage": {"input_tokens": 500, "output_tokens": 500},
            },
        }
        r_direct = build_record(shape_direct)
        assert r_direct["cost_estimated"] is False, "expected cost_estimated=False for direct cost"
        assert r_direct["cost_usd"] == 0.00042, \
            f"expected 0.00042 (direct), got {r_direct['cost_usd']}"

        # --- resolve_routing: gearbox:builder, no model param → derived ---
        rr1 = resolve_routing("gearbox:builder", {}, None)
        assert rr1["model"] == "sonnet", f"expected sonnet, got {rr1['model']}"
        assert rr1["model_source"] == "derived", f"expected derived, got {rr1['model_source']}"
        assert rr1["tier"] == "T1", f"expected T1, got {rr1['tier']}"
        assert rr1["verdict"] is None, f"expected None verdict, got {rr1['verdict']}"

        # --- resolve_routing: model param present → passed ---
        rr2 = resolve_routing("gearbox:builder", {"model": "haiku"}, None)
        assert rr2["model"] == "haiku", f"expected haiku, got {rr2['model']}"
        assert rr2["model_source"] == "passed", f"expected passed, got {rr2['model_source']}"

        # --- resolve_routing: verifier with VERDICT: REJECT ---
        rr3 = resolve_routing("verifier", {}, "Work done. VERDICT: REJECT — missing tests.")
        assert rr3["verdict"] == "reject", f"expected reject, got {rr3['verdict']}"

        # --- resolve_routing: verifier with VERDICT: APPROVE ---
        rr4 = resolve_routing("gearbox:verifier", {}, {"output": "All checks pass. VERDICT: APPROVE"})
        assert rr4["verdict"] == "approve", f"expected approve, got {rr4['verdict']}"

        # --- resolve_routing: verifier with no verdict ---
        rr5 = resolve_routing("verifier", {}, "Looks good but no explicit verdict here.")
        assert rr5["verdict"] is None, f"expected None, got {rr5['verdict']}"

        # --- resolve_routing: unknown agent, no param → absent ---
        rr6 = resolve_routing("general-purpose", {}, None)
        assert rr6["model"] == "(not passed)", f"expected (not passed), got {rr6['model']}"
        assert rr6["model_source"] == "absent", f"expected absent, got {rr6['model_source']}"
        assert rr6["tier"] is None, f"expected None tier, got {rr6['tier']}"

        # --- resolve_routing: verifier tier is TV (meta-tier, not a routing tier) ---
        rr7 = resolve_routing("verifier", {}, None)
        assert rr7["tier"] == "TV", f"expected TV, got {rr7['tier']}"

        # --- _scrub_secrets: AWS access key id is redacted ---
        aws_text = "Use this key: AKIAIOSFODNN7EXAMPLE to authenticate"
        scrubbed_aws = _scrub_secrets(aws_text)
        assert "[REDACTED]" in scrubbed_aws, f"AWS key not redacted: {scrubbed_aws!r}"
        assert "AKIAIOSFODNN7EXAMPLE" not in scrubbed_aws, f"raw AWS key still present: {scrubbed_aws!r}"

        # --- _scrub_secrets: key=value credential pair is redacted ---
        kv_text = "API_KEY=sk-abc123verylongsecrettoken99999999 and other stuff"
        scrubbed_kv = _scrub_secrets(kv_text)
        assert "[REDACTED]" in scrubbed_kv, f"kv secret not redacted: {scrubbed_kv!r}"
        assert "sk-abc123verylongsecrettoken99999999" not in scrubbed_kv, f"raw kv value still present: {scrubbed_kv!r}"
        assert "API_KEY" in scrubbed_kv, f"key name must be preserved: {scrubbed_kv!r}"

        # --- _scrub_secrets: long hex token is redacted ---
        hex_text = "token: 0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d and done"
        scrubbed_hex = _scrub_secrets(hex_text)
        assert "[REDACTED]" in scrubbed_hex, f"long hex not redacted: {scrubbed_hex!r}"
        assert "0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d" not in scrubbed_hex, f"raw hex still present: {scrubbed_hex!r}"

        # --- _scrub_secrets: PEM private-key block is redacted (real footer, no space) ---
        pem_text = "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAabc\n-----END RSA PRIVATE KEY-----\ndone"
        scrubbed_pem = _scrub_secrets(pem_text)
        assert "[REDACTED]" in scrubbed_pem, f"PEM block not redacted: {scrubbed_pem!r}"
        assert "MIIEpAIBAAKCAQEAabc" not in scrubbed_pem, f"raw PEM body still present: {scrubbed_pem!r}"

        # --- _scrub_secrets: ordinary prose is left unchanged ---
        prose = "Refactor the auth module"
        assert _scrub_secrets(prose) == prose, f"ordinary prose must not be changed: {_scrub_secrets(prose)!r}"

        # --- build_record: prompt_head is scrubbed before storage ---
        event_secret = {
            "session_id": "test-secret",
            "tool_name": "Task",
            "tool_input": {
                "subagent_type": "builder",
                "model": "claude-sonnet",
                "prompt": "Use AWS key AKIAIOSFODNN7EXAMPLE for this task",
            },
            "cwd": "/tmp",
            "tool_response": {"totalTokens": 100, "totalToolUseCount": 1, "totalDurationMs": 500},
        }
        r_secret = build_record(event_secret)
        assert "AKIAIOSFODNN7EXAMPLE" not in r_secret["prompt_head"], \
            f"raw AWS key must not appear in prompt_head: {r_secret['prompt_head']!r}"
        assert "[REDACTED]" in r_secret["prompt_head"], \
            f"prompt_head must contain [REDACTED]: {r_secret['prompt_head']!r}"

        # --- build_record: uid field is present and non-empty ---
        assert "uid" in r_secret, "uid field must be present in record"
        assert r_secret["uid"], "uid must be non-empty"

        # --- build_record: two records built in the same call get distinct uids ---
        event_a = {
            "session_id": "same",
            "tool_name": "Task",
            "tool_input": {"subagent_type": "builder", "model": "claude-sonnet", "prompt": "same"},
            "cwd": "/tmp",
            "tool_response": {"totalTokens": 10, "totalToolUseCount": 0, "totalDurationMs": 100},
        }
        ra = build_record(event_a)
        rb = build_record(event_a)
        assert ra["uid"] != rb["uid"], f"parallel-identical records must have distinct uids: {ra['uid']!r}"

        # Split-usage fallback: no aggregate token field, only input/output → summed
        # input(120) + output(30) = 150; split present → exact cost, cost_estimated=False
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
        assert r4["cost_estimated"] is False, "expected cost_estimated=False (split present)"
        assert r4["cost_usd"] is not None and r4["cost_usd"] > 0, "expected cost_usd > 0"

        # Split-usage fallback: only input side present → input total (not 0, not None)
        # split present → exact cost, cost_estimated=False
        split_input_only = {
            "session_id": "s5",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "gearbox:scout", "model": "claude-haiku-4-5", "prompt": "p"},
            "cwd": "/tmp",
            "tool_response": {"usage": {"input_tokens": 80}},
        }
        r5 = build_record(split_input_only)
        assert r5["total_tokens"] == 80, f"expected 80 (input only), got {r5['total_tokens']}"
        assert r5["cost_estimated"] is False, "expected cost_estimated=False (split present)"

        # Split-usage fallback: neither side present → None (not 0)
        split_none = {
            "session_id": "s6",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "gearbox:scout", "model": "claude-haiku-4-5", "prompt": "p"},
            "cwd": "/tmp",
            "tool_response": {"usage": {}},
        }
        r6 = build_record(split_none)
        assert r6["total_tokens"] is None, f"expected None when both sides absent, got {r6['total_tokens']}"

        # G5: bool must not be coerced to int in _int_or_none
        assert _int_or_none(True) is None, "_int_or_none(True) must return None"
        assert _int_or_none(False) is None, "_int_or_none(False) must return None"
        assert _int_or_none(5) == 5, "_int_or_none(5) must return 5"
        assert _int_or_none(None) is None, "_int_or_none(None) must return None"

        print("selfcheck OK")
        sys.exit(0)

    main()
