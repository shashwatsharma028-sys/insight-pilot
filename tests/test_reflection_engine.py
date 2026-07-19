"""
Test Suite 9: Reflection Engine
Verifies lesson generation from execution facts, JSON storage, relevance
ranking, prompt injection into planning, and the after-every-task hook.
Uses LESSONS_DIR env override so tests never touch the real lesson store.
"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from agent.reflection_engine import (
    generate_lessons_for_task, generate_lesson_for_user_correction,
    save_lessons, load_all_lessons, load_relevant_lessons,
    format_lessons_for_prompt,
)
from agent.state import AnalysisStatus, ErrorType
from agent.nodes.interpreter import advance_task_node


@pytest.fixture(autouse=True)
def isolated_lessons_dir(tmp_path, monkeypatch):
    """Every test gets its own empty lessons store."""
    monkeypatch.setenv("LESSONS_DIR", str(tmp_path / "lessons"))
    yield


class TestLessonGeneration:

    def test_failed_task_generates_failure_lesson(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.FAILED,
                "retry_count": 3, "error_type": ErrorType.DATA,
                "error_message": "KeyError: 'salse'"}
        lessons = generate_lessons_for_task(task, "Sales Data")
        assert len(lessons) == 1
        l = lessons[0]
        assert l["kind"] == "failure"
        assert "failed after 3" in l["issue"]
        assert "KeyError" in l["cause"]
        assert "column names" in l["fix"]          # DATA-specific fix
        assert l["confidence"] == 0.9
        assert l["timestamp"] and l["dataset"] == "Sales Data"

    def test_retried_success_generates_retry_lesson(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.COMPLETED,
                "retry_count": 2, "error_type": ErrorType.SYNTAX,
                "confidence_score": 0.9}
        lessons = generate_lessons_for_task(task, "Sales Data")
        assert len(lessons) == 1
        assert lessons[0]["kind"] == "retry"
        assert "2 retry" in lessons[0]["issue"]

    def test_low_confidence_generates_weak_output_lesson(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.COMPLETED,
                "retry_count": 0, "confidence_score": 0.3}
        lessons = generate_lessons_for_task(task, "Sales Data")
        assert len(lessons) == 1
        assert lessons[0]["kind"] == "weak_output"

    def test_clean_task_generates_no_lessons(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.COMPLETED,
                "retry_count": 0, "confidence_score": 0.95}
        assert generate_lessons_for_task(task, "Sales Data") == []

    def test_retried_and_weak_generates_two_lessons(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.COMPLETED,
                "retry_count": 1, "confidence_score": 0.4}
        lessons = generate_lessons_for_task(task, "Sales Data")
        assert {l["kind"] for l in lessons} == {"retry", "weak_output"}

    def test_lesson_has_all_required_fields(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.FAILED,
                "retry_count": 1, "error_type": ErrorType.RUNTIME}
        lesson = generate_lessons_for_task(task, "D")[0]
        for field in ["issue", "cause", "fix", "confidence", "timestamp"]:
            assert field in lesson

    def test_user_correction_lesson(self):
        lesson = generate_lesson_for_user_correction("Skip task 3, add churn analysis", "Sales Data")
        assert lesson["kind"] == "user_correction"
        assert "Skip task 3" in lesson["cause"]
        assert lesson["confidence"] == 0.8


class TestLessonStorage:

    def test_save_and_load_roundtrip(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.FAILED,
                "retry_count": 1, "error_type": ErrorType.DATA}
        lessons = generate_lessons_for_task(task, "DS1")
        total = save_lessons(lessons)
        assert total == 1
        loaded = load_all_lessons()
        assert loaded[0]["issue"] == lessons[0]["issue"]

    def test_lessons_accumulate(self, sample_task):
        task = {**sample_task, "status": AnalysisStatus.FAILED,
                "retry_count": 1, "error_type": ErrorType.DATA}
        save_lessons(generate_lessons_for_task(task, "A"))
        total = save_lessons(generate_lessons_for_task(task, "B"))
        assert total == 2

    def test_stored_as_valid_json(self, sample_task):
        from agent.reflection_engine import _lessons_path
        task = {**sample_task, "status": AnalysisStatus.FAILED,
                "retry_count": 1, "error_type": ErrorType.DATA}
        save_lessons(generate_lessons_for_task(task, "A"))
        with open(_lessons_path()) as f:
            data = json.load(f)               # Raises if not valid JSON
        assert isinstance(data, list)

    def test_empty_store_loads_empty(self):
        assert load_all_lessons() == []

    def test_save_nothing_is_safe(self):
        assert save_lessons([]) == 0


class TestRelevanceRanking:

    def _seed(self, dataset, atype, when):
        save_lessons([{
            "timestamp": when, "dataset": dataset, "task_title": "t",
            "analysis_type": atype, "issue": f"i-{dataset}-{atype}",
            "cause": "c", "fix": "f", "confidence": 0.7, "kind": "retry",
        }])

    def test_same_dataset_ranks_first(self):
        self._seed("Other", "trend", "2026-07-01T00:00:00")
        self._seed("Sales", "trend", "2026-06-01T00:00:00")
        top = load_relevant_lessons(dataset_name="Sales")[0]
        assert top["dataset"] == "Sales"

    def test_limit_respected(self):
        for i in range(10):
            self._seed("Sales", "trend", f"2026-07-{i+1:02d}T00:00:00")
        assert len(load_relevant_lessons(dataset_name="Sales", limit=5)) == 5

    def test_no_lessons_returns_empty(self):
        assert load_relevant_lessons(dataset_name="X") == []


class TestPromptFormatting:

    def test_empty_lessons_empty_string(self):
        assert format_lessons_for_prompt([]) == ""

    def test_formatted_block_contains_lesson_content(self):
        block = format_lessons_for_prompt([{
            "kind": "failure", "issue": "Task X failed.", "cause": "KeyError",
            "fix": "Check columns.", "confidence": 0.9,
        }])
        assert "LESSONS FROM PAST RUNS" in block
        assert "Task X failed." in block and "Check columns." in block


class TestAfterEveryTaskHook:
    """advance_task_node must reflect on the finished task before advancing."""

    def test_failed_task_recorded_on_advance(self, base_state, sample_task):
        failed = {**sample_task, "status": AnalysisStatus.FAILED,
                  "retry_count": 3, "error_type": ErrorType.DATA,
                  "error_message": "KeyError"}
        state = {**base_state, "analysis_plan": [failed], "current_task_index": 0}
        result = advance_task_node(state)
        assert result["current_task_index"] == 1
        assert len(load_all_lessons()) == 1
        assert any(e.get("node") == "reflection" for e in result["execution_log"])

    def test_clean_task_records_nothing(self, base_state, sample_task):
        clean = {**sample_task, "status": AnalysisStatus.COMPLETED,
                 "retry_count": 0, "confidence_score": 0.95}
        state = {**base_state, "analysis_plan": [clean], "current_task_index": 0}
        result = advance_task_node(state)
        assert result["current_task_index"] == 1
        assert load_all_lessons() == []

    def test_empty_plan_still_advances_safely(self, base_state):
        result = advance_task_node(base_state)   # No tasks at all
        assert result["current_task_index"] == 1


class TestPlannerUsesLessons:

    @patch("agent.nodes.planner.get_llm")
    def test_lessons_injected_into_planning_prompt(self, mock_get_llm, base_state, fake_response):
        save_lessons([{
            "timestamp": "2026-07-19T00:00:00", "dataset": "Test Dataset",
            "task_title": "t", "analysis_type": "trend",
            "issue": "UNIQUE_LESSON_MARKER failed before.", "cause": "c",
            "fix": "Do it simpler.", "confidence": 0.9, "kind": "failure",
        }])
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response(json.dumps([
            {"id": "a", "title": "T", "priority": 1}
        ]))
        mock_get_llm.return_value = mock_llm

        from agent.nodes.planner import planner_node
        planner_node({**base_state, "df_summary": "summary"})

        sent = mock_llm.invoke.call_args[0][0]
        all_content = " ".join(m.content for m in sent)
        assert "UNIQUE_LESSON_MARKER" in all_content
        assert "LESSONS FROM PAST RUNS" in all_content

    @patch("agent.nodes.planner.get_llm")
    def test_user_correction_saved_as_lesson(self, mock_get_llm, base_state, fake_response):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = fake_response(json.dumps([
            {"id": "a", "title": "T", "priority": 1}
        ]))
        mock_get_llm.return_value = mock_llm

        from agent.nodes.planner import planner_node
        planner_node({**base_state, "df_summary": "s",
                      "user_edits_to_plan": "skip correlation, add churn"})

        stored = load_all_lessons()
        assert any(l["kind"] == "user_correction" for l in stored)
