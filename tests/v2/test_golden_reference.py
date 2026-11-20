"""
P0: golden answers are reproducible and independently verified.

Three independent sources must agree before a golden answer is trusted:
1. the committed expected/*.json files,
2. a fresh standard-library computation (tests/v2/golden/reference.py),
3. a separate pandas computation written in this file.
The V1 data-ingestion node's quality report must match the reference too.
"""

import pandas as pd
import pytest

from tests.v2.golden import reference as ref
from tests.v2.golden.compare import mismatches
from tests.v2.conftest import make_initial_state

DATASETS = list(ref.DATASETS)


@pytest.mark.parametrize("dataset", DATASETS)
def test_demo_dataset_unchanged(dataset):
    """The examination demo datasets must be byte-identical to the baseline."""
    expected = ref.load_expected(dataset)
    assert ref.sha256_file(f"{ref.DATA_DIR}/{expected['file']}") == expected["sha256"]


@pytest.mark.parametrize("dataset", DATASETS)
def test_committed_expected_matches_fresh_stdlib_computation(dataset):
    assert ref.compute(dataset) == ref.load_expected(dataset)


def _pandas_answers(dataset: str, df: pd.DataFrame) -> dict:
    if dataset == "sales":
        by_region = df.groupby("region")["sales_amount"].sum()
        return {
            "row_count": len(df),
            "total_sales_amount": df["sales_amount"].sum(),
            "sales_by_region": by_region.to_dict(),
            "top_region": by_region.idxmax(),
            "total_returns": df["returns"].sum(),
            "mean_customer_satisfaction": df["customer_satisfaction"].mean(),
            "target_attainment_pct": df["sales_amount"].sum() / df["sales_target"].sum() * 100,
        }
    if dataset == "attendance":
        by_dept = df.groupby("department")["hours_worked"].mean()
        return {
            "row_count": len(df),
            "status_counts": df["status"].value_counts().to_dict(),
            "present_rate": (df["status"] == "Present").mean(),
            "mean_hours_worked": df["hours_worked"].mean(),
            "mean_hours_by_department": by_dept.to_dict(),
            "top_department_by_mean_hours": by_dept.idxmax(),
            "gps_mismatch_count": int((df["gps_location_match"] == False).sum()),  # noqa: E712
        }
    if dataset == "student":
        by_subject = df.groupby("subject")["score"].mean()
        return {
            "row_count": len(df),
            "pass_rate": df["passed"].mean(),
            "grade_letter_counts": df["grade_letter"].value_counts().to_dict(),
            "mean_score": df["score"].mean(),
            "mean_score_by_subject": by_subject.to_dict(),
            "top_subject_by_mean_score": by_subject.idxmax(),
            "corr_study_hours_score": df["study_hours_per_day"].corr(df["score"]),
        }
    if dataset == "ecommerce":
        means = df.groupby("is_weekend")["revenue"].mean()
        monthly = df.groupby(df["date"].str[:7])["revenue"].sum()
        return {
            "row_count": len(df),
            "total_revenue": df["revenue"].sum(),
            "total_orders": df["orders"].sum(),
            "mean_revenue_weekend": means[True],
            "mean_revenue_weekday": means[False],
            "top_month_by_revenue": monthly.idxmax(),
            "corr_visitors_revenue": df["website_visitors"].corr(df["revenue"]),
        }
    raise KeyError(dataset)


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if hasattr(value, "item"):
        return value.item()
    return value


@pytest.mark.parametrize("dataset", DATASETS)
def test_pandas_cross_check_agrees_with_stdlib_reference(dataset):
    expected = ref.load_expected(dataset)
    df = pd.read_csv(f"{ref.DATA_DIR}/{expected['file']}")
    actual = _plain(_pandas_answers(dataset, df))
    assert set(actual) == set(expected["answers"])
    assert mismatches(actual, expected["answers"]) == {}


@pytest.mark.parametrize("dataset", DATASETS)
def test_v1_quality_report_matches_independent_reference(dataset):
    from agent.nodes.data_ingestion import data_ingestion_node

    state = data_ingestion_node(make_initial_state(dataset, safe_mode=True))
    assert state["next_action"] == "plan"
    report = state["data_quality_report"]
    expected = ref.load_expected(dataset)["quality"]
    for key in ("total_rows", "total_columns", "duplicate_rows", "missing_values", "quality_score"):
        assert report[key] == expected[key], key
