"""
Node 1: Data Ingestion & Quality Check
- Loads the CSV
- Builds a df_summary string for the LLM (never passes the raw df)
- Runs automated quality checks: missing values, duplicates, outliers, type issues
- Computes a quality score and recommendations
"""

import pandas as pd
import numpy as np
from datetime import datetime
from agent.state import AgentState, DataQualityReport
from utils.llm import get_llm


def _detect_outliers(df: pd.DataFrame) -> list:
    """IQR-based outlier detection on numeric columns."""
    outlier_cols = []
    for col in df.select_dtypes(include=[np.number]).columns:
        Q1 = df[col].quantile(0.25)
        Q3 = df[col].quantile(0.75)
        IQR = Q3 - Q1
        outliers = df[(df[col] < Q1 - 1.5 * IQR) | (df[col] > Q3 + 1.5 * IQR)]
        if len(outliers) > 0:
            outlier_cols.append(f"{col} ({len(outliers)} outliers)")
    return outlier_cols


def _detect_type_issues(df: pd.DataFrame) -> list:
    """Detect columns that look numeric but are stored as object."""
    issues = []
    for col in df.select_dtypes(include=["object", "str"]).columns:
        sample = df[col].dropna().head(50)
        numeric_count = sum(1 for v in sample if str(v).replace('.', '', 1).replace('-', '', 1).isdigit())
        if numeric_count > len(sample) * 0.8:
            issues.append(col)
    return issues


def _quality_score(df: pd.DataFrame, duplicate_rows: int, missing_vals: dict) -> float:
    """Score data quality 0-100."""
    total_cells = df.shape[0] * df.shape[1]
    total_missing = sum(missing_vals.values())
    missing_pct = total_missing / total_cells if total_cells > 0 else 0
    duplicate_pct = duplicate_rows / len(df) if len(df) > 0 else 0

    score = 100
    score -= missing_pct * 100 * 0.5   # Up to -50 for missing
    score -= duplicate_pct * 100 * 0.3  # Up to -30 for duplicates
    return round(max(0, score), 1)


def data_ingestion_node(state: AgentState) -> AgentState:
    """Load CSV, build summary, run quality checks."""

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "data_ingestion",
        "action": "Loading and profiling dataset"
    }

    try:
        df = pd.read_csv(state["csv_path"])

        # ── Build LLM-friendly summary ─────────────────────────────
        dtypes_str = {col: str(dtype) for col, dtype in df.dtypes.items()}
        numeric_stats = df.describe().round(2).to_string() if len(df.select_dtypes(include=[np.number]).columns) > 0 else "No numeric columns"
        sample_str = df.head(5).to_string()

        df_summary = f"""
DATASET: {state['dataset_name']}
SHAPE: {df.shape[0]} rows × {df.shape[1]} columns
COLUMNS & TYPES: {dtypes_str}
SAMPLE (first 5 rows):
{sample_str}

NUMERIC STATISTICS:
{numeric_stats}

CATEGORICAL COLUMNS: {list(df.select_dtypes(include=['object', 'str']).columns)}
DATE-LIKE COLUMNS: {[c for c in df.columns if 'date' in c.lower() or 'time' in c.lower() or 'month' in c.lower()]}
"""

        # ── Quality checks ─────────────────────────────────────────
        duplicate_rows = int(df.duplicated().sum())
        missing_vals = {col: int(df[col].isna().sum())
                        for col in df.columns if df[col].isna().sum() > 0}
        outlier_cols = _detect_outliers(df)
        invalid_type_cols = _detect_type_issues(df)
        q_score = _quality_score(df, duplicate_rows, missing_vals)

        recommendations = []
        if duplicate_rows > 0:
            recommendations.append(f"Remove {duplicate_rows} duplicate rows before analysis.")
        if missing_vals:
            for col, count in missing_vals.items():
                pct = round(count / len(df) * 100, 1)
                if pct > 10:
                    recommendations.append(f"Column '{col}' has {pct}% missing — consider imputation or exclusion.")
                else:
                    recommendations.append(f"Column '{col}' has {count} missing values ({pct}%) — minor, monitor.")
        if outlier_cols:
            recommendations.append(f"Outliers detected in: {', '.join(outlier_cols[:3])} — verify before trend analysis.")
        if invalid_type_cols:
            recommendations.append(f"Columns may need type conversion: {invalid_type_cols}")

        quality_report: DataQualityReport = {
            "total_rows": df.shape[0],
            "total_columns": df.shape[1],
            "duplicate_rows": duplicate_rows,
            "missing_values": missing_vals,
            "outlier_columns": outlier_cols,
            "invalid_type_columns": invalid_type_cols,
            "quality_score": q_score,
            "recommendations": recommendations
        }

        log_entry["status"] = "success"
        log_entry["detail"] = f"Loaded {df.shape[0]}×{df.shape[1]} dataset. Quality score: {q_score}/100"

        return {
            **state,
            "df_summary": df_summary,
            "df_dtypes": dtypes_str,
            "df_shape": df.shape,
            "df_sample": sample_str,
            "data_quality_report": quality_report,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "plan"
        }

    except Exception as e:
        log_entry["status"] = "error"
        log_entry["detail"] = str(e)
        return {
            **state,
            "error_message": f"Failed to load dataset: {str(e)}",
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "error"
        }
