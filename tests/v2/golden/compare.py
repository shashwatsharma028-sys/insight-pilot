"""
Helpers to compare pipeline output with golden answers and V1 snapshots.

Snapshots (tests/v2/golden/snapshots/*.json) have two parts:
- "behavior": gated. Must match exactly for every later phase with all V2
  flags off. Regenerate only with reviewer approval:
      IP_UPDATE_GOLDEN=1 python -m pytest tests/v2
- "informational": NOT gated. Character-based mock token estimates; they are
  not provider token counts and must not be reported as provider savings.
"""

import json
import math
import os
import re

SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
_GOLDEN_LINE = re.compile(r"^GOLDEN (\w+)=(.+)$", re.M)


def parse_golden(stdout: str) -> dict:
    return {k: json.loads(v) for k, v in _GOLDEN_LINE.findall(stdout or "")}


def values_match(actual, expected, rel=1e-9, abs_tol=1e-6) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual == expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=rel, abs_tol=abs_tol)
    if isinstance(expected, dict) and isinstance(actual, dict):
        return (set(actual) == set(expected)
                and all(values_match(actual[k], expected[k], rel, abs_tol) for k in expected))
    if isinstance(expected, list) and isinstance(actual, list):
        return len(actual) == len(expected) and all(
            values_match(a, e, rel, abs_tol) for a, e in zip(actual, expected))
    return actual == expected


def mismatches(actual: dict, expected: dict) -> dict:
    out = {}
    for key in sorted(set(actual) | set(expected)):
        if key not in actual or key not in expected or not values_match(actual[key], expected[key]):
            out[key] = {"actual": actual.get(key, "<missing>"), "expected": expected.get(key, "<missing>")}
    return out


def _v(x):
    return getattr(x, "value", x)


def normalize_run(state: dict, controller, monitor_summary: dict, safe_mode: bool) -> dict:
    """Deterministic, path- and timestamp-free view of a pipeline run."""
    report_headings = []
    md_path = state.get("final_report_md")
    if md_path and os.path.exists(md_path):
        with open(md_path, encoding="utf-8") as f:
            report_headings = [line.rstrip() for line in f if line.startswith("#")]

    return {
        "behavior": {
            "dataset_name": state["dataset_name"],
            "safe_mode": safe_mode,
            "quality_score": (state.get("data_quality_report") or {}).get("quality_score"),
            "tasks": [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "status": _v(t["status"]),
                    "retry_count": t["retry_count"],
                    "error_type": _v(t.get("error_type")),
                    "has_chart": bool(t.get("chart_path")),
                    "has_interpretation": bool(t.get("interpretation")),
                }
                for t in state.get("analysis_plan", [])
            ],
            "completed_ids": [t["id"] for t in state.get("completed_tasks", [])],
            "failed_ids": [t["id"] for t in state.get("failed_tasks", [])],
            "business_insights_count": len(state.get("business_insights", [])),
            "chart_count": len(state.get("chart_paths", [])),
            "report_headings": report_headings,
            "has_pdf": bool(state.get("final_report_pdf")),
            "has_notebook": bool(state.get("notebook_path")),
            "execution_log": [[e.get("node"), e.get("status")] for e in state.get("execution_log", [])],
            "llm_calls_by_skill": controller.calls_by_skill(),
        },
        "informational": {
            "note": "Mock-LLM character estimates. NOT provider tokens; never report as cost or savings.",
            "mock_estimated_input_tokens": monitor_summary.get("input_tokens"),
            "mock_estimated_output_tokens": monitor_summary.get("output_tokens"),
            "mock_llm_calls": monitor_summary.get("calls"),
        },
    }


def snapshot_path(name: str) -> str:
    return os.path.join(SNAPSHOT_DIR, f"{name}.json")


def check_snapshot(name: str, snapshot: dict) -> None:
    """Assert the gated behaviour equals the committed V1 snapshot."""
    path = snapshot_path(name)
    if os.environ.get("IP_UPDATE_GOLDEN") == "1":
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        return
    assert os.path.exists(path), (
        f"Missing golden snapshot {path}. Generate it with IP_UPDATE_GOLDEN=1 after review.")
    with open(path, encoding="utf-8") as f:
        committed = json.load(f)
    # Round-trip through JSON so tuples/lists compare equally.
    actual = json.loads(json.dumps(snapshot["behavior"], ensure_ascii=False))
    assert actual == committed["behavior"], (
        f"V1 behaviour drifted from golden snapshot '{name}'.")
