"""
Independent reference answers for the golden datasets.

Deliberately implemented with the Python standard library only (csv,
statistics, math) so the expected values do not depend on pandas, which is
what the agent's generated code uses. tests/v2/test_golden_reference.py
cross-checks every value against a separate pandas implementation; the two
must agree before a value is trusted as a golden answer.

Regenerate the committed expected files (only after a reviewed dataset
change) with:
    python -m tests.v2.golden.reference --write
"""

import csv
import hashlib
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DATA_DIR = os.path.join(ROOT, "data")
EXPECTED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "expected")

DATASETS = {
    "sales": "sales_data.csv",
    "attendance": "attendance_data.csv",
    "student": "student_data.csv",
    "ecommerce": "ecommerce_timeseries.csv",
}

# pandas' documented default NA tokens (read_csv `na_values` defaults).
NA_TOKENS = {
    "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan",
    "1.#IND", "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a",
    "nan", "null",
}


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_rows(path: str):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [r for r in reader]
    return header, rows


def _col(header, rows, name):
    i = header.index(name)
    return [r[i] for r in rows]


def _is_na(v: str) -> bool:
    return v in NA_TOKENS


def _floats(values):
    return [float(v) for v in values if not _is_na(v)]


def _r(x: float, nd: int = 6) -> float:
    return round(float(x), nd)


def _pearson(xs, ys) -> float:
    pairs = [(float(a), float(b)) for a, b in zip(xs, ys) if not _is_na(a) and not _is_na(b)]
    n = len(pairs)
    mx = math.fsum(p[0] for p in pairs) / n
    my = math.fsum(p[1] for p in pairs) / n
    sxy = math.fsum((a - mx) * (b - my) for a, b in pairs)
    sxx = math.fsum((a - mx) ** 2 for a, _ in pairs)
    syy = math.fsum((b - my) ** 2 for _, b in pairs)
    return sxy / math.sqrt(sxx * syy)


def _group_sum(keys, values):
    acc = defaultdict(list)
    for k, v in zip(keys, values):
        if not _is_na(v):
            acc[k].append(float(v))
    return {k: math.fsum(vs) for k, vs in acc.items()}


def _group_mean(keys, values):
    acc = defaultdict(list)
    for k, v in zip(keys, values):
        if not _is_na(v):
            acc[k].append(float(v))
    return {k: math.fsum(vs) / len(vs) for k, vs in acc.items()}


def _argmax(d: dict) -> str:
    return max(sorted(d), key=lambda k: d[k])


# ── Data-quality reference (mirrors the documented formula) ──────────────────

def quality_reference(header, rows) -> dict:
    seen, dups = set(), 0
    for r in rows:
        t = tuple(r)
        if t in seen:
            dups += 1
        seen.add(t)
    missing = {}
    for i, name in enumerate(header):
        n = sum(1 for r in rows if _is_na(r[i]))
        if n:
            missing[name] = n
    total_cells = len(rows) * len(header)
    missing_pct = sum(missing.values()) / total_cells if total_cells else 0
    dup_pct = dups / len(rows) if rows else 0
    score = round(max(0, 100 - missing_pct * 100 * 0.5 - dup_pct * 100 * 0.3), 1)
    return {
        "total_rows": len(rows),
        "total_columns": len(header),
        "duplicate_rows": dups,
        "missing_values": missing,
        "quality_score": score,
    }


# ── Per-dataset golden questions ─────────────────────────────────────────────

def answers_sales(h, rows) -> dict:
    amount = _col(h, rows, "sales_amount")
    region = _col(h, rows, "region")
    by_region = _group_sum(region, amount)
    total = math.fsum(_floats(amount))
    target = math.fsum(_floats(_col(h, rows, "sales_target")))
    return {
        "row_count": len(rows),
        "total_sales_amount": _r(total),
        "sales_by_region": {k: _r(v) for k, v in sorted(by_region.items())},
        "top_region": _argmax(by_region),
        "total_returns": _r(math.fsum(_floats(_col(h, rows, "returns")))),
        "mean_customer_satisfaction": _r(statistics.fmean(_floats(_col(h, rows, "customer_satisfaction")))),
        "target_attainment_pct": _r(total / target * 100),
    }


def answers_attendance(h, rows) -> dict:
    status = Counter(_col(h, rows, "status"))
    by_dept = _group_mean(_col(h, rows, "department"), _col(h, rows, "hours_worked"))
    return {
        "row_count": len(rows),
        "status_counts": dict(sorted(status.items())),
        "present_rate": _r(status.get("Present", 0) / len(rows)),
        "mean_hours_worked": _r(statistics.fmean(_floats(_col(h, rows, "hours_worked")))),
        "mean_hours_by_department": {k: _r(v) for k, v in sorted(by_dept.items())},
        "top_department_by_mean_hours": _argmax(by_dept),
        "gps_mismatch_count": sum(1 for v in _col(h, rows, "gps_location_match") if v == "False"),
    }


def answers_student(h, rows) -> dict:
    passed = _col(h, rows, "passed")
    by_subject = _group_mean(_col(h, rows, "subject"), _col(h, rows, "score"))
    return {
        "row_count": len(rows),
        "pass_rate": _r(sum(1 for v in passed if v == "True") / len(rows)),
        "grade_letter_counts": dict(sorted(Counter(_col(h, rows, "grade_letter")).items())),
        "mean_score": _r(statistics.fmean(_floats(_col(h, rows, "score")))),
        "mean_score_by_subject": {k: _r(v) for k, v in sorted(by_subject.items())},
        "top_subject_by_mean_score": _argmax(by_subject),
        "corr_study_hours_score": _r(_pearson(_col(h, rows, "study_hours_per_day"), _col(h, rows, "score"))),
    }


def answers_ecommerce(h, rows) -> dict:
    revenue = _col(h, rows, "revenue")
    weekend = _col(h, rows, "is_weekend")
    by_month = _group_sum([d[:7] for d in _col(h, rows, "date")], revenue)
    wk = [float(r) for r, w in zip(revenue, weekend) if w == "True"]
    wd = [float(r) for r, w in zip(revenue, weekend) if w == "False"]
    return {
        "row_count": len(rows),
        "total_revenue": _r(math.fsum(_floats(revenue))),
        "total_orders": _r(math.fsum(_floats(_col(h, rows, "orders")))),
        "mean_revenue_weekend": _r(statistics.fmean(wk)),
        "mean_revenue_weekday": _r(statistics.fmean(wd)),
        "top_month_by_revenue": _argmax(by_month),
        "corr_visitors_revenue": _r(_pearson(_col(h, rows, "website_visitors"), revenue)),
    }


ANSWER_FUNCS = {
    "sales": answers_sales,
    "attendance": answers_attendance,
    "student": answers_student,
    "ecommerce": answers_ecommerce,
}


def compute(dataset: str) -> dict:
    path = os.path.join(DATA_DIR, DATASETS[dataset])
    header, rows = read_rows(path)
    return {
        "dataset": dataset,
        "file": DATASETS[dataset],
        "sha256": sha256_file(path),
        "method": "python-stdlib (csv, statistics, math); independent of pandas",
        "quality": quality_reference(header, rows),
        "answers": ANSWER_FUNCS[dataset](header, rows),
    }


def expected_path(dataset: str) -> str:
    return os.path.join(EXPECTED_DIR, f"{dataset}.json")


def load_expected(dataset: str) -> dict:
    with open(expected_path(dataset), encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    write = "--write" in sys.argv
    os.makedirs(EXPECTED_DIR, exist_ok=True)
    for name in DATASETS:
        result = compute(name)
        if write:
            with open(expected_path(name), "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, sort_keys=True)
                f.write("\n")
        print(json.dumps(result, indent=2, sort_keys=True))
