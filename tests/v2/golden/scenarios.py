"""
Golden scenarios: scripted LLM replies for each demo dataset.

What these scenarios do and do not prove (be honest in reports):
- The "generated code" below is hand-written pandas code that stands in for
  the LLM. It runs through the REAL V1 sandbox subprocess, so the tests prove
  the pipeline (ingest -> plan -> codegen -> sandbox -> retry -> interpret ->
  report -> chat) carries correct numbers end to end, and that those numbers
  equal the independently computed golden answers.
- They do NOT measure LLM answer quality, provider token usage or cost.
  Token figures from these runs are character-based mock estimates.

Each analysis task prints lines of the form  GOLDEN <key>=<json>  which the
tests parse from the sandbox's captured stdout.
"""

import json
import textwrap

_EMIT = textwrap.dedent("""
    import json
    def emit(key, value):
        print("GOLDEN " + key + "=" + json.dumps(value))
""").strip()


def _code(body: str) -> str:
    return _EMIT + "\n\n" + textwrap.dedent(body).strip() + "\n"


def _task(tid, title, atype, priority, description):
    return {
        "id": tid, "title": title, "description": description,
        "analysis_type": atype, "priority": priority, "status": "pending",
        "generated_code": None, "execution_result": None, "error_message": None,
        "error_type": None, "retry_count": 0, "chart_path": None,
        "interpretation": None, "confidence_score": None, "assumptions": None,
    }


# ── Sales: includes a self-correction retry and a blocked unsafe task ────────

SALES = {
    "dataset_name": "Sales Data",
    "file": "sales_data.csv",
    "tasks": [
        _task("sales_t1", "Sales Overview", "descriptive", 1,
              "Overall sales volume, returns and satisfaction."),
        _task("sales_t2", "Regional Sales Comparison", "comparative", 2,
              "Compare total sales by region and identify the leader."),
        _task("sales_t3", "Target Attainment", "descriptive", 3,
              "Total sales as a percentage of total target."),
        _task("sales_t4", "Filesystem Inventory", "custom", 4,
              "Adversarial task: generated code tries to use the os module."),
    ],
    # Code attempts per task title. Attempt n is used for retry n (last repeats).
    "code": {
        "Sales Overview": [
            # Attempt 1: wrong column name -> KeyError -> DATA error -> retry
            _code("""
                emit("total_sales_amount", float(df["Sales"].sum()))
            """),
            _code("""
                emit("row_count", int(len(df)))
                emit("total_sales_amount", float(df["sales_amount"].sum()))
                emit("total_returns", float(df["returns"].sum()))
                emit("mean_customer_satisfaction", float(df["customer_satisfaction"].mean()))
                df["sales_amount"].plot(kind="hist", bins=20, title="Sales amount distribution")
                plt.savefig(CHART_PATH, bbox_inches="tight")
                plt.close("all")
            """),
        ],
        "Regional Sales Comparison": [
            _code("""
                by_region = df.groupby("region")["sales_amount"].sum()
                emit("sales_by_region", {k: float(v) for k, v in by_region.items()})
                emit("top_region", str(by_region.idxmax()))
                by_region.plot(kind="bar", title="Sales by region")
                plt.savefig(CHART_PATH, bbox_inches="tight")
                plt.close("all")
            """),
        ],
        "Target Attainment": [
            _code("""
                pct = df["sales_amount"].sum() / df["sales_target"].sum() * 100
                emit("target_attainment_pct", float(pct))
            """),
        ],
        "Filesystem Inventory": [
            # Always violates sandbox_safety -> blocked before execution every time
            _code("""
                import os
                emit("files", os.listdir("."))
            """),
        ],
    },
    "expected_status": {
        "sales_t1": "completed", "sales_t2": "completed",
        "sales_t3": "completed", "sales_t4": "failed",
    },
    "expected_retries": {"sales_t1": 1},
    "expected_charts": {"sales_t1", "sales_t2"},
}

ATTENDANCE = {
    "dataset_name": "Attendance Data",
    "file": "attendance_data.csv",
    "tasks": [
        _task("att_t1", "Attendance Status Breakdown", "descriptive", 1,
              "Counts of Present / Late / Absent and the present rate."),
        _task("att_t2", "Hours Worked by Department", "comparative", 2,
              "Mean hours worked overall and per department."),
        _task("att_t3", "GPS Location Mismatches", "outlier", 3,
              "Count punches where GPS location did not match the site."),
    ],
    "code": {
        "Attendance Status Breakdown": [_code("""
            counts = df["status"].value_counts()
            emit("row_count", int(len(df)))
            emit("status_counts", {k: int(v) for k, v in counts.items()})
            emit("present_rate", float((df["status"] == "Present").mean()))
            counts.plot(kind="bar", title="Attendance status")
            plt.savefig(CHART_PATH, bbox_inches="tight")
            plt.close("all")
        """)],
        "Hours Worked by Department": [_code("""
            by_dept = df.groupby("department")["hours_worked"].mean()
            emit("mean_hours_worked", float(df["hours_worked"].mean()))
            emit("mean_hours_by_department", {k: float(v) for k, v in by_dept.items()})
            emit("top_department_by_mean_hours", str(by_dept.idxmax()))
            by_dept.plot(kind="bar", title="Mean hours by department")
            plt.savefig(CHART_PATH, bbox_inches="tight")
            plt.close("all")
        """)],
        "GPS Location Mismatches": [_code("""
            emit("gps_mismatch_count", int((df["gps_location_match"] == False).sum()))
        """)],
    },
    "expected_status": {"att_t1": "completed", "att_t2": "completed", "att_t3": "completed"},
    "expected_retries": {},
    "expected_charts": {"att_t1", "att_t2"},
}

STUDENT = {
    "dataset_name": "Student Performance",
    "file": "student_data.csv",
    "tasks": [
        _task("stu_t1", "Score Overview", "descriptive", 1,
              "Mean score, pass rate and grade distribution."),
        _task("stu_t2", "Scores by Subject", "comparative", 2,
              "Mean score per subject and the strongest subject."),
        _task("stu_t3", "Study Hours vs Score", "correlation", 3,
              "Pearson correlation between daily study hours and score."),
    ],
    "code": {
        "Score Overview": [_code("""
            emit("row_count", int(len(df)))
            emit("pass_rate", float(df["passed"].mean()))
            emit("mean_score", float(df["score"].mean()))
            emit("grade_letter_counts", {k: int(v) for k, v in df["grade_letter"].value_counts().items()})
            df["score"].plot(kind="hist", bins=20, title="Score distribution")
            plt.savefig(CHART_PATH, bbox_inches="tight")
            plt.close("all")
        """)],
        "Scores by Subject": [_code("""
            by_subject = df.groupby("subject")["score"].mean()
            emit("mean_score_by_subject", {k: float(v) for k, v in by_subject.items()})
            emit("top_subject_by_mean_score", str(by_subject.idxmax()))
        """)],
        "Study Hours vs Score": [_code("""
            emit("corr_study_hours_score", float(df["study_hours_per_day"].corr(df["score"])))
            plt.scatter(df["study_hours_per_day"], df["score"], s=4)
            plt.savefig(CHART_PATH, bbox_inches="tight")
            plt.close("all")
        """)],
    },
    "expected_status": {"stu_t1": "completed", "stu_t2": "completed", "stu_t3": "completed"},
    "expected_retries": {},
    "expected_charts": {"stu_t1", "stu_t3"},
}

ECOMMERCE = {
    "dataset_name": "E-commerce Time Series",
    "file": "ecommerce_timeseries.csv",
    "tasks": [
        _task("eco_t1", "Revenue Overview", "descriptive", 1,
              "Total revenue and orders across the period."),
        _task("eco_t2", "Weekend vs Weekday Revenue", "comparative", 2,
              "Mean daily revenue on weekends versus weekdays."),
        _task("eco_t3", "Monthly Revenue Trend", "trend", 3,
              "Best revenue month and visitors-revenue correlation."),
    ],
    "code": {
        "Revenue Overview": [_code("""
            emit("row_count", int(len(df)))
            emit("total_revenue", float(df["revenue"].sum()))
            emit("total_orders", float(df["orders"].sum()))
        """)],
        "Weekend vs Weekday Revenue": [_code("""
            means = df.groupby("is_weekend")["revenue"].mean()
            emit("mean_revenue_weekend", float(means[True]))
            emit("mean_revenue_weekday", float(means[False]))
        """)],
        "Monthly Revenue Trend": [_code("""
            monthly = df.groupby(df["date"].str[:7])["revenue"].sum()
            emit("top_month_by_revenue", str(monthly.idxmax()))
            emit("corr_visitors_revenue", float(df["website_visitors"].corr(df["revenue"])))
            monthly.plot(title="Monthly revenue")
            plt.savefig(CHART_PATH, bbox_inches="tight")
            plt.close("all")
        """)],
    },
    "expected_status": {"eco_t1": "completed", "eco_t2": "completed", "eco_t3": "completed"},
    "expected_retries": {},
    "expected_charts": {"eco_t3"},
}

SCENARIOS = {
    "sales": SALES,
    "attendance": ATTENDANCE,
    "student": STUDENT,
    "ecommerce": ECOMMERCE,
}


def plan_json(scenario: dict, edited: bool = False) -> str:
    """Planner reply. The edited plan (HITL 'edit' path) drops the last task."""
    tasks = scenario["tasks"][:-1] if edited else scenario["tasks"]
    return json.dumps(tasks)


def interpretation_json(task_title: str) -> str:
    return json.dumps({
        "interpretation": f"Mock interpretation for {task_title}.",
        "business_insight": f"Mock business insight for {task_title}.",
        "confidence_score": 0.85,
        "assumptions": ["Scripted offline test response"],
    })


FOLLOWUP_REPLY = "Mock follow-up answer based on the stored findings."
