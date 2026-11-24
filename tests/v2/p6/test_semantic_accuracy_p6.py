"""
P6 optimisation 4: semantic planner guidance + evaluation-only semantic diagnostics (offline only).

Confirmed mechanisms (docs/v2/p6/SEMANTIC_ACCURACY.md, from recorded accepted plans re-run through the engine):
  1. grouping granularity: argmax_group_sum by a text month-NAME column merges a month across years and returns a
     name, where the question asked for a calendar month (date column, by_prefix 7);
  2. result shape: group_mean by a boolean column returns a mapping where the question asked for one filtered mean
     (mean_where_equals).
These tests use NOVEL synthetic datasets and questions (not the benchmark's) to check the guidance states true,
general semantics, and that the diagnostics classify such errors without touching prompts or grading. Mock-provider
tests say nothing about real model reasoning.
"""

import ast
import copy
import csv
import inspect
import json
import subprocess

import pytest

from evaluation import live
from multi_agent import claims, op_engine, prompts, verification
from multi_agent import dataset as dsmod
from tests.v2.p6 import test_selective_recovery_p6 as sr
from tests.v2.p6.test_live_p6 import _hidden_needles

# sha256 of the schema-only (op_schema) planner and claims prompts at bb2847d9: unchanged by this branch.
SCHEMA_ONLY_BB2847D9 = {
    "sales": ("4a2f5507d4493cf2a9879f50af06a188922b8347ef481587e9d17236c0583d54",
              "68f49c79d56f252f67789c24829a5a16d4c0a2c3bc0ccd8313dcf26ca40f0b05"),
    "attendance": ("4a56d8d5eb529fae002f1aad5e8ce28b2d0f19adc8960f1f6dd18482de3750ef",
                   "d322682b55c3a8c38275938d46fcf6d1c7877017c0a00dc5efd6288320f9f86d"),
    "student": ("1885a5218d5beb7a74805731c66a3a6a657185faaa283195d12eee862ecfa7c1",
                "d36b88c9e8edff9be52e2d714b4ca76cfbfa1edd708e91b42678dde2b27b4d35"),
    "ecommerce": ("e2a5ece4c8dfe990ad10205815056a5c2e8061db330c51c01ce1a6eb9bb262ae",
                  "1fa48cd4320cc30ca3e4f88a702b150f32cd69bd86dfeee6f0d65ea16628b00f"),
}

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


# ── a novel synthetic workload (not a benchmark dataset) ────────────────────────────────────────────────────

@pytest.fixture
def shop(tmp_path):
    """Two years of daily rows. Calendar month 2021-12 has the single largest total (31 x 200), but the month NAME
    'March' has the largest total across both years (2 x 31 x 160 > 31 x 200 + 31 x 100), so the two groupings
    disagree by construction."""
    import datetime as dt
    rows, d = [], dt.date(2021, 1, 1)
    while d.year < 2023:
        sales = 100.0 + (60.0 if d.month == 3 else 0.0) + (100.0 if (d.year, d.month) == (2021, 12) else 0.0)
        promo = "True" if d.day % 3 == 0 else "False"
        sales += 25.0 if promo == "True" else 0.0
        rows.append({"day": d.isoformat(), "month_name": MONTHS[d.month - 1], "promo": promo,
                     "region": "AB"[d.day % 2], "sales": f"{sales:.2f}", "units": str(d.day % 7 + 1)})
        d += dt.timedelta(days=1)
    rows[5]["units"] = ""                                                          # one missing value
    path = tmp_path / "shop.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return str(path)


def _both(spec, path):
    """The trusted engine's and the independent verifier's value for one claim spec."""
    engine = op_engine.compute(spec, op_engine.Frame(path))
    verifier = verification.recompute(spec, dsmod.load(path))[0]
    return engine, verifier


def _close(a, b):
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    if isinstance(a, float) or isinstance(b, float):
        return abs(a - b) < 1e-9
    return a == b


# ── every OP_RESULTS description is true for BOTH implementations ───────────────────────────────────────────

def test_op_results_cover_exactly_the_op_table():
    assert list(claims.describe_op_results()) == list(claims.OPS) and set(claims.OP_RESULTS) == set(claims.OPS)


SHAPES = {  # op -> (spec args on the shop dataset, expected result type)
    "count_rows": ({}, int), "sum": ({"column": "sales"}, float), "mean": ({"column": "units"}, float),
    "group_sum": ({"by": "region", "column": "sales"}, dict), "group_mean": ({"by": "promo", "column": "sales"}, dict),
    "argmax_group_sum": ({"by": "day", "column": "sales", "by_prefix": 7}, str),
    "argmax_group_mean": ({"by": "region", "column": "sales"}, str),
    "ratio_of_sums_pct": ({"numerator": "units", "denominator": "sales"}, float),
    "count_where_equals": ({"column": "promo", "value": "True"}, int),
    "rate_where_equals": ({"column": "promo", "value": "True"}, float),
    "value_counts": ({"column": "region"}, dict),
    "mean_where_equals": ({"column": "sales", "where_column": "promo", "value": "False"}, float),
    "pearson": ({"x": "units", "y": "sales"}, float),
}


@pytest.mark.parametrize("op", sorted(claims.OPS))
def test_each_op_result_description_matches_engine_and_verifier(shop, op):
    args, typ = SHAPES[op]
    spec = {"key": "k", "op": op, "args": args}
    assert claims.validate_claim(spec, dsmod.profile(dsmod.load(shop))) == []
    engine, verifier = _both(spec, shop)
    assert _close(engine, verifier)
    desc = claims.OP_RESULTS[op]
    assert isinstance(engine, typ) or (typ is float and isinstance(engine, (int, float)))
    assert desc.startswith("a mapping") == (typ is dict)
    assert desc.startswith("one group") == (typ is str)
    if desc.startswith("one integer"):
        assert isinstance(engine, int)
    if "from 0 to 1" in desc:
        assert 0.0 <= engine <= 1.0
    if "from -1 to 1" in desc:
        assert -1.0 <= engine <= 1.0


def test_rate_is_divided_by_all_rows_and_matching_is_exact_text(shop):
    rows = list(csv.DictReader(open(shop)))
    hits = sum(r["promo"] == "True" for r in rows)
    spec = {"key": "k", "op": "rate_where_equals", "args": {"column": "promo", "value": "True"}}
    assert _both(spec, shop) == (hits / len(rows), hits / len(rows))
    for wrong in ("true", "TRUE", " True", "1"):                                 # structurally valid, matches nothing
        s = {"key": "k", "op": "count_where_equals", "args": {"column": "promo", "value": wrong}}
        assert claims.validate_claim(s, dsmod.profile(dsmod.load(shop))) == [] and _both(s, shop) == (0, 0)


def test_date_prefixes_group_by_calendar_month_and_year(shop):
    m = {"key": "k", "op": "group_sum", "args": {"by": "day", "column": "sales", "by_prefix": 7}}
    y = {"key": "k", "op": "group_sum", "args": {"by": "day", "column": "sales", "by_prefix": 4}}
    months, years = _both(m, shop)[0], _both(y, shop)[0]
    assert all(len(k) == 7 and k[4] == "-" for k in months) and set(years) == {"2021", "2022"}
    assert len(months) == 24
    rules = " ".join(claims.SEMANTIC_RULES)
    assert "every cell of a date column starts with yyyy-mm-dd" in rules.lower()
    assert "by_prefix 7 groups by calendar month (YYYY-MM)" in rules and "by_prefix 4 by calendar year (YYYY)" in rules
    assert dsmod._DATE.pattern == r"^\d{4}-\d{2}-\d{2}"                             # the kind contract the rule states


def test_ties_go_to_the_first_group_in_sorted_order(tmp_path):
    p = tmp_path / "tie.csv"
    p.write_text("g,v\nb,1\na,1\nc,0\n")
    spec = {"key": "k", "op": "argmax_group_sum", "args": {"by": "g", "column": "v"}}
    assert _both(spec, str(p)) == ("a", "a")


# ── the confirmed mechanisms, reproduced on novel data: valid plans, wrong answers ─────────────────────────

def test_grouping_by_a_month_name_column_merges_years_and_answers_a_different_question(shop):
    prof = dsmod.profile(dsmod.load(shop))
    by_name = {"key": "top_period", "op": "argmax_group_sum", "args": {"by": "month_name", "column": "sales"}}
    by_month = {"key": "top_period", "op": "argmax_group_sum", "args": {"by": "day", "column": "sales", "by_prefix": 7}}
    assert claims.validate_claim(by_name, prof) == [] == claims.validate_claim(by_month, prof)
    assert _both(by_name, shop) == ("March", "March")                              # merged across two years
    assert _both(by_month, shop) == ("2021-12", "2021-12")                         # the calendar month
    ref = ("argmax_group_sum", {"by": "day", "column": "sales", "by_prefix": 7})
    assert live.semantic_category(live._spec_record("t", by_name), ref) == "grouping"


def test_a_grouped_mean_is_not_a_filtered_mean(shop):
    prof = dsmod.profile(dsmod.load(shop))
    grouped = {"key": "avg_no_promo", "op": "group_mean", "args": {"by": "promo", "column": "sales"}}
    filtered = {"key": "avg_no_promo", "op": "mean_where_equals",
                "args": {"column": "sales", "where_column": "promo", "value": "False"}}
    assert claims.validate_claim(grouped, prof) == [] == claims.validate_claim(filtered, prof)
    g, f = _both(grouped, shop)[0], _both(filtered, shop)[0]
    assert isinstance(g, dict) and isinstance(f, float) and abs(g["False"] - f) < 1e-9   # right number, wrong shape
    ref = ("mean_where_equals", {"column": "sales", "where_column": "promo", "value": "False"})
    assert live.semantic_category(live._spec_record("t", grouped), ref) == "result_shape"


@pytest.mark.parametrize("spec,ref,cat", [
    ({"op": "rate_where_equals", "args": {"column": "promo", "value": "true"}},
     ("rate_where_equals", {"column": "promo", "value": "True"}), "value_representation"),
    ({"op": "rate_where_equals", "args": {"column": "promo", "value": "False"}},
     ("rate_where_equals", {"column": "promo", "value": "True"}), "filter_value"),
    ({"op": "sum", "args": {"column": "units"}}, ("sum", {"column": "sales"}), "column"),
    ({"op": "mean", "args": {"column": "sales"}}, ("sum", {"column": "sales"}), "operation"),
    ({"op": "sum", "args": {"column": "sales"}}, ("sum", {"column": "sales"}), "same_spec"),
    (None, ("sum", {"column": "sales"}), "spec_unrecorded"),
])
def test_semantic_categories(spec, ref, cat):
    rec = None if spec is None else {"task_id": "t", "key": "k", **spec}
    assert live.semantic_category(rec, ref) == cat


# ── the guidance: content, independence, compatibility, no leakage ─────────────────────────────────────────

def _req(ds, **kw):
    prof = dsmod.profile(dsmod.load(live.dataset_path(ds)))
    return {"attempt": 1, "goal": live.multi_goal(live.tasks_for(ds)), "profile": prof, "max_tasks": 8,
            "feedback": [], **kw}


@pytest.mark.parametrize("ds", live.DATASETS)
def test_schema_only_prompts_are_byte_identical_to_bb2847d9(ds):
    import hashlib
    prof = _req(ds)["profile"]
    p = prompts.planning(_req(ds, op_schema=True))
    q = live.single_claims_prompt(prof, live.tasks_for(ds)[0], 1, [], op_schema=True)
    assert (hashlib.sha256(p.encode()).hexdigest(), hashlib.sha256(q.encode()).hexdigest()) == SCHEMA_ONLY_BB2847D9[ds]


@pytest.mark.parametrize("schema,semantic", [(False, False), (True, False), (False, True), (True, True)])
def test_the_two_sections_are_independently_switchable(schema, semantic):
    p = prompts.planning(_req("ecommerce", op_schema=schema, op_semantics=semantic))
    assert (prompts.op_schema_section() in p) is schema and (prompts.op_semantics_section() in p) is semantic
    base = prompts.planning(_req("ecommerce"))
    assert p.replace(prompts.op_schema_section(), "").replace(prompts.op_semantics_section(), "") == base
    if schema and semantic:
        assert p.index(prompts.op_schema_section()) < p.index(prompts.op_semantics_section())


@pytest.mark.parametrize("ds", live.DATASETS)
def test_the_semantics_section_leaks_nothing_and_is_dataset_agnostic(ds):
    sec = prompts.op_semantics_section()
    for needle in _hidden_needles(ds):
        assert needle not in sec, needle
    prof = _req(ds)["profile"]
    for col in prof["columns"]:
        assert f'"{col["name"]}"' not in sec                                     # no column is named
    assert ds not in sec.lower() and prof["file_name"] not in sec
    assert "profile" not in inspect.signature(prompts.op_semantics_section).parameters
    assert prompts.op_semantics_section() == sec                                  # one constant for every dataset
    for k in live.QUESTIONS[ds]:
        assert k not in sec                                                        # no benchmark question key


def test_no_benchmark_question_or_dataset_specific_rule_is_hardcoded():
    src = (json.dumps(claims.OP_RESULTS) + " ".join(claims.SEMANTIC_RULES) + prompts.op_semantics_section()
           + inspect.getsource(prompts.op_semantics_section) + inspect.getsource(claims.describe_op_results))
    for ds in live.DATASETS:
        for k in live.QUESTIONS[ds]:
            assert k not in src, k
    for word in ("revenue", "weekend", "weekday", "is_weekend", "ecommerce", "month\"", "passed", "gps"):
        assert word not in prompts.op_semantics_section()


# ── strict validation, engine and verifier unchanged ───────────────────────────────────────────────────────

@pytest.mark.parametrize("module,func", [("multi_agent/claims.py", "validate_claim"),
                                         ("multi_agent/op_engine.py", "compute"),
                                         ("multi_agent/verification.py", "recompute"),
                                         ("multi_agent/contracts.py", "validate_plan"),
                                         ("multi_agent/contracts.py", "triage_plan")])
def test_validators_engine_and_verifier_are_unchanged_from_bb2847d9(module, func):
    def body(src):
        return next(ast.dump(n) for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == func)
    old = subprocess.run(["git", "show", f"bb2847d9:{module}"], capture_output=True, text=True, check=True).stdout
    assert body(old) == body(open(module).read())


def test_ops_table_is_unchanged():
    old = subprocess.run(["git", "show", "bb2847d9:multi_agent/claims.py"], capture_output=True, text=True,
                         check=True).stdout
    get = lambda src: next(ast.dump(n) for n in ast.parse(src).body if isinstance(n, ast.Assign)   # noqa: E731
                           and getattr(n.targets[0], "id", None) == "OPS")
    assert get(old) == get(open("multi_agent/claims.py").read())


# ── end to end: diagnostics are correct, do not touch grading, and never reach a prompt ────────────────────

def _month_name_plan():
    plan = sr._oracle_plan("ecommerce")
    for t in plan["tasks"]:
        for cl in t["claims"]:
            if cl["key"] == "top_month_by_revenue":
                cl["args"] = {"by": "month", "column": "revenue"}
            if cl["key"] in ("mean_revenue_weekday", "mean_revenue_weekend"):
                cl["op"], cl["args"] = "group_mean", {"by": "is_weekend", "column": "revenue"}
    return plan


def _run(tmp_path, replies, **kw):
    script = sr.Script("ecommerce", replies)
    out = live.run_multi_agent("ecommerce", sr._port(tmp_path, script), selective_recovery=True, **kw)
    return out, live.grade("ecommerce", out["claims"]), script


def test_multi_agent_diagnostics_record_the_accepted_specs_and_classify_semantic_errors(tmp_path):
    out, g, _ = _run(tmp_path, [_month_name_plan()])
    d = out["diagnostics"]
    assert d["accepted_claim_specs"]["top_month_by_revenue"]["args"] == {"by": "month", "column": "revenue"}
    assert d["reported_values"]["top_month_by_revenue"] == "March"
    assert all(v["status"] == "completed" for v in d["verification"].values())
    qd = live.question_diagnostics("ecommerce", "multi_agent", out, g)
    assert qd["top_month_by_revenue"] == {"outcome": "incorrect", "category": "grouping",
                                          "spec": d["accepted_claim_specs"]["top_month_by_revenue"]}
    assert qd["mean_revenue_weekday"]["category"] == qd["mean_revenue_weekend"]["category"] == "result_shape"
    assert qd["total_revenue"] == {"outcome": "correct"}


def test_missing_answers_are_classified_by_stage(tmp_path):
    bad = sr._defect("ecommerce_weekday_bool", sr._oracle_plan("ecommerce"))
    out, g, _ = _run(tmp_path, [bad, bad])                                         # eco_t2 abandoned
    qd = live.question_diagnostics("ecommerce", "multi_agent", out, g)
    assert qd["mean_revenue_weekday"] == {"outcome": "missing", "stage": "abandoned"}
    (tmp_path / "x").mkdir()
    out2, g2, _ = _run(tmp_path / "x", [{"tasks": []}, {"tasks": []}])            # no plan at all
    assert out2["status"] == "plan_invalid"
    assert {q["stage"] for q in live.question_diagnostics("ecommerce", "multi_agent", out2, g2).values()} == {"no_plan"}


def test_diagnostics_do_not_change_grading_or_the_run_output(tmp_path):
    out, g, _ = _run(tmp_path, [_month_name_plan()])
    out_copy, g_copy = copy.deepcopy(out), copy.deepcopy(g)
    live.question_diagnostics("ecommerce", "multi_agent", out, g)
    assert out == out_copy and g == g_copy
    assert live.grade("ecommerce", out["claims"]) == g                              # grade comes from claims only


def test_no_diagnostic_or_reference_spec_ever_reaches_an_agent_prompt(tmp_path):
    from tests.v2.p5 import scripted
    out, _, script = _run(tmp_path, [_month_name_plan()], semantic_guidance=True, schema_aware=True)
    assert len(script.planner_prompts) == 1
    ref = json.dumps(scripted.CLAIM_SPECS["ecommerce"]["top_month_by_revenue"][1], sort_keys=True)
    for p in script.planner_prompts:
        assert "accepted_claim_specs" not in p and "semantic" not in p.split("CLAIM SEMANTICS")[0].lower()
        assert ref not in p and '"by_prefix": 7' not in p


def test_single_agent_diagnostics_record_specs_and_values(tmp_path):
    def answer(skill, system, user):
        r = live.oracle_answer("ecommerce")(skill, system, user)
        return r.replace('"by": "date", "column": "revenue", "by_prefix": 7', '"by": "month", "column": "revenue"')
    port = live_llm_port(tmp_path, answer)
    out = live.run_single_agent("ecommerce", port, semantic_guidance=True)
    g = live.grade("ecommerce", out["claims"])
    d = out["diagnostics"]
    assert d["claim_specs"]["top_month_by_revenue"]["args"] == {"by": "month", "column": "revenue"}
    assert d["reported_values"]["top_month_by_revenue"] == "March"
    assert live.question_diagnostics("ecommerce", "single_agent", out, g)["top_month_by_revenue"]["category"] == "grouping"


def live_llm_port(tmp_path, answer):
    from multi_agent import live_llm
    return live_llm.BudgetedPort(live_llm.FakeProvider(answer), model="gemini-2.5-flash-lite", budget_usd=1,
                                 ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")), max_calls=100,
                                 max_total_tokens=10 ** 8)


def test_values_are_recorded_only_for_the_controlled_benchmark_datasets():
    assert live._bounded_values("not_a_benchmark_dataset", {"k": 1}) == {}
    v = live._bounded_values("sales", {"k": "x" * 5000})
    assert len(v["k"]) <= live.MAX_DIAG_VALUE_CHARS + 3


# ── compatibility with selective recovery and the schema flag; experiment isolation ────────────────────────

def test_selective_recovery_repairs_carry_the_semantics_section(tmp_path):
    bad = sr._defect("ecommerce_weekday_bool", sr._oracle_plan("ecommerce"))
    out, g, script = _run(tmp_path, [bad, sr._repaired_tasks("ecommerce", bad)], semantic_guidance=True)
    assert out["plan_recovery"]["recovered_task_ids"] == ["eco_t2"] and len(script.planner_prompts) == 2
    assert all(prompts.op_semantics_section() in p for p in script.planner_prompts)
    assert all(prompts.op_schema_section() not in p for p in script.planner_prompts)   # independent of schema


@pytest.mark.parametrize("schema,arch", [(False, "multi_agent"), (True, "multi_agent"), (True, "single_agent")])
def test_the_semantic_experiment_isolates_only_the_semantics_section(tmp_path, schema, arch):
    skill = "planner" if arch == "multi_agent" else "single_agent_claims"
    argv = ["--provider", "fake", "--mode", "full", "--experiment", "semantic_planner", "--arch", arch,
            "--out", str(tmp_path)]
    rc = live.main(argv + (["--schema-aware-guidance"] if schema else []))
    exp = json.load(open(tmp_path / "semantic_planner_experiment.json"))
    assert rc == 0 and exp["summary"]["matched_pairs"] == 20 and exp["common_run"] == ({"schema_aware": True} if schema else {})
    for r in exp["results"]:
        sem, sch = r["semantics_audit"][skill], r["prompt_audit"][skill]
        if r["variant"] == "semantic":
            assert sem["without_semantics"] == 0 and sem["with_semantics"] >= 1
        else:
            assert sem["with_semantics"] == 0
        assert (sch["with_guidance"] >= 1 and sch["without_guidance"] == 0) if schema else sch["with_guidance"] == 0
        assert "question_diagnostics" in r and ("plan_recovery" in r["run"]) is (arch == "multi_agent")
    s = exp["summary"]
    for arm in ("semantic", "control"):
        assert {"semantic_errors_by_category", "missing_by_stage", "semantic_error_rate"} <= set(s[arm])


def test_an_experiment_refuses_to_hold_its_own_variable_constant():
    with pytest.raises(ValueError):
        live.run_matched_experiment(lambda *a: None, "semantic_planner", common_run={"semantic_guidance": True})
    with pytest.raises(ValueError):
        live.run_matched_experiment(lambda *a: None, "schema_aware_planner", common_run={"schema_aware": True})
