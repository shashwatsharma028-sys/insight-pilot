"""
Test Suite 4: Agent Logic (Planner parsing + Executor retry routing)
LLM calls are mocked — we test OUR logic, not Gemini's.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from agent.nodes.planner import _parse_plan, planner_node
from agent.nodes.executor import executor_node
from agent.nodes.interpreter import advance_task_node
from agent.state import AnalysisStatus, ErrorType


VALID_PLAN_JSON = json.dumps([
    {"id": "a1", "title": "Descriptive Stats", "description": "Summary",
     "analysis_type": "descriptive", "priority": 1, "status": "pending"},
    {"id": "a2", "title": "Correlations", "description": "Relationships",
     "analysis_type": "correlation", "priority": 2, "status": "pending"},
])


class TestPlanParsing:
    """_parse_plan must survive every way an LLM mangles JSON output."""

    def test_clean_json(self):
        plan = _parse_plan(VALID_PLAN_JSON)
        assert len(plan) == 2
        assert plan[0]["title"] == "Descriptive Stats"

    def test_json_wrapped_in_markdown_fences(self):
        wrapped = f"```json\n{VALID_PLAN_JSON}\n```"
        plan = _parse_plan(wrapped)
        assert len(plan) == 2

    def test_json_wrapped_in_bare_fences(self):
        wrapped = f"```\n{VALID_PLAN_JSON}\n```"
        plan = _parse_plan(wrapped)
        assert len(plan) == 2

    def test_missing_fields_get_defaults(self):
        minimal = json.dumps([{"title": "Only Title"}])
        plan = _parse_plan(minimal)
        assert plan[0]["retry_count"] == 0
        assert plan[0]["status"] == AnalysisStatus.PENDING
        assert plan[0]["generated_code"] is None

    def test_tasks_sorted_by_priority(self):
        unsorted = json.dumps([
            {"id": "low", "title": "Low", "priority": 5},
            {"id": "high", "title": "High", "priority": 1},
        ])
        plan = _parse_plan(unsorted)
        assert plan[0]["id"] == "high"

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            _parse_plan("this is not json at all")


class TestPlannerNode:
    """planner_node with a mocked LLM."""

    @patch("agent.nodes.planner.get_llm")
    def test_generates_plan_and_awaits_approval(self, mock_get_llm, base_state, fake_response):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response(VALID_PLAN_JSON)
        mock_get_llm.return_value = mock_llm

        state = {**base_state, "df_summary": "fake summary", "safe_mode": True}
        result = planner_node(state)

        assert len(result["analysis_plan"]) == 2
        assert result["plan_approved"] is False
        assert result["next_action"] == "await_approval"

    @patch("agent.nodes.planner.get_llm")
    def test_llm_failure_routes_to_error(self, mock_get_llm, base_state):
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = Exception("API down")
        mock_get_llm.return_value = mock_llm

        state = {**base_state, "df_summary": "fake summary"}
        result = planner_node(state)
        assert result["next_action"] == "error"


class TestExecutorRetryRouting:
    """The heart of self-correction: failed tasks must route back to
    code generation with error info attached, until max_retries."""

    def test_successful_execution_routes_to_interpret(self, base_state, sample_task):
        task = {**sample_task, "generated_code": "print('works:', len(df))"}
        state = {**base_state, "analysis_plan": [task]}
        result = executor_node(state)

        assert result["next_action"] == "interpret"
        assert result["analysis_plan"][0]["status"] == AnalysisStatus.COMPLETED
        assert "works: 20" in result["analysis_plan"][0]["execution_result"]

    def test_failed_execution_routes_to_retry(self, base_state, sample_task):
        task = {**sample_task, "generated_code": "print(df['no_such_col'])"}
        state = {**base_state, "analysis_plan": [task]}
        result = executor_node(state)

        assert result["next_action"] == "generate_code"      # Back for a retry
        updated = result["analysis_plan"][0]
        assert updated["retry_count"] == 1
        assert updated["error_type"] == ErrorType.DATA
        assert updated["generated_code"] is None             # Forces regeneration

    def test_max_retries_exhausted_gives_up(self, base_state, sample_task):
        task = {**sample_task,
                "generated_code": "print(df['no_such_col'])",
                "retry_count": 2}                            # max_retries is 2 in fixture
        state = {**base_state, "analysis_plan": [task]}
        result = executor_node(state)

        assert result["next_action"] == "next_task"          # Moved on, not stuck
        assert result["analysis_plan"][0]["status"] == AnalysisStatus.FAILED
        assert len(result["failed_tasks"]) == 1

    def test_advance_task_increments_index(self, base_state):
        state = {**base_state, "current_task_index": 0}
        result = advance_task_node(state)
        assert result["current_task_index"] == 1
