"""
Test Suite 5: Report Generation
Verifies Markdown reports, notebooks, and end-of-run outputs are
produced correctly from a completed agent state. No LLM calls.
"""

import os
import pytest
from agent.nodes.report_generator import report_generator_node, _generate_markdown_report
from agent.state import AnalysisStatus


@pytest.fixture
def completed_state(base_state, sample_task):
    """A state that looks like a finished analysis run."""
    done_task = {
        **sample_task,
        "status": AnalysisStatus.COMPLETED,
        "generated_code": "print(df.describe())",
        "execution_result": "count 20 rows, mean sales 3000",
        "interpretation": "Sales average around 3000 with stable variance.",
        "confidence_score": 0.9,
        "assumptions": ["Data covers full period"],
    }
    return {
        **base_state,
        "df_summary": "test summary",
        "data_quality_report": {
            "total_rows": 20, "total_columns": 4, "duplicate_rows": 0,
            "missing_values": {}, "outlier_columns": [],
            "invalid_type_columns": [], "quality_score": 99.0,
            "recommendations": ["No issues found"],
        },
        "analysis_plan": [done_task],
        "completed_tasks": [done_task],
        "business_insights": ["**Descriptive Statistics:** Sales are healthy."],
        "execution_log": [
            {"timestamp": "2026-07-19T10:00:00", "node": "test",
             "action": "test action", "status": "success", "detail": "ok"}
        ],
    }


class TestMarkdownReport:

    def test_report_contains_all_sections(self, completed_state):
        md = _generate_markdown_report(completed_state)
        assert "Data Quality Assessment" in md
        assert "Analysis Results" in md
        assert "Key Business Insights" in md
        assert "Execution Timeline" in md

    def test_report_includes_task_findings(self, completed_state):
        md = _generate_markdown_report(completed_state)
        assert "Descriptive Statistics" in md
        assert "Sales average around 3000" in md

    def test_report_shows_confidence(self, completed_state):
        md = _generate_markdown_report(completed_state)
        assert "90%" in md

    def test_failed_tasks_section_appears_when_failures_exist(self, completed_state, sample_task):
        failed = {**sample_task, "id": "f1", "title": "Broken Analysis",
                  "status": AnalysisStatus.FAILED, "error_message": "KeyError: xyz"}
        state = {**completed_state, "failed_tasks": [failed]}
        md = _generate_markdown_report(state)
        assert "Failed Analyses" in md
        assert "Broken Analysis" in md


class TestReportGeneratorNode:

    def test_produces_markdown_file(self, completed_state):
        result = report_generator_node(completed_state)
        assert result["final_report_md"] is not None
        assert os.path.exists(result["final_report_md"])

    def test_produces_notebook(self, completed_state):
        result = report_generator_node(completed_state)
        assert result["notebook_path"] is not None
        assert os.path.exists(result["notebook_path"])
        assert result["notebook_path"].endswith(".ipynb")

    def test_notebook_contains_task_code(self, completed_state):
        import nbformat
        result = report_generator_node(completed_state)
        nb = nbformat.read(result["notebook_path"], as_version=4)
        all_source = " ".join(c.source for c in nb.cells)
        assert "df.describe()" in all_source

    def test_routes_to_done(self, completed_state):
        result = report_generator_node(completed_state)
        assert result["next_action"] == "done"
