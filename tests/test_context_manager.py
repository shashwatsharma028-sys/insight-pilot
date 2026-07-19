"""
Test Suite 11: Context Manager
Verifies measurement, the 80% threshold, deterministic compression that
preserves critical facts and recent turns, bounded re-compression, and
integration into the follow-up flow.
"""

import pytest
from unittest.mock import patch, MagicMock

from agent.context_manager import (
    estimate_tokens, measure_context, needs_compression,
    compress_context, manage_context, RECENT_TURNS_KEPT,
)


def make_turn(role, message):
    return {"role": role, "message": message,
            "timestamp": "2026-07-19T12:00:00", "related_task_id": None}


@pytest.fixture
def small_window(monkeypatch):
    """Shrink the window so tests can cross 80% cheaply."""
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "1000")   # 1k tokens


class TestMeasurement:

    def test_estimate_tokens(self):
        assert estimate_tokens("x" * 400) == 100

    def test_measure_counts_all_components(self, base_state):
        state = {
            **base_state,
            "df_summary": "p" * 400,                       # 100 tok
            "business_insights": ["i" * 200],              # 50 tok
            "conversation_history": [make_turn("user", "h" * 400)],  # 100 tok
        }
        m = measure_context(state)
        assert m["breakdown"]["dataset_profile"] == 100
        assert m["breakdown"]["insights"] == 50
        assert m["breakdown"]["conversation_history"] == 100
        assert m["tokens"] == 250

    def test_pct_against_window(self, base_state, small_window):
        state = {**base_state, "df_summary": "x" * 2000}   # 500 tok of 1000
        assert measure_context(state)["pct"] == pytest.approx(0.5)

    def test_empty_state_zero(self, base_state):
        m = measure_context(base_state)
        assert m["tokens"] == 0 and m["pct"] == 0.0


class TestThreshold:

    def test_below_threshold_no_compression_needed(self, base_state, small_window):
        state = {**base_state, "df_summary": "x" * 2000}   # 50%
        assert needs_compression(state) is False

    def test_above_threshold_detected(self, base_state, small_window):
        state = {**base_state,
                 "conversation_history": [make_turn("user", "x" * 4000)]}  # 100%
        assert needs_compression(state) is True


class TestCompression:

    def _big_history_state(self, base_state, n_turns=10):
        return {
            **base_state,
            "df_summary": "CRITICAL_PROFILE " * 20,
            "business_insights": ["CRITICAL_INSIGHT about sales"],
            "completed_tasks": [{"interpretation": "CRITICAL_INTERPRETATION", "status": "completed"}],
            "conversation_history": [
                make_turn("user" if i % 2 == 0 else "agent", f"turn-{i} " + "x" * 300)
                for i in range(n_turns)
            ],
        }

    def test_recent_turns_kept_verbatim(self, base_state, small_window):
        state = self._big_history_state(base_state)
        out = compress_context(state)
        assert len(out["conversation_history"]) == RECENT_TURNS_KEPT
        assert out["conversation_history"][-1]["message"].startswith("turn-9")

    def test_older_turns_summarized(self, base_state, small_window):
        state = self._big_history_state(base_state)
        out = compress_context(state)
        assert out["context_summary"]
        assert "Compressed 6 earlier turn(s)" in out["context_summary"]  # Header always kept
        assert "turn-5" in out["context_summary"]       # Newest older-turn survives
        assert "oldest turn(s) elided" in out["context_summary"]  # Honest elision marker

    def test_all_turns_kept_when_budget_allows(self, base_state, monkeypatch):
        """With a roomy window, nothing is elided — even turn-0 survives."""
        monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "50000")
        state = self._big_history_state(base_state)
        out = compress_context(state)
        assert "turn-0" in out["context_summary"]
        assert "elided" not in out["context_summary"]

    def test_critical_facts_untouched(self, base_state, small_window):
        state = self._big_history_state(base_state)
        out = compress_context(state)
        assert out["df_summary"] == state["df_summary"]
        assert out["business_insights"] == state["business_insights"]
        assert out["completed_tasks"] == state["completed_tasks"]

    def test_compression_reduces_tokens(self, base_state, small_window):
        state = self._big_history_state(base_state, n_turns=12)
        before = measure_context(state)["tokens"]
        after = measure_context(compress_context(state))["tokens"]
        assert after < before

    def test_warning_logged(self, base_state, small_window):
        out = compress_context(self._big_history_state(base_state))
        log = out["execution_log"][-1]
        assert log["node"] == "context_manager"
        assert log["status"] == "warning"
        assert out["context_warning"] is True

    def test_short_history_not_compressed(self, base_state):
        state = {**base_state,
                 "conversation_history": [make_turn("user", "hi")] * 3}
        out = compress_context(state)
        assert len(out["conversation_history"]) == 3
        assert not out.get("context_summary")

    def test_recompression_folds_previous_summary(self, base_state, small_window):
        state = self._big_history_state(base_state)
        once = compress_context(state)
        # Grow history again and recompress
        again = {**once, "conversation_history":
                 once["conversation_history"] + [
                     make_turn("user", f"new-{i} " + "y" * 300) for i in range(6)
                 ]}
        twice = compress_context(again)
        assert "turn-0" in twice["context_summary"] or "…" in twice["context_summary"]
        assert len(twice["conversation_history"]) == RECENT_TURNS_KEPT

    def test_summary_stays_bounded(self, base_state, small_window):
        """Repeated compressions must not grow the summary unboundedly."""
        state = self._big_history_state(base_state)
        for _ in range(5):
            state = compress_context(state)
            state["conversation_history"] = state["conversation_history"] + [
                make_turn("user", "z" * 400) for _ in range(6)
            ]
        max_summary_tokens = 1000 // 10 + 5   # window/10 + slack
        assert estimate_tokens(state["context_summary"]) <= max_summary_tokens


class TestManageEntryPoint:

    def test_under_threshold_passthrough(self, base_state):
        out = manage_context(base_state)
        assert out["context_warning"] is False
        assert out.get("conversation_history") == base_state["conversation_history"]

    def test_over_threshold_compresses(self, base_state, small_window):
        state = {**base_state, "conversation_history": [
            make_turn("user", "x" * 500) for _ in range(10)
        ]}
        out = manage_context(state)
        assert out["context_warning"] is True
        assert len(out["conversation_history"]) == RECENT_TURNS_KEPT


class TestFollowupIntegration:
    """The follow-up node must manage context before calling the LLM and
    include the summary block in its prompt."""

    @patch("agent.memory.conversation.get_llm")
    def test_summary_included_in_prompt(self, mock_get_llm, base_state, fake_response, small_window):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response("answer")
        mock_get_llm.return_value = mock_llm

        state = {
            **base_state,
            "current_user_message": "what changed?",
            "context_summary": "UNIQUE_SUMMARY_MARKER earlier discussion",
            "conversation_history": [make_turn("user", "old q")],
        }
        from agent.memory.conversation import handle_followup_node
        handle_followup_node(state)

        sent = mock_llm.invoke.call_args[0][0]
        all_content = " ".join(m.content for m in sent)
        assert "UNIQUE_SUMMARY_MARKER" in all_content
        assert "[SUMMARY OF EARLIER CONVERSATION]" in all_content

    @patch("agent.memory.conversation.get_llm")
    def test_big_history_triggers_compression_in_flow(self, mock_get_llm, base_state, fake_response, small_window):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response("answer")
        mock_get_llm.return_value = mock_llm

        state = {
            **base_state,
            "current_user_message": "and now?",
            "conversation_history": [
                make_turn("user", "x" * 500) for _ in range(10)
            ],
        }
        from agent.memory.conversation import handle_followup_node
        result = handle_followup_node(state)

        assert any(e.get("node") == "context_manager"
                   for e in result["execution_log"])
