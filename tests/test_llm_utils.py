"""
Test Suite 2: LLM Utilities
Tests get_text() response normalization and the RateLimitedLLM retry wrapper —
all WITHOUT real API calls (mocked), so tests are free and instant.
"""

import pytest
from unittest.mock import MagicMock, patch
from utils.llm import get_text, RateLimitedLLM


class TestGetText:
    """Gemini models return either str or list-of-parts; get_text must handle all."""

    def test_plain_string(self, fake_response):
        assert get_text(fake_response("hello world")) == "hello world"

    def test_list_of_strings(self, fake_response):
        assert get_text(fake_response(["hello", " ", "world"])) == "hello world"

    def test_list_of_dicts_gemini_format(self, fake_response):
        resp = fake_response([{"text": "part one. "}, {"text": "part two."}])
        assert get_text(resp) == "part one. part two."

    def test_mixed_list(self, fake_response):
        resp = fake_response([{"text": "json: "}, "raw_string"])
        assert get_text(resp) == "json: raw_string"

    def test_empty_list(self, fake_response):
        assert get_text(fake_response([])) == ""

    def test_dict_without_text_key(self, fake_response):
        """Dicts missing 'text' should contribute empty string, not crash."""
        resp = fake_response([{"type": "thinking"}, {"text": "answer"}])
        assert get_text(resp) == "answer"


class TestRateLimitedLLM:
    """The retry wrapper must wait-and-retry on per-minute 429s,
    fail fast on daily quota, and pass through other errors untouched."""

    def test_success_passthrough(self):
        inner = MagicMock()
        inner.invoke.return_value = "ok"
        wrapped = RateLimitedLLM(inner)
        assert wrapped.invoke("prompt") == "ok"
        inner.invoke.assert_called_once()

    def test_non_rate_limit_error_raises_immediately(self):
        inner = MagicMock()
        inner.invoke.side_effect = ValueError("some other problem")
        wrapped = RateLimitedLLM(inner)
        with pytest.raises(ValueError):
            wrapped.invoke("prompt")
        assert inner.invoke.call_count == 1  # No retries for non-429

    @patch("utils.llm.time.sleep")  # Don't actually sleep in tests
    def test_per_minute_429_retries_then_succeeds(self, mock_sleep):
        inner = MagicMock()
        inner.invoke.side_effect = [
            Exception("429 RESOURCE_EXHAUSTED ... retry in 5s"),
            "recovered"
        ]
        wrapped = RateLimitedLLM(inner)
        result = wrapped.invoke("prompt")
        assert result == "recovered"
        assert inner.invoke.call_count == 2
        mock_sleep.assert_called_once()  # It waited before retrying

    @patch("utils.llm.time.sleep")
    def test_daily_quota_fails_fast_with_clear_message(self, mock_sleep):
        inner = MagicMock()
        inner.invoke.side_effect = Exception(
            "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"
        )
        wrapped = RateLimitedLLM(inner)
        with pytest.raises(RuntimeError, match="DAILY quota exhausted"):
            wrapped.invoke("prompt")
        mock_sleep.assert_not_called()  # Waiting is pointless for daily quota

    @patch("utils.llm.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep):
        inner = MagicMock()
        inner.invoke.side_effect = Exception("429 RESOURCE_EXHAUSTED retry in 1s")
        wrapped = RateLimitedLLM(inner)
        with pytest.raises(Exception, match="429"):
            wrapped.invoke("prompt")
        # 1 initial + MAX_RATE_LIMIT_RETRIES retries
        from utils.llm import MAX_RATE_LIMIT_RETRIES
        assert inner.invoke.call_count == MAX_RATE_LIMIT_RETRIES + 1
