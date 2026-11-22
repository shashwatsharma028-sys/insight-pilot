"""P5.0: the Verification Agent recomputes claims from the data; it never trusts the analyst."""

import ast
import csv
import inspect
import os
import random

import pandas as pd
import pytest

from multi_agent import dataset as dsmod
from multi_agent import verification as v
from multi_agent.agents import VerificationAgent
from tests.v2.golden.compare import values_match as golden_match
from tests.v2.golden.reference import DATA_DIR, DATASETS
from tests.v2.p5 import scripted


def _spec(dataset, key):
    op, args = scripted.CLAIM_SPECS[dataset][key]
    return {"key": key, "op": op, "args": dict(args)}


def _request(dataset, submitted, keys=None, errors=None, sha=None, path=None):
    path = path or os.path.join(DATA_DIR, DATASETS[dataset])
    keys = keys or sorted(submitted)
    return {"task_id": "t", "attempt": 1, "claims_spec": [_spec(dataset, k) for k in keys], "submitted": submitted,
            "claim_errors": errors or {}, "unplanned": [], "dataset_path": path,
            "dataset_sha256": sha or dsmod.sha256_file(path)}


# ── independence and correctness of the recomputation ────────────────────────

@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_recomputation_equals_the_separately_implemented_golden_answers(dataset):
    """multi_agent.verification and tests/v2/golden/reference.py are two implementations; all 28 must agree."""
    ds = dsmod.load(os.path.join(DATA_DIR, DATASETS[dataset]))
    golden = scripted.golden_answers(dataset)
    for key, expected in golden.items():
        value, evidence = v.recompute(_spec(dataset, key), ds)
        assert golden_match(value, expected), (key, value, expected)
        assert isinstance(evidence, dict) and evidence


def test_verifier_does_not_import_the_golden_reference_or_pandas():
    imported = set()
    for mod in (v, dsmod):
        for node in ast.walk(ast.parse(inspect.getsource(mod))):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
    assert imported <= {"csv", "hashlib", "os", "re", "math", "collections", "dataclasses", "multi_agent"}, imported


@pytest.fixture(scope="module")
def synthetic_csv(tmp_path_factory):
    """Deterministic random data with pandas NA tokens, to cross-check every op against pandas."""
    rng = random.Random(20261122)
    path = tmp_path_factory.mktemp("syn") / "syn.csv"
    na = ["", "NA", "null", "NaN", "n/a"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["g", "x", "y", "flag", "day"])
        for i in range(400):
            g = rng.choice(["a", "b", "c", "d"]) if rng.random() > 0.05 else rng.choice(na)
            x = f"{rng.uniform(-50, 200):.3f}" if rng.random() > 0.1 else rng.choice(na)
            y = f"{rng.gauss(10, 4):.4f}" if rng.random() > 0.1 else rng.choice(na)
            w.writerow([g, x, y, rng.choice(["True", "False"]), f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"])
    return str(path)


def test_every_op_matches_pandas_on_data_with_missing_values(synthetic_csv):
    ds = dsmod.load(synthetic_csv)
    df = pd.read_csv(synthetic_csv)
    s = df.astype({"flag": str})
    checks = {
        ("count_rows", ()): len(df),
        ("sum", (("column", "x"),)): df["x"].sum(),
        ("mean", (("column", "y"),)): df["y"].mean(),
        ("group_sum", (("by", "g"), ("column", "x"))): df.groupby("g")["x"].sum().to_dict(),
        ("group_mean", (("by", "g"), ("column", "y"))): df.groupby("g")["y"].mean().to_dict(),
        ("argmax_group_sum", (("by", "g"), ("column", "x"))): df.groupby("g")["x"].sum().idxmax(),
        ("argmax_group_mean", (("by", "g"), ("column", "y"))): df.groupby("g")["y"].mean().idxmax(),
        ("group_sum", (("by", "day"), ("column", "x"), ("by_prefix", 7))):
            df.groupby(df["day"].str[:7])["x"].sum().to_dict(),
        ("ratio_of_sums_pct", (("numerator", "x"), ("denominator", "y"))): df["x"].sum() / df["y"].sum() * 100,
        ("count_where_equals", (("column", "flag"), ("value", "True"))): int((s["flag"] == "True").sum()),
        ("rate_where_equals", (("column", "flag"), ("value", "False"))): float((s["flag"] == "False").mean()),
        ("value_counts", (("column", "g"),)): df["g"].value_counts().to_dict(),
        ("mean_where_equals", (("column", "x"), ("where_column", "flag"), ("value", "True"))):
            df.loc[s["flag"] == "True", "x"].mean(),
        ("pearson", (("x", "x"), ("y", "y"))): df["x"].corr(df["y"]),
    }
    for (op, args), expected in checks.items():
        value, _ = v.recompute({"key": "k", "op": op, "args": dict(args)}, ds)
        if isinstance(expected, dict):
            expected = {str(k): (int(x) if op == "value_counts" else float(x)) for k, x in expected.items()}
        elif not isinstance(expected, str):
            expected = float(expected)
        assert v.values_match(value, expected), (op, value, expected)


def test_unverifiable_inputs_raise_instead_of_guessing(tmp_path):
    p = tmp_path / "flat.csv"
    p.write_text("x,y,t\n1,5,a\n1,5,b\n", encoding="utf-8")
    ds = dsmod.load(str(p))
    with pytest.raises(v.Unverifiable, match="zero variance"):
        v.recompute({"key": "k", "op": "pearson", "args": {"x": "x", "y": "y"}}, ds)
    with pytest.raises(v.Unverifiable, match="denominator"):
        v.recompute({"key": "k", "op": "ratio_of_sums_pct", "args": {"numerator": "x", "denominator": "z"}},
                    dsmod.Dataset(ds.path, ds.sha256, ("x", "z"), (("1", "0"),)))
    with pytest.raises(v.Unverifiable, match="non-numeric"):
        v.recompute({"key": "k", "op": "sum", "args": {"column": "t"}}, ds)
    with pytest.raises(dsmod.DatasetError):
        v.recompute({"key": "k", "op": "sum", "args": {"column": "nope"}}, ds)


# ── comparison semantics ─────────────────────────────────────────────────────

@pytest.mark.parametrize("reported,expected,ok", [
    (293, 293, True), (293.0, 293, True), (1e6 * (1 + 5e-10), 1e6, True), (1e6 + 0.01, 1e6, False),
    (0.1234565, 0.123456, True), (0.123458, 0.123456, False),
    (True, 1, False), (1, True, False), (True, True, True),
    ("North", "North", True), ("north", "North", False), ("293", 293, False), (None, 0, False),
    ({"a": 1.0, "b": 2.0}, {"a": 1, "b": 2}, True), ({"a": 1.0}, {"a": 1, "b": 2}, False),
    ({"a": 1, "b": 2, "c": 0}, {"a": 1, "b": 2}, False), ({"a": 1.5, "b": 2}, {"a": 1, "b": 2}, False),
    ([1, 2], [1, 2], False),       # lists are not a claim type
])
def test_values_match(reported, expected, ok):
    assert v.values_match(reported, expected) is ok


@pytest.mark.parametrize("value,ok", [(1.0, True), ("x", True), ({"a": 1}, True), (float("nan"), False),
                                      (float("inf"), False), ({"a": float("-inf")}, False), (object(), False)])
def test_finite(value, ok):
    assert v.finite(value) is ok


# ── verification reports ─────────────────────────────────────────────────────

def test_correct_claims_are_verified_with_evidence():
    golden = scripted.golden_answers("sales")
    report = v.verify_claims(_request("sales", dict(golden)))
    assert report["verdict"] == "accepted" and report["dataset_integrity"] is True
    assert all(e["status"] == "verified" and e["evidence"] for e in report["claims"].values())
    assert report["claims"]["mean_customer_satisfaction"]["evidence"]["non_missing"] == 287
    assert report["feedback"] == []


def test_each_failure_status_and_feedback_never_leaks_the_recomputed_value():
    golden = scripted.golden_answers("sales")
    submitted = dict(golden)
    submitted["total_sales_amount"] = golden["total_sales_amount"] * 1.01
    submitted["top_region"] = "South"
    submitted["total_returns"] = float("nan")
    submitted.pop("row_count")
    errors = {"target_attainment_pct": "value is not valid JSON"}
    submitted.pop("target_attainment_pct")
    report = v.verify_claims(_request("sales", submitted, keys=sorted(golden), errors=errors))
    st = {k: e["status"] for k, e in report["claims"].items()}
    assert st == {"mean_customer_satisfaction": "verified", "row_count": "missing", "sales_by_region": "verified",
                  "target_attainment_pct": "malformed", "top_region": "refuted", "total_returns": "malformed",
                  "total_sales_amount": "refuted"}
    assert report["verdict"] == "rejected"
    text = " ".join(report["feedback"])
    for leaked in ("North", "13573929", "13,573,929", "1799", "93.13", "293"):
        assert leaked not in text


def test_dataset_changed_after_profiling_makes_every_claim_unverifiable(tmp_path):
    src = os.path.join(DATA_DIR, DATASETS["sales"])
    original_sha = dsmod.sha256_file(src)
    copy_path = tmp_path / "sales.csv"
    copy_path.write_bytes(open(src, "rb").read().replace(b"North", b"Nxrth", 1))
    report = v.verify_claims(_request("sales", dict(scripted.golden_answers("sales")), path=str(copy_path),
                                      sha=original_sha))
    assert report["dataset_integrity"] is False and report["verdict"] == "rejected"
    assert {e["status"] for e in report["claims"].values()} == {"unverifiable"}


def test_unreadable_dataset_is_unverifiable_not_accepted(tmp_path):
    report = v.verify_claims(_request("sales", {"row_count": 293}, path=str(tmp_path / "missing.csv"), sha="0" * 64))
    assert report["verdict"] == "rejected" and report["claims"]["row_count"]["status"] == "unverifiable"


def test_unplanned_claims_are_reported_but_never_verified():
    req = _request("sales", {"row_count": 293, "bonus": 1}, keys=["row_count"])
    req["unplanned"] = ["other"]
    report = v.verify_claims(req)
    assert report["verdict"] == "accepted" and set(report["claims"]) == {"row_count"}
    assert report["unplanned"] == ["bonus", "other"]


def test_verification_agent_has_no_llm_and_rejects_unknown_kinds():
    agent = VerificationAgent()
    assert not hasattr(agent, "llm")
    with pytest.raises(TypeError):
        VerificationAgent(llm=object())        # noqa
    assert agent.spec.tools == ("dataset_reader",)
    from multi_agent.contracts import HandoffRejected
    with pytest.raises(HandoffRejected):
        agent.run("plan_request", {})


# ── insight grounding ────────────────────────────────────────────────────────

VERIFIED = {"total_sales_amount": 13573929.0, "top_region": "North", "present_rate": 0.62,
            "sales_by_region": {"East": 3541525.0, "North": 3850817.0}, "top_month_by_revenue": "2024-03",
            "grade_letter_counts": {"1": 10, "2": 5}}


def _check(*insights):
    return v.check_insights({"task_id": "t", "verified_claims": VERIFIED,
                             "insights": [{"text": t, "cites": c} for t, c in insights]})


@pytest.mark.parametrize("text,cites", [
    ("Total sales were 13,573,929.00.", ["total_sales_amount"]),
    ("North leads.", ["top_region"]),
    ("62% of days were present (0.62).", ["present_rate"]),
    ("North sold 3,850,817.00 and East 3,541,525.", ["sales_by_region"]),
    ("Revenue peaked in 2024-03.", ["top_month_by_revenue"]),
    ("Grade 1 appears 10 times.", ["grade_letter_counts"]),
])
def test_grounded_insights_are_accepted(text, cites):
    assert _check((text, cites))["verdict"] == "accepted", _check((text, cites))


@pytest.mark.parametrize("insights,needle", [
    ([("Sales grew 37.5% year on year.", ["total_sales_amount"])], "37.5%"),
    ([("North leads.", ["churn_rate"])], "not verified"),
    ([("North leads.", [])], "cites no claim"),
    ([("Total sales were 13,573,930.", ["total_sales_amount"])], "13,573,930"),
    ([("Present rate was 62%.", ["top_region"])], "62%"),       # number not supported by the claim it cites
    ([("Revenue peaked in 2024-04.", ["top_month_by_revenue"])], "2024"),
    ([("Grade 12 appears 10 times.", ["grade_letter_counts"])], "12"),
    ([], "no insights"),
    # conservative by design: rescaled units ("13.6 million") are not traced, so they are rejected
    ([("Total sales were about 13.6 million.", ["total_sales_amount"])], "13.6"),
])
def test_ungrounded_insights_are_rejected(insights, needle):
    report = _check(*insights)
    assert report["verdict"] == "rejected" and any(needle in p for p in report["problems"]), report
