"""
Shared pytest fixtures for InsightPilot test suite.
Provides sample CSVs and pre-built agent states so tests don't depend
on external files or API calls.
"""

import os
import sys
import pytest
import pandas as pd
import numpy as np

# Make project root importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.state import AgentState, AnalysisStatus


@pytest.fixture
def sample_csv(tmp_path):
    """A small, clean sales-like CSV."""
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=20, freq="D").astype(str),
        "region": ["North", "South"] * 10,
        "sales": np.random.randint(1000, 5000, 20),
        "units": np.random.randint(10, 100, 20),
    })
    path = tmp_path / "sample_sales.csv"
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def dirty_csv(tmp_path):
    """CSV with quality problems: missing values, duplicates, outliers."""
    df = pd.DataFrame({
        "name": ["A", "B", "C", "D", "E", "E"],          # duplicate row at end
        "score": [50, 60, np.nan, 55, 10000, 58],         # NaN + extreme outlier
        "grade": ["X", "Y", "X", None, "Y", "Y"],         # missing categorical
    })
    df = pd.concat([df, df.tail(1)], ignore_index=True)   # explicit duplicate
    path = tmp_path / "dirty.csv"
    df.to_csv(path, index=False)
    return str(path)


@pytest.fixture
def base_state(sample_csv) -> AgentState:
    """A minimal valid AgentState pointing at the sample CSV."""
    return {
        "csv_path": sample_csv,
        "dataset_name": "Test Dataset",
        "df_summary": None, "df_dtypes": None, "df_shape": None, "df_sample": None,
        "data_quality_report": None,
        "analysis_plan": [], "plan_approved": False, "user_edits_to_plan": None,
        "current_task_index": 0, "completed_tasks": [], "failed_tasks": [],
        "max_retries": 2,
        "chart_paths": [], "execution_log": [],
        "final_report_md": None, "final_report_pdf": None, "notebook_path": None,
        "business_insights": [],
        "conversation_history": [],
        "current_user_message": None, "agent_response": None,
        "safe_mode": True,
        "is_followup_query": False,
        "error_message": None, "next_action": None,
    }


@pytest.fixture
def sample_task():
    """A single pending analysis task."""
    return {
        "id": "task_001",
        "title": "Descriptive Statistics",
        "description": "Summarize numeric columns",
        "analysis_type": "descriptive",
        "priority": 1,
        "status": AnalysisStatus.PENDING,
        "generated_code": None,
        "execution_result": None,
        "error_message": None,
        "error_type": None,
        "retry_count": 0,
        "chart_path": None,
        "interpretation": None,
        "confidence_score": None,
        "assumptions": None,
    }


class FakeResponse:
    """Mimics a LangChain LLM response object."""
    def __init__(self, content):
        self.content = content


@pytest.fixture
def fake_response():
    return FakeResponse
