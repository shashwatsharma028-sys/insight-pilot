"""
P6 optimisation 3: schema-aware planner guidance (offline only; FakeProvider, no network, no key).

Root cause (docs/v2/p6/plan_diagnostic_sample and recovery_experiment): the planner received argument NAMES only
(claims.describe_ops), never their representation, so it wrote JSON booleans for "value" on boolean columns and
"args": [] for count_rows. The opt-in guidance states the representation; the validator is unchanged and strict.
"""

import copy
import hashlib
import json

import pytest

from evaluation import live
from multi_agent import claims, contracts as c, dataset as dsmod, live_llm, prompts
from tests.v2.p6 import test_selective_recovery_p6 as sr
from tests.v2.p6.test_live_p6 import _hidden_needles

# sha256 of the planner and single-agent claims prompts at 170beb00 (before this change), flag off.
LEGACY_PROMPTS = {
    "sales": ("22d20e68de5fd384d6c3541f5a53b4513f219bc9df6b9d05b97f5be887db6836",
              "65271d79925c131b8eea1274ea8c264c522116c6c6f9eaec74ea8a92790c54b0"),
    "attendance": ("7877f0bbf8679794212ee02260be41bee2c53913cebf00b561d67eaa5588450c",
                   "385551830688e2a811f6564e79a0b66746097e410ad7238910ecb22824f011ad"),
    "student": ("3f6d15841e5ff60c63a7255c8d9d081ed652210a43803aefb745892794a30fb7",
                "6ff04e17cca5fce5d280fcf86fc2cfffe13442920f997e66e2cbb75e5a2fb837"),
    "ecommerce": ("d964385b91471065423fa43d094f647836344b952f1e4dba1180d960f65e6feb",
                  "9051cc9879e8baadfb69e540951167c03c491c40ecdf6b336865dd6bc59caa01"),
}


def _profile(ds):
    return dsmod.profile(dsmod.load(live.dataset_path(ds)))


def _req(ds, **kw):
    return {"attempt": 1, "goal": live.multi_goal(live.tasks_for(ds)), "profile": _profile(ds), "max_tasks": 8,
            "feedback": [], **kw}


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ── backward compatibility: flag off is byte-identical to before ────────────────────────────────────────────

@pytest.mark.parametrize("ds", live.DATASETS)
def test_flag_off_prompts_are_byte_identical_to_the_previous_checkpoint(ds):
    planner = prompts.planning(_req(ds))
    single = live.single_claims_prompt(_profile(ds), live.tasks_for(ds)[0], 1, [])
    assert (_sha(planner), _sha(single)) == LEGACY_PROMPTS[ds]
    assert "CLAIM ARGUMENT SCHEMA" not in planner and "CLAIM RULES" not in single
    assert prompts.planning(_req(ds, op_schema=False)) == planner


# ── the guidance itself ─────────────────────────────────────────────────────────────────────────────────────

def test_the_schema_covers_every_op_and_argument_and_is_generated_from_the_validator_table():
    schema = claims.describe_ops_schema()
    assert set(schema) == set(claims.OPS)
    for op, spec in claims.OPS.items():
        assert set(schema[op]) == {a.rstrip("?") for a in spec}
        for name, rule in spec.items():
            text = schema[op][name.rstrip("?")]
            if rule == "str":
                assert "JSON string" in text
            elif rule == "prefix":
                assert "integer" in text and "optional" in text
            else:
                assert text.startswith("column name")
                assert all(kind in text for kind in rule) or text.endswith("any kind")
    affected = sorted(op for op, spec in claims.OPS.items() if "value" in spec)
    assert affected == ["count_where_equals", "mean_where_equals", "rate_where_equals"]


@pytest.mark.parametrize("ds", live.DATASETS)
def test_schema_guidance_appears_in_planner_and_single_agent_prompts_when_on(ds):
    planner = prompts.planning(_req(ds, op_schema=True))
    single = live.single_claims_prompt(_profile(ds), live.tasks_for(ds)[0], 1, [], op_schema=True)
    for p in (planner, single):
        assert prompts.op_schema_section() in p
        assert 'never a JSON boolean, number or null' in p and '"True" or "False"' in p
        assert "use {} for an op that has no arguments" in p
    # the section sits right after the op catalogue and nothing else in the prompt changes
    assert planner.replace(prompts.op_schema_section(), "") == prompts.planning(_req(ds))


@pytest.mark.parametrize("ds", live.DATASETS)
def test_the_guidance_leaks_no_hidden_answer_and_names_no_dataset_value(ds):
    section = prompts.op_schema_section()
    for needle in _hidden_needles(ds):
        assert needle not in section, needle
        assert needle not in prompts.planning(_req(ds, op_schema=True)), needle
    for col in _profile(ds)["columns"]:
        assert f'"{col["name"]}"' not in section                                # no column is named
    assert ds not in section.lower() and _profile(ds)["file_name"] not in section  # no dataset is named
    assert section == prompts.op_schema_section()                               # identical for every dataset


# ── strict validation is unchanged (no coercion) ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["True", "False", "Present", "3", ""])
def test_valid_string_values_are_accepted(value):
    prof = _profile("student")
    claim = {"key": "k", "op": "rate_where_equals", "args": {"column": "passed", "value": value}}
    assert claims.validate_claim(claim, prof) == []


@pytest.mark.parametrize("value", [True, False, 1, 0, 3.5, None, ["True"], {"v": "True"}])
def test_boolean_numeric_and_other_non_string_values_are_still_rejected(value):
    prof = _profile("student")
    for op, extra in (("rate_where_equals", {}), ("count_where_equals", {}),
                      ("mean_where_equals", {"where_column": "passed"})):
        args = {"column": "score" if op == "mean_where_equals" else "passed", "value": value, **extra}
        problems = claims.validate_claim({"key": "k", "op": op, "args": args}, prof)
        assert problems == ["claim 'k': 'value' must be a string of at most 200 characters"]


def test_args_must_still_be_an_object():
    prof = _profile("ecommerce")
    assert claims.validate_claim({"key": "row_count", "op": "count_rows", "args": []}, prof) == [
        "claim 'row_count': args must be an object"]
    assert claims.validate_claim({"key": "row_count", "op": "count_rows", "args": {}}, prof) == []


def test_validation_does_not_depend_on_the_flag():
    """The validator has no flag: the same plan is rejected identically whether or not guidance was given."""
    import inspect
    assert "op_schema" not in inspect.getsource(claims.validate_claim)
    assert "schema_aware" not in inspect.getsource(c.validate_plan) + inspect.getsource(c.triage_plan)
    bad = sr._defect("student_pass_rate_bool", sr._oracle_plan("student"))
    assert c.validate_plan(bad, _profile("student")) == sr.RECORDED["student_pass_rate_bool"][1]


# ── end to end through the Supervisor, with selective recovery unchanged ────────────────────────────────────

def _run(tmp_path, ds, replies, schema_aware, selective=True):
    script = sr.Script(ds, replies)
    out = live.run_multi_agent(ds, sr._port(tmp_path, script), selective_recovery=selective, schema_aware=schema_aware)
    return out, live.grade(ds, out["claims"]), script


def test_every_plan_request_including_repairs_carries_the_guidance_when_on(tmp_path):
    ds = "student"
    bad = sr._defect("student_pass_rate_bool", sr._oracle_plan(ds))
    out, g, script = _run(tmp_path, ds, [bad, sr._repaired_tasks(ds, bad)], schema_aware=True)
    assert out["status"] == "completed" and g["correct_hidden"] == g["total_hidden"]
    assert out["plan_recovery"]["recovered_task_ids"] == ["stu_t1"]            # selective recovery unchanged
    assert len(script.planner_prompts) == 2 and all(prompts.op_schema_section() in p for p in script.planner_prompts)
    assert "REPAIR ONLY" in script.planner_prompts[1]


def test_with_the_flag_off_no_request_carries_the_guidance_and_results_are_identical(tmp_path):
    ds = "attendance"
    bad = sr._defect("attendance_gps_bool", sr._oracle_plan(ds))
    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on, g_on, s_on = _run(tmp_path / "on", ds, [bad, sr._repaired_tasks(ds, bad)], schema_aware=True)
    off, g_off, s_off = _run(tmp_path / "off", ds, [bad, sr._repaired_tasks(ds, bad)], schema_aware=False)
    assert all("CLAIM ARGUMENT SCHEMA" not in p for p in s_off.planner_prompts)
    assert [p.replace(prompts.op_schema_section(), "") for p in s_on.planner_prompts] == s_off.planner_prompts
    assert g_on == g_off and on["status"] == off["status"]                     # the scripted model ignores prompts


def test_a_bad_value_is_still_rejected_and_recovered_with_the_flag_on(tmp_path):
    ds = "ecommerce"
    bad = sr._defect("ecommerce_weekday_bool", sr._oracle_plan(ds))
    out, g, script = _run(tmp_path, ds, [bad, bad], schema_aware=True)       # the model ignores the guidance
    assert out["status"] == "completed_with_failures"
    assert [a["id"] for a in out["plan_recovery"]["abandoned_tasks"]] == ["eco_t2"]
    assert any("'value' must be a string" in p for p in out["plan_recovery"]["abandoned_tasks"][0]["problems"])


def test_the_supervisor_adds_the_request_key_only_when_the_flag_is_on(tmp_path):
    from multi_agent.team import build_team
    from tests.v2.p5 import scripted
    from multi_agent import op_engine
    for flag in (False, True):
        script = sr.Script("sales", [sr._oracle_plan("sales")])
        sup, _ = build_team(sr._port(tmp_path, script), None, scripted.approve_all,
                            analyst=op_engine.TrustedOpsAnalyst())
        res = sup.run(live.dataset_path("sales"), goal=live.multi_goal(live.tasks_for("sales")),
                      limits={"schema_aware_planner": True} if flag else None)
        req = next(m for m in res["messages"] if m["kind"] == "plan_request")["payload"]
        assert ("op_schema" in req) is flag
        assert set(req) - {"op_schema"} == {"goal", "profile", "attempt", "feedback", "max_tasks"}


def test_single_agent_uses_the_guidance_only_when_asked(tmp_path):
    seen = []

    def answer(skill, system, user):
        seen.append((skill, "CLAIM ARGUMENT SCHEMA" in user))
        return live.oracle_answer("sales")(skill, system, user)
    port = live_llm.BudgetedPort(live_llm.FakeProvider(answer), model="gemini-2.5-flash-lite", budget_usd=1,
                                 ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")), max_calls=100,
                                 max_total_tokens=10 ** 8)
    live.run_single_agent("sales", port, schema_aware=True)
    live.run_single_agent("sales", port)
    claims_calls = [flag for skill, flag in seen if skill == "single_agent_claims"]
    assert claims_calls[:3] == [True] * 3 and claims_calls[3:] == [False] * 3
    assert all(not flag for skill, flag in seen if skill == "single_agent_insight")


# ── a controlled experiment is available (offline dry run) ─────────────────────────────────────────────────

@pytest.mark.parametrize("arch", ["multi_agent", "single_agent"])
def test_the_matched_experiment_differs_only_in_the_guidance(tmp_path, arch):
    rc = live.main(["--provider", "fake", "--mode", "full", "--experiment", "schema_aware_planner", "--arch", arch,
                    "--out", str(tmp_path)])
    exp = json.load(open(tmp_path / "schema_aware_planner_experiment.json"))
    assert rc == 0 and exp["summary"]["matched_pairs"] == 20
    assert {r["variant"] for r in exp["results"]} == {"schema_aware", "control"}
    assert all(r["transient_retries"] == 0 for r in exp["results"])            # same provider policy in both arms
    # only the guidance differs: each pair's first claims/planner call is longer by exactly the section's size
    skill = "planner" if arch == "multi_agent" else "single_agent_claims"
    section_tokens = len(prompts.op_schema_section()) / 4                       # FakeProvider: ceil(chars / 4)
    by = {}
    for r in exp["results"]:
        by.setdefault(r["slot"], {})[r["variant"]] = next(x for x in r["llm_records"] if x["skill"] == skill)
    for p in by.values():
        assert abs((p["schema_aware"]["input_tokens"] - p["control"]["input_tokens"]) - section_tokens) <= 1
    # OBSERVED (not inferred): every prompt sent in the schema arm contained the exact section; none in the control
    for r in exp["results"]:
        a = r["prompt_audit"][skill]
        if r["variant"] == "schema_aware":
            assert a["with_guidance"] >= 1 and a["without_guidance"] == 0, r["order"]
        else:
            assert a["with_guidance"] == 0 and a["partial_guidance"] == 0 and a["without_guidance"] >= 1, r["order"]
        for other, b in r["prompt_audit"].items():                             # insight prompts never get it
            if other != skill:
                assert b["with_guidance"] == 0 and b["partial_guidance"] == 0
    s = exp["summary"]
    assert s["schema_aware"]["prompt_audit"][skill]["without_guidance"] == 0
    assert s["control"]["prompt_audit"][skill]["with_guidance"] == 0
    if arch == "multi_agent":
        assert all("plan_recovery" in r["run"] for r in exp["results"])       # selective recovery ON in both arms


def test_the_prompt_audit_observes_initial_and_repair_prompts_and_delegates_everything(tmp_path):
    ds = "student"
    bad = sr._defect("student_pass_rate_bool", sr._oracle_plan(ds))
    for flag in (True, False):
        script = sr.Script(ds, [bad, sr._repaired_tasks(ds, bad)])
        (tmp_path / str(flag)).mkdir()
        port = sr._port(tmp_path / str(flag), script)
        audited = live.PromptAudit(port)
        out = live.run_multi_agent(ds, audited, selective_recovery=True, schema_aware=flag)
        assert out["plan_recovery"]["recovered_task_ids"] == ["stu_t1"]       # initial plan + one repair
        planner = audited.audit["planner"]
        assert planner == ({"with_guidance": 2, "without_guidance": 0, "partial_guidance": 0} if flag
                           else {"with_guidance": 0, "without_guidance": 2, "partial_guidance": 0})
        assert audited.calls == port.calls and audited.records is port.records   # transparent delegation
