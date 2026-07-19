"""
Context Manager
Tracks how much of the model's context window the conversation is consuming,
and compresses older context when usage crosses the threshold (80%).

What counts as context here: everything assembled into follow-up prompts —
dataset profile, completed-task interpretations, business insights, and the
conversation history. History is the unbounded part; it's what gets
compressed.

What is ALWAYS preserved (never compressed away):
- Critical facts: dataset profile, task interpretations, business insights
- Rules: loaded fresh from /rules each call (disk, not context)
- Skills: loaded fresh from /skills each call (disk, not context)
- Lessons: loaded fresh from /lessons each call (disk, not context)
- The most recent conversation turns (the live thread)

Compression is DETERMINISTIC structured extraction — not an LLM call.
Older turns are distilled into a compact summary block (who asked what,
key answer fragments). This is honest, free, instant, and cannot
hallucinate. The summary then stands in for the full older history.
"""

import os
from datetime import datetime

CHARS_PER_TOKEN = 4          # Consistent with token_monitor's estimator
DEFAULT_MAX_CONTEXT = 32_768  # Practical budget; override via env
COMPRESSION_THRESHOLD = 0.80  # Warn + compress above this
RECENT_TURNS_KEPT = 4         # Live thread always kept verbatim


def max_context_tokens() -> int:
    return int(os.environ.get("CONTEXT_WINDOW_TOKENS", DEFAULT_MAX_CONTEXT))


def estimate_tokens(text: str) -> int:
    return len(str(text)) // CHARS_PER_TOKEN


# ── Measurement ──────────────────────────────────────────────────────────────

def measure_context(state: dict) -> dict:
    """
    Measure the context that would be assembled for the next follow-up call.
    Returns {tokens, max_tokens, pct, breakdown}.
    """
    profile = state.get("df_summary") or ""
    interpretations = " ".join(
        t.get("interpretation") or "" for t in state.get("completed_tasks", [])
    )
    insights = " ".join(state.get("business_insights", []))
    summary = state.get("context_summary") or ""
    history = " ".join(
        turn.get("message", "") for turn in state.get("conversation_history", [])
    )

    breakdown = {
        "dataset_profile": estimate_tokens(profile),
        "interpretations": estimate_tokens(interpretations),
        "insights": estimate_tokens(insights),
        "context_summary": estimate_tokens(summary),
        "conversation_history": estimate_tokens(history),
    }
    tokens = sum(breakdown.values())
    max_t = max_context_tokens()

    return {
        "tokens": tokens,
        "max_tokens": max_t,
        "pct": round(tokens / max_t, 4) if max_t else 0.0,
        "breakdown": breakdown,
    }


def needs_compression(state: dict) -> bool:
    return measure_context(state)["pct"] > COMPRESSION_THRESHOLD


# ── Compression (deterministic) ─────────────────────────────────────────────

def _distill_turn(turn: dict) -> str:
    """One compact line per turn: role + leading fragment of the message."""
    role = turn.get("role", "?")
    msg = str(turn.get("message", "")).strip().replace("\n", " ")
    fragment = msg[:140] + ("…" if len(msg) > 140 else "")
    return f"{role}: {fragment}"


def compress_context(state: dict) -> dict:
    """
    Compress older conversation turns into a summary block, keeping the
    most recent RECENT_TURNS_KEPT turns verbatim. Critical facts
    (profile, interpretations, insights) are untouched. Any previous
    summary is folded into the new one (summaries of summaries stay
    bounded because each distilled line is capped).

    Returns a NEW state (immutable-style, per coding rules).
    """
    history = state.get("conversation_history", [])
    if len(history) <= RECENT_TURNS_KEPT:
        return {**state}   # Nothing old enough to compress

    older = history[:-RECENT_TURNS_KEPT]
    recent = history[-RECENT_TURNS_KEPT:]

    distilled = [_distill_turn(t) for t in older]
    previous_summary = state.get("context_summary") or ""

    # Build the summary within a budget: header ALWAYS kept, then distilled
    # lines newest-first until the budget is spent, then any prior summary.
    summary_budget_chars = (max_context_tokens() // 10) * CHARS_PER_TOKEN
    header = (
        f"[Compressed {len(older)} earlier turn(s) at "
        f"{datetime.now().strftime('%H:%M:%S')}]"
    )
    kept_lines = []
    used = len(header)
    for line in reversed(distilled):              # Newest older-turns first
        if used + len(line) > summary_budget_chars:
            break
        kept_lines.append(line)
        used += len(line)
    kept_lines.reverse()                           # Restore chronological order

    parts = [header] + kept_lines
    if previous_summary and used + len(previous_summary) <= summary_budget_chars:
        parts = [previous_summary] + parts        # Fold prior summary if it fits
    if len(kept_lines) < len(distilled):
        parts.insert(1, f"…({len(distilled) - len(kept_lines)} oldest turn(s) elided)")
    summary_text = "\n".join(parts)

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "context_manager",
        "action": f"Context >{COMPRESSION_THRESHOLD:.0%} — compressed {len(older)} older turn(s) into summary",
        "status": "warning",
        "detail": f"Kept {len(recent)} recent turns verbatim; critical facts, rules, skills, and lessons preserved",
    }

    return {
        **state,
        "context_summary": summary_text,
        "conversation_history": recent,
        "execution_log": state.get("execution_log", []) + [log_entry],
        "context_warning": True,
    }


def manage_context(state: dict) -> dict:
    """
    The single entry point: measure, and compress if over threshold.
    Called before assembling any follow-up prompt.
    """
    if needs_compression(state):
        return compress_context(state)
    return {**state, "context_warning": False}
