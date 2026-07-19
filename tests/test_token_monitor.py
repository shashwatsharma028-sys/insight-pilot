"""
Test Suite 10: Token Monitoring
Verifies session tracking, per-skill attribution, cost estimation,
metadata extraction with fallback, and choke-point integration —
all without real API calls.
"""

import pytest
from unittest.mock import MagicMock

from agent.token_monitor import TokenMonitor, monitor as global_monitor
from utils.llm import RateLimitedLLM


@pytest.fixture
def fresh_monitor():
    m = TokenMonitor()
    return m


@pytest.fixture(autouse=True)
def clean_global_monitor():
    """Existing llm tests fire the wrapper; keep the global monitor clean."""
    global_monitor.reset()
    yield
    global_monitor.reset()


class FakeUsageResponse:
    """Response with provider usage metadata (LangChain standard)."""
    def __init__(self, inp, out, content="x"):
        self.usage_metadata = {"input_tokens": inp, "output_tokens": out,
                               "total_tokens": inp + out}
        self.content = content


class FakePlainResponse:
    """Response without usage metadata — forces estimation."""
    def __init__(self, content):
        self.content = content


class TestRecording:

    def test_totals_accumulate(self, fresh_monitor):
        fresh_monitor.record("planner", 100, 50)
        fresh_monitor.record("code_generator", 200, 80)
        assert fresh_monitor.total_input_tokens == 300
        assert fresh_monitor.total_output_tokens == 130
        assert fresh_monitor.total_tokens == 430
        assert fresh_monitor.call_count == 2

    def test_per_skill_attribution(self, fresh_monitor):
        fresh_monitor.record("planner", 100, 50)
        fresh_monitor.record("planner", 10, 5)
        fresh_monitor.record("business_insights", 30, 20)
        assert fresh_monitor.by_skill["planner"] == {"input": 110, "output": 55, "calls": 2}
        assert fresh_monitor.by_skill["business_insights"]["calls"] == 1

    def test_reset_clears_everything(self, fresh_monitor):
        fresh_monitor.record("planner", 100, 50)
        fresh_monitor.reset()
        assert fresh_monitor.total_tokens == 0
        assert fresh_monitor.by_skill == {}
        assert fresh_monitor.call_count == 0


class TestExtraction:

    def test_provider_metadata_used_when_present(self, fresh_monitor):
        resp = FakeUsageResponse(1234, 567)
        fresh_monitor.record_from_response("planner", resp, ("prompt",))
        assert fresh_monitor.total_input_tokens == 1234
        assert fresh_monitor.total_output_tokens == 567
        assert fresh_monitor.estimated_calls == 0

    def test_fallback_estimation_when_metadata_absent(self, fresh_monitor):
        resp = FakePlainResponse("x" * 400)          # ~100 output tokens
        fresh_monitor.record_from_response("planner", resp, ("p" * 800,))  # ~200 input
        assert fresh_monitor.estimated_calls == 1
        assert fresh_monitor.total_output_tokens == pytest.approx(100, abs=5)
        assert fresh_monitor.total_input_tokens > 0

    def test_extraction_never_raises(self, fresh_monitor):
        fresh_monitor.record_from_response("planner", None, None)  # Worst case
        assert fresh_monitor.call_count <= 1          # Either recorded or skipped, no crash


class TestCostEstimation:

    def test_cost_math(self, fresh_monitor, monkeypatch):
        monkeypatch.setenv("TOKEN_PRICE_INPUT_PER_M", "1.0")
        monkeypatch.setenv("TOKEN_PRICE_OUTPUT_PER_M", "2.0")
        fresh_monitor.record("planner", 500_000, 250_000)
        # 0.5M * $1 + 0.25M * $2 = $1.00
        assert fresh_monitor.estimated_cost_usd == pytest.approx(1.00)

    def test_zero_usage_zero_cost(self, fresh_monitor):
        assert fresh_monitor.estimated_cost_usd == 0.0


class TestSummary:

    def test_summary_shape(self, fresh_monitor):
        fresh_monitor.record("planner", 100, 50)
        s = fresh_monitor.summary()
        for key in ["calls", "input_tokens", "output_tokens", "total_tokens",
                    "estimated_cost_usd", "by_skill", "session_started"]:
            assert key in s
        assert s["by_skill"]["planner"]["total"] == 150

    def test_by_skill_sorted_by_usage(self, fresh_monitor):
        fresh_monitor.record("small", 10, 5)
        fresh_monitor.record("big", 1000, 500)
        skills = list(fresh_monitor.summary()["by_skill"].keys())
        assert skills[0] == "big"


class TestChokePointIntegration:
    """RateLimitedLLM must record every successful call to the monitor,
    attributed to its skill — and existing behavior stays intact."""

    def test_successful_call_recorded_with_skill(self):
        inner = MagicMock()
        inner.invoke.return_value = FakeUsageResponse(300, 120)
        wrapped = RateLimitedLLM(inner, skill="planner")
        wrapped.invoke("prompt")
        assert global_monitor.total_tokens == 420
        assert "planner" in global_monitor.by_skill

    def test_multiple_skills_tracked_separately(self):
        for skill, tokens in [("planner", (100, 40)), ("code_generator", (500, 200))]:
            inner = MagicMock()
            inner.invoke.return_value = FakeUsageResponse(*tokens)
            RateLimitedLLM(inner, skill=skill).invoke("p")
        assert set(global_monitor.by_skill) == {"planner", "code_generator"}
        assert global_monitor.by_skill["code_generator"]["input"] == 500

    def test_failed_call_not_recorded(self):
        inner = MagicMock()
        inner.invoke.side_effect = ValueError("boom")
        wrapped = RateLimitedLLM(inner, skill="planner")
        with pytest.raises(ValueError):
            wrapped.invoke("p")
        assert global_monitor.call_count == 0

    def test_default_skill_is_unknown(self):
        inner = MagicMock()
        inner.invoke.return_value = FakeUsageResponse(10, 5)
        RateLimitedLLM(inner).invoke("p")
        assert "unknown" in global_monitor.by_skill

    def test_return_value_unchanged(self):
        """Monitoring must not alter what callers receive."""
        inner = MagicMock()
        resp = FakeUsageResponse(10, 5, content="the answer")
        inner.invoke.return_value = resp
        assert RateLimitedLLM(inner, skill="x").invoke("p") is resp
