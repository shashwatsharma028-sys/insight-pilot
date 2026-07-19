"""
Test Suite 3: Data Ingestion & Quality Checks
Verifies the agent correctly profiles datasets and detects quality issues —
missing values, duplicates, outliers. Runs without any LLM calls.
"""

import pytest
from agent.nodes.data_ingestion import data_ingestion_node


class TestDataIngestion:

    def test_clean_csv_loads_successfully(self, base_state):
        result = data_ingestion_node(base_state)
        assert result["next_action"] == "plan"
        assert result["df_shape"] == (20, 4)
        assert result["df_summary"] is not None
        assert "region" in result["df_summary"]

    def test_quality_report_structure(self, base_state):
        result = data_ingestion_node(base_state)
        q = result["data_quality_report"]
        assert q["total_rows"] == 20
        assert q["total_columns"] == 4
        assert 0 <= q["quality_score"] <= 100

    def test_clean_data_scores_high(self, base_state):
        result = data_ingestion_node(base_state)
        assert result["data_quality_report"]["quality_score"] >= 95

    def test_detects_duplicates(self, base_state, dirty_csv):
        state = {**base_state, "csv_path": dirty_csv}
        result = data_ingestion_node(state)
        assert result["data_quality_report"]["duplicate_rows"] >= 1

    def test_detects_missing_values(self, base_state, dirty_csv):
        state = {**base_state, "csv_path": dirty_csv}
        result = data_ingestion_node(state)
        missing = result["data_quality_report"]["missing_values"]
        assert "score" in missing
        assert "grade" in missing

    def test_detects_outliers(self, base_state, dirty_csv):
        """The 10000 in a column of ~50s must be flagged."""
        state = {**base_state, "csv_path": dirty_csv}
        result = data_ingestion_node(state)
        outliers = result["data_quality_report"]["outlier_columns"]
        assert any("score" in o for o in outliers)

    def test_dirty_data_generates_recommendations(self, base_state, dirty_csv):
        state = {**base_state, "csv_path": dirty_csv}
        result = data_ingestion_node(state)
        assert len(result["data_quality_report"]["recommendations"]) > 0

    def test_dirty_data_scores_lower_than_clean(self, base_state, dirty_csv):
        clean_score = data_ingestion_node(base_state)["data_quality_report"]["quality_score"]
        dirty_score = data_ingestion_node({**base_state, "csv_path": dirty_csv})["data_quality_report"]["quality_score"]
        assert dirty_score < clean_score

    def test_nonexistent_file_routes_to_error(self, base_state):
        state = {**base_state, "csv_path": "/does/not/exist.csv"}
        result = data_ingestion_node(state)
        assert result["next_action"] == "error"
        assert result["error_message"] is not None

    def test_execution_log_updated(self, base_state):
        result = data_ingestion_node(base_state)
        log = result["execution_log"]
        assert len(log) == 1
        assert log[0]["node"] == "data_ingestion"
        assert log[0]["status"] == "success"
