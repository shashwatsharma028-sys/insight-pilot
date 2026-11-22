"""
Pure metric functions over a finished V1 pipeline state (no I/O except reading produced files).

Every function returns plain JSON-serialisable data. Answers use the golden
tolerance (tests/v2/golden/compare.values_match: rel 1e-9, abs 1e-6).
"""

import json
import math
import os
import re

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
QUALITY_KEYS = ("total_rows", "total_columns", "duplicate_rows", "missing_values", "quality_score")


def status_of(task):
    return getattr(task.get("status"), "value", task.get("status"))


# ── numerical accuracy ───────────────────────────────────────────────────────

def score_answers(printed: dict, expected: dict) -> dict:
    """Each expected answer is correct, incorrect or missing; extra printed keys are reported."""
    from tests.v2.golden.compare import values_match
    per = {}
    for key in sorted(expected):
        if key not in printed:
            per[key] = "missing"
        else:
            try:
                per[key] = "correct" if values_match(printed[key], expected[key]) else "incorrect"
            except (TypeError, ValueError):
                per[key] = "incorrect"
    counts = {s: sum(1 for v in per.values() if v == s) for s in ("correct", "incorrect", "missing")}
    return {"expected": len(expected), **counts, "unexpected": sorted(set(printed) - set(expected)),
            "per_answer": per}


def printed_answers(state) -> dict:
    from tests.v2.golden.compare import parse_golden
    out, dup = {}, []
    for task in state.get("completed_tasks") or []:
        for k, v in parse_golden(task.get("execution_result")).items():
            if k in out:
                dup.append(k)
            out[k] = v
    return out, sorted(set(dup))


# ── data quality ─────────────────────────────────────────────────────────────

def score_quality(report, expected: dict) -> dict:
    from tests.v2.golden.compare import values_match
    report = report if isinstance(report, dict) else {}
    per = {}
    for key in QUALITY_KEYS:
        if key not in report:
            per[key] = "missing"
            continue
        a, e = report[key], expected.get(key)
        if isinstance(e, dict):
            ok = isinstance(a, dict) and set(a) == set(e) and all(values_match(a[k], e[k]) for k in e)
        else:
            try:
                ok = values_match(a, e)
            except (TypeError, ValueError):
                ok = False
        per[key] = "correct" if ok else "incorrect"
    return {"expected": len(QUALITY_KEYS), "correct": sum(v == "correct" for v in per.values()),
            "per_value": per}


# ── tasks, retries, failures ─────────────────────────────────────────────────

def score_tasks(state, scenario) -> list:
    rows = []
    for t in state.get("analysis_plan") or []:
        tid = t.get("id")
        status = status_of(t)
        expected_status = scenario["expected_status"].get(tid)
        rows.append({
            "task_id": tid, "title": t.get("title"), "status": status, "expected_status": expected_status,
            "status_as_expected": status == expected_status,
            "retries": t.get("retry_count", 0), "expected_retries": scenario["expected_retries"].get(tid, 0),
            "error_type": getattr(t.get("error_type"), "value", t.get("error_type")),
            "has_chart": bool(t.get("chart_path")), "expects_chart": tid in scenario["expected_charts"],
            "has_interpretation": bool(t.get("interpretation")),
        })
    return rows


def task_summary(rows) -> dict:
    expected_completed = [r for r in rows if r["expected_status"] == "completed"]
    expected_failed = [r for r in rows if r["expected_status"] == "failed"]
    return {
        "planned": len(rows),
        "completed": sum(r["status"] == "completed" for r in rows),
        "failed": sum(r["status"] == "failed" for r in rows),
        "expected_completed": len(expected_completed),
        "completed_of_expected": sum(r["status"] == "completed" for r in expected_completed),
        "expected_failures_observed": sum(r["status"] == "failed" for r in expected_failed),
        "expected_failures": len(expected_failed),
        "status_as_expected": sum(r["status_as_expected"] for r in rows),
        "retries": sum(r["retries"] for r in rows),
        "retries_as_expected": sum(r["retries"] == r["expected_retries"] or r["status"] == "failed" for r in rows),
        "error_types": sorted({r["error_type"] for r in rows if r["error_type"]}),
    }


# ── charts and reports ───────────────────────────────────────────────────────

def check_png(path, allowed_dir) -> dict:
    out = {"path_inside_output_dir": False, "exists": False, "png_signature": False, "decodes": False,
           "width": None, "height": None}
    if not path:
        return out
    out["path_inside_output_dir"] = os.path.dirname(os.path.realpath(path)) == os.path.realpath(allowed_dir)
    if not os.path.isfile(path):
        return out
    out["exists"] = True
    with open(path, "rb") as f:
        out["png_signature"] = f.read(8) == PNG_MAGIC
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            img.load()
            out["width"], out["height"] = img.size
        out["decodes"] = out["width"] > 0 and out["height"] > 0
    except Exception:
        out["decodes"] = False
    return out


def chart_validity(state, scenario, charts_dir) -> dict:
    per = {}
    for t in state.get("analysis_plan") or []:
        tid = t.get("id")
        if tid in scenario["expected_charts"] or t.get("chart_path"):
            c = check_png(t.get("chart_path"), charts_dir)
            c["expected"] = tid in scenario["expected_charts"]
            c["valid"] = c["expected"] and c["path_inside_output_dir"] and c["png_signature"] and c["decodes"]
            per[tid] = c
    return {"expected": len(scenario["expected_charts"]), "valid": sum(c["valid"] for c in per.values()),
            "unexpected": sorted(t for t, c in per.items() if not c["expected"]), "per_task": per}


def report_validity(state, reports_dir) -> dict:
    real = os.path.realpath(reports_dir)
    out = {}
    md = state.get("final_report_md")
    headings, md_ok = [], False
    if md and os.path.isfile(md) and os.path.dirname(os.path.realpath(md)) == real:
        with open(md, encoding="utf-8") as f:
            text = f.read()
        headings = [ln.rstrip() for ln in text.splitlines() if ln.startswith("#")]
        titles = [t.get("title") for t in state.get("completed_tasks") or []]
        md_ok = bool(headings) and headings[0].startswith("# ") and all(
            any(re.fullmatch(rf"### \d+\. {re.escape(title)}", h) for h in headings) for title in titles)
    out["markdown"] = {"valid": md_ok, "headings": headings}
    pdf = state.get("final_report_pdf")
    pdf_ok = False
    if pdf and os.path.isfile(pdf) and os.path.dirname(os.path.realpath(pdf)) == real:
        with open(pdf, "rb") as f:
            data = f.read()
        pdf_ok = data.startswith(b"%PDF-") and b"%%EOF" in data[-1024:]
    out["pdf"] = {"valid": pdf_ok}
    nb = state.get("notebook_path")
    nb_ok, cells = False, 0
    if nb and os.path.isfile(nb) and os.path.dirname(os.path.realpath(nb)) == real:
        try:
            import nbformat
            with open(nb, encoding="utf-8") as f:
                node = nbformat.read(f, as_version=4)
            nbformat.validate(node)
            cells = sum(1 for c in node.cells if c.cell_type == "code")
            nb_ok = cells >= 1 + len(state.get("completed_tasks") or [])
        except Exception:
            nb_ok = False
    out["notebook"] = {"valid": nb_ok, "code_cells": cells}
    out["valid"] = sum(1 for k in ("markdown", "pdf", "notebook") if out[k]["valid"])
    out["expected"] = 3
    return out


# ── latency statistics ───────────────────────────────────────────────────────

def percentile(values, q):
    """Nearest-rank percentile; raises on empty input."""
    if not values:
        raise ValueError("no samples")
    s = sorted(values)
    k = max(1, math.ceil(q * len(s) / 100))
    return s[k - 1]


def latency_stats(values) -> dict:
    vals = [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if not vals:
        return {"n": 0, "median_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None}
    s = sorted(vals)
    mid = len(s) // 2
    median = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    return {"n": len(s), "median_ms": round(median * 1000, 1), "p95_ms": round(percentile(s, 95) * 1000, 1),
            "min_ms": round(s[0] * 1000, 1), "max_ms": round(s[-1] * 1000, 1)}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
