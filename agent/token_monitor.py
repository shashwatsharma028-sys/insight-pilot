"""
Token Monitor
Session-scoped tracking of LLM token usage and estimated cost, attributed
per skill (planner, code_generator, business_insights, followup_chat).

How counts are obtained:
- Primary: the provider's own usage metadata on each response
  (LangChain standard `response.usage_metadata`).
- Fallback: a character-based estimate (~4 chars/token) when metadata is
  absent, flagged as estimated.

Cost is an ESTIMATE: prices default to Gemini flash-lite-class rates and
are configurable via env (TOKEN_PRICE_INPUT_PER_M / TOKEN_PRICE_OUTPUT_PER_M)
since actual pricing varies by model and changes over time.

Integration: recording happens inside utils/llm.RateLimitedLLM.invoke —
the single choke point all LLM calls already pass through (llm_usage
rules 11). Nodes only declare their skill name via get_llm(skill=...).
"""

import os
from datetime import datetime
from threading import Lock

# Default prices per 1M tokens (USD) — flash-lite-class; override via env
DEFAULT_INPUT_PRICE_PER_M = 0.10
DEFAULT_OUTPUT_PRICE_PER_M = 0.40

CHARS_PER_TOKEN_ESTIMATE = 4  # Rough heuristic for fallback estimation


class TokenMonitor:
    """Accumulates token usage for the current session."""

    def __init__(self):
        self._lock = Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", Lock()):
            self.session_started = datetime.now().isoformat()
            self.total_input_tokens = 0
            self.total_output_tokens = 0
            self.call_count = 0
            self.estimated_calls = 0          # Calls where we had to estimate
            self.by_skill = {}                # skill -> {input, output, calls}

    # ── Recording ────────────────────────────────────────────────────────

    def record(self, skill: str, input_tokens: int, output_tokens: int,
               estimated: bool = False) -> None:
        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            self.call_count += 1
            if estimated:
                self.estimated_calls += 1

            bucket = self.by_skill.setdefault(
                skill, {"input": 0, "output": 0, "calls": 0}
            )
            bucket["input"] += input_tokens
            bucket["output"] += output_tokens
            bucket["calls"] += 1

    def record_from_response(self, skill: str, response, prompt_args) -> None:
        """
        Extract usage from a provider response; fall back to estimation.
        Never raises — monitoring must not break the agent.
        """
        try:
            usage = getattr(response, "usage_metadata", None)
            if isinstance(usage, dict) and "input_tokens" in usage:
                self.record(
                    skill,
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    estimated=False,
                )
                return

            # Fallback: estimate from character lengths
            prompt_chars = len(str(prompt_args))
            content = getattr(response, "content", "")
            output_chars = len(str(content))
            self.record(
                skill,
                prompt_chars // CHARS_PER_TOKEN_ESTIMATE,
                output_chars // CHARS_PER_TOKEN_ESTIMATE,
                estimated=True,
            )
        except Exception:
            pass  # Monitoring is best-effort; never disrupt the run

    # ── Reading ──────────────────────────────────────────────────────────

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def estimated_cost_usd(self) -> float:
        in_price = float(os.environ.get(
            "TOKEN_PRICE_INPUT_PER_M", DEFAULT_INPUT_PRICE_PER_M))
        out_price = float(os.environ.get(
            "TOKEN_PRICE_OUTPUT_PER_M", DEFAULT_OUTPUT_PRICE_PER_M))
        return (
            self.total_input_tokens / 1_000_000 * in_price
            + self.total_output_tokens / 1_000_000 * out_price
        )

    def summary(self) -> dict:
        """Compact snapshot for display."""
        return {
            "session_started": self.session_started,
            "calls": self.call_count,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "estimated_calls": self.estimated_calls,
            "by_skill": {
                skill: {**data, "total": data["input"] + data["output"]}
                for skill, data in sorted(
                    self.by_skill.items(),
                    key=lambda kv: -(kv[1]["input"] + kv[1]["output"]),
                )
            },
        }


# Session-scoped singleton — one per Python process (= one Streamlit session
# server-side). The UI reads this; the LLM wrapper writes to it.
monitor = TokenMonitor()
