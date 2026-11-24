"""
P6 selective task recovery (opt-in Supervisor flag), offline only: FakeProvider, no network, no key, no container.

The diagnostic sample (docs/v2/p6/plan_diagnostic_sample) recorded the EXACT validation problems of every rejected
plan, but not the plan text itself (plan payloads are recorded from 578ce5ec+1 on). The replay fixtures below
therefore rebuild each rejected plan from the reviewed oracle plan with the single defect the diagnostics name, and
assert that the validator reproduces the recorded problem text verbatim before recovery is exercised.
"""

import copy
import json

import pytest

from evaluation import live
from multi_agent import contracts as c
from multi_agent import live_llm
from tests.v2.p5 import scripted

# Exact problems recorded in docs/v2/p6/plan_diagnostic_sample/runs.jsonl (prefix "invalid handoff: " removed).
RECORDED = {
    "student_pass_rate_bool": ("student", ["task 0: claim 'pass_rate': 'value' must be a string of at most 200 characters"]),
    "ecommerce_weekday_bool": ("ecommerce", [
        "task 1: claim 'mean_revenue_weekday': 'value' must be a string of at most 200 characters",
        "task 1: claim 'mean_revenue_weekend': 'value' must be a string of at most 200 characters"]),
    "ecommerce_row_count_args": ("ecommerce", ["task 0: claim 'row_count': args must be an object"]),
    "attendance_missing_titles": ("attendance", ["task 0 is missing ['title']", "task 1 is missing ['title']",
                                                 "task 2 is missing ['title']"]),
    "attendance_gps_bool": ("attendance", ["task 2: claim 'gps_mismatch_count': 'value' must be a string of at most "
                                           "200 characters"]),
}


def _oracle_plan(ds):
    specs = scripted.CLAIM_SPECS[ds]
    return {"tasks": [{"id": t["id"], "title": t["title"], "description": t["description"],
                       "analysis_type": t["analysis_type"],
                       "claims": [{"key": k, "op": specs[k][0], "args": dict(specs[k][1])} for k in t["questions"]]}
                      for t in live.tasks_for(ds)]}


def _claim(plan, key):
    return next(cl for t in plan["tasks"] for cl in t["claims"] if cl["key"] == key)


def _defect(name, plan):
    """The single defect each recorded diagnostic names, applied to an otherwise valid plan."""
    p = copy.deepcopy(plan)
    if name == "student_pass_rate_bool":
        _claim(p, "pass_rate")["args"]["value"] = True
    elif name == "ecommerce_weekday_bool":
        t = next(t for t in p["tasks"] if t["id"] == "eco_t2")
        t["claims"].sort(key=lambda cl: cl["key"] != "mean_revenue_weekday")    # recorded order: weekday first
        _claim(p, "mean_revenue_weekday")["args"]["value"] = False
        _claim(p, "mean_revenue_weekend")["args"]["value"] = True
    elif name == "ecommerce_row_count_args":
        _claim(p, "row_count")["args"] = []
    elif name == "attendance_missing_titles":
        for t in p["tasks"]:
            del t["title"]
    elif name == "attendance_gps_bool":
        _claim(p, "gps_mismatch_count")["args"]["value"] = False
    return p


class Script:
    """Planner replies in order (a callable may inspect the prompt); insights from the oracle."""

    def __init__(self, ds, planner_replies):
        self.oracle = live.oracle_answer(ds)
        self.replies = list(planner_replies)
        self.planner_prompts = []

    def __call__(self, skill, system, user):
        if skill != "planner":
            return self.oracle(skill, system, user)
        self.planner_prompts.append(user)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(user) if callable(r) else json.dumps(r)


def _port(tmp_path, script):
    class P(live_llm.FakeProvider):
        def complete(self, skill, system, user):
            if skill == "planner" and script.replies and isinstance(script.replies[0], Exception):
                script.planner_prompts.append(user)
                raise script.replies.pop(0)
            return super().complete(skill, system, user)
    return live_llm.BudgetedPort(P(script), model="gemini-2.5-flash-lite", budget_usd=1,
                                 ledger=live_llm.Ledger(str(tmp_path / "l.jsonl")), max_calls=200,
                                 max_total_tokens=10 ** 8)


def _run(tmp_path, ds, replies, selective):
    script = Script(ds, replies)
    out = live.run_multi_agent(ds, _port(tmp_path, script), selective_recovery=selective)
    return out, live.grade(ds, out["claims"]), script


def _repaired_tasks(ds, bad_plan):
    """The corrected versions of exactly the invalid tasks (what a successful repair reply contains)."""
    good = {t["id"]: t for t in _oracle_plan(ds)["tasks"]}
    _, _, rejected = c.triage_plan(bad_plan, scripted_profile(ds))
    return {"tasks": [good[t["id"]] for _, t, _ in rejected]}


def scripted_profile(ds):
    from multi_agent import dataset as dsmod
    return dsmod.profile(dsmod.load(live.dataset_path(ds)))


# ── the validator still reproduces every recorded rejection, verbatim (strictness unchanged) ────────────────

@pytest.mark.parametrize("name", sorted(RECORDED))
def test_reconstructed_rejected_plans_reproduce_the_recorded_problems_exactly(name):
    ds, recorded = RECORDED[name]
    bad = _defect(name, _oracle_plan(ds))
    assert c.validate_plan(bad, scripted_profile(ds)) == recorded
    structural, accepted, rejected = c.triage_plan(bad, scripted_profile(ds))
    assert structural == [] and [p for _, _, ps in rejected for p in ps] == recorded
    assert len(accepted) + len(rejected) == len(bad["tasks"])
    assert c.validate_plan(_oracle_plan(ds), scripted_profile(ds)) == []          # the oracle plan itself is valid


# ── replay: recovery repairs only the invalid tasks and reaches the full answer set ─────────────────────────

@pytest.mark.parametrize("name", sorted(RECORDED))
def test_replay_selective_recovery_repairs_only_the_invalid_tasks(tmp_path, name):
    ds, recorded = RECORDED[name]
    bad = _defect(name, _oracle_plan(ds))
    repair = _repaired_tasks(ds, bad)
    out, g, script = _run(tmp_path, ds, [bad, repair], selective=True)
    assert out["status"] == "completed" and g["correct_hidden"] == g["total_hidden"]
    assert len(script.planner_prompts) == 2                                     # same planner-call budget (2)
    rep = script.planner_prompts[1]
    assert "REPAIR ONLY" in rep and f"MAX TASKS: {len(repair['tasks'])}" in rep
    for p in recorded:
        assert p in rep                                                         # exact errors fed back
    bad_ids = {t["id"] for t in repair["tasks"]}
    kept_ids = {t["id"] for t in bad["tasks"]} - bad_ids
    for t in bad["tasks"]:                                                      # only invalid tasks are re-sent
        assert (f"invalid task id {t['id']!r}" in rep) == (t["id"] in bad_ids)
    pr = out["plan_recovery"]
    assert sorted(pr["recovered_task_ids"]) == sorted(bad_ids) and pr["abandoned_tasks"] == []
    assert set(pr["kept_task_ids"]) == kept_ids | bad_ids
    assert out["diagnostics"]["rejected_plans"] and json.loads(out["diagnostics"]["rejected_plans"][0]["plan_json"]) == bad


def test_replay_with_recovery_off_is_the_original_whole_plan_retry(tmp_path):
    ds, _ = RECORDED["student_pass_rate_bool"]
    bad = _defect("student_pass_rate_bool", _oracle_plan(ds))
    out, g, script = _run(tmp_path, ds, [bad, bad], selective=False)            # the model repeats its mistake
    assert out["status"] == "plan_invalid" and g["correct_hidden"] == 0 and "plan_recovery" not in out
    assert "REPAIR ONLY" not in script.planner_prompts[1] and "MAX TASKS: 8" in script.planner_prompts[1]


# ── unrepairable tasks are reported, never silently omitted ─────────────────────────────────────────────────

def test_a_task_that_stays_invalid_is_abandoned_and_reported(tmp_path):
    ds = "student"
    bad = _defect("student_pass_rate_bool", _oracle_plan(ds))
    still_bad = {"tasks": [next(t for t in bad["tasks"] if t["id"] == "stu_t1")]}
    approvals = []
    from tests.v2.p5 import scripted as sc
    orig = sc.approve_all

    def spy(req):
        approvals.append(req)
        return orig(req)
    sc.approve_all = spy
    try:
        out, g, _ = _run(tmp_path, ds, [bad, still_bad], selective=True)
    finally:
        sc.approve_all = orig
    assert out["status"] == "completed_with_failures" and out["run_complete"]
    ab = out["plan_recovery"]["abandoned_tasks"]
    assert [a["id"] for a in ab] == ["stu_t1"] and "pass_rate" in ab[0]["claim_keys"]
    assert any("'value' must be a string" in p for p in ab[0]["problems"])
    assert approvals and approvals[0]["abandoned_tasks"] == ab                  # the human sees what was lost
    assert [t["id"] for t in approvals[0]["plan"]["tasks"]] == ["stu_t2", "stu_t3"]
    assert g["per_key"]["pass_rate"] == "missing" and g["per_key"]["mean_score_by_subject"] == "correct"
    assert live.required_task_completion(ds, g["per_key"]) == (2, 3)


def test_a_provider_error_on_the_repair_keeps_the_valid_tasks(tmp_path):
    ds = "ecommerce"
    bad = _defect("ecommerce_weekday_bool", _oracle_plan(ds))
    out, g, _ = _run(tmp_path, ds, [bad, live_llm.ProviderError("HTTP 503: high demand", status=503)], selective=True)
    assert out["status"] == "completed_with_failures"
    ab = out["plan_recovery"]["abandoned_tasks"]
    assert [a["id"] for a in ab] == ["eco_t2"] and any("ProviderError" in p for p in ab[0]["problems"])
    assert g["per_key"]["total_revenue"] == "correct" and g["per_key"]["mean_revenue_weekday"] == "missing"


def test_a_provider_error_first_then_a_flawed_plan_keeps_its_valid_tasks(tmp_path):
    ds = "attendance"
    bad = _defect("attendance_gps_bool", _oracle_plan(ds))
    out, g, script = _run(tmp_path, ds, [live_llm.ProviderError("HTTP 503", status=503), bad], selective=True)
    assert len(script.planner_prompts) == 2 and "REPAIR ONLY" not in script.planner_prompts[1]
    assert out["status"] == "completed_with_failures" and [a["id"] for a in out["plan_recovery"]["abandoned_tasks"]] == ["att_t3"]
    assert g["per_key"]["present_rate"] == "correct" and g["per_key"]["gps_mismatch_count"] == "missing"
    off, g_off, _ = _run(tmp_path, ds, [live_llm.ProviderError("HTTP 503", status=503), bad], selective=False)
    assert off["status"] == "plan_invalid" and g_off["correct_hidden"] == 0


def test_when_every_task_stays_invalid_the_run_is_plan_invalid(tmp_path):
    ds = "attendance"
    bad = _defect("attendance_missing_titles", _oracle_plan(ds))
    out, g, _ = _run(tmp_path, ds, [bad, bad], selective=True)
    assert out["status"] == "plan_invalid" and g["correct_hidden"] == 0
    assert live.required_task_completion(ds, g["per_key"]) == (0, 3)


# ── whole-plan checks still apply to the merged plan ────────────────────────────────────────────────────────

def test_a_repair_may_not_add_tasks_or_reuse_kept_ids_or_claim_keys(tmp_path):
    ds = "student"
    plan = _oracle_plan(ds)
    bad = _defect("student_pass_rate_bool", plan)
    fixed = next(t for t in plan["tasks"] if t["id"] == "stu_t1")
    extra = copy.deepcopy(plan["tasks"][1])
    extra["id"] = "stu_extra"
    dup_key = copy.deepcopy(fixed)
    dup_key["claims"].append(copy.deepcopy(plan["tasks"][1]["claims"][0]))  # a key owned by a kept task
    prof = scripted_profile(ds)
    kept = [t for t in plan["tasks"] if t["id"] != "stu_t1"]
    _, acc, rej = c.triage_plan({"tasks": [extra]}, prof, kept=kept, allowed_ids={"stu_t1"})
    assert acc == [] and "is not one of the tasks to repair" in rej[0][2][0]
    _, acc, rej = c.triage_plan({"tasks": [dup_key]}, prof, kept=kept, allowed_ids={"stu_t1"})
    assert acc == [] and "duplicate claim key" in " ".join(rej[0][2])
    _, acc, rej = c.triage_plan({"tasks": [fixed, fixed]}, prof, kept=kept, allowed_ids={"stu_t1"})
    assert [t["id"] for t in acc] == ["stu_t1"] and "duplicate id" in " ".join(rej[0][2])
    out, g, _ = _run(tmp_path, ds, [bad, {"tasks": [dup_key, extra]}], selective=True)
    assert out["status"] == "completed_with_failures"
    assert [a["id"] for a in out["plan_recovery"]["abandoned_tasks"]] == ["stu_t1"]


def test_a_structurally_invalid_first_reply_gets_the_original_whole_replan(tmp_path):
    ds = "sales"
    out, g, script = _run(tmp_path, ds, [{"tasks": []}, _oracle_plan(ds)], selective=True)
    assert out["status"] == "completed" and "REPAIR ONLY" not in script.planner_prompts[1]
    assert out["plan_recovery"] is None                                         # recovery never triggered


def test_a_fully_valid_plan_is_identical_with_the_flag_on_or_off(tmp_path):
    for ds in live.DATASETS:
        (tmp_path / ds).mkdir()
        on, g_on, s_on = _run(tmp_path / ds, ds, [_oracle_plan(ds)], selective=True)
        off, g_off, s_off = _run(tmp_path / ds, ds, [_oracle_plan(ds)], selective=False)
        assert g_on == g_off and on["status"] == off["status"] == "completed"
        assert {k: v for k, v in on.items() if k != "plan_recovery"} == off and on["plan_recovery"] is None
        assert s_on.planner_prompts == s_off.planner_prompts


# ── independent verification still decides what is accepted ─────────────────────────────────────────────────

def test_recovered_tasks_still_pass_through_independent_verification(tmp_path):
    from multi_agent.agents import VerificationAgent
    calls = []
    orig = VerificationAgent.run

    def spy(self, kind, payload):
        calls.append((kind, payload.get("task_id")))
        return orig(self, kind, payload)
    VerificationAgent.run = spy
    try:
        ds = "student"
        bad = _defect("student_pass_rate_bool", _oracle_plan(ds))
        out, g, _ = _run(tmp_path, ds, [bad, _repaired_tasks(ds, bad)], selective=True)
    finally:
        VerificationAgent.run = orig
    assert ("verification_request", "stu_t1") in calls and g["per_key"]["pass_rate"] == "correct"


# ── no hidden answer reaches the repair prompt ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(RECORDED))
def test_repair_prompts_contain_no_hidden_golden_answer(tmp_path, name):
    from tests.v2.p6.test_live_p6 import _hidden_needles
    ds, _ = RECORDED[name]
    bad = _defect(name, _oracle_plan(ds))
    _, _, script = _run(tmp_path, ds, [bad, _repaired_tasks(ds, bad)], selective=True)
    for needle in _hidden_needles(ds):
        assert needle not in script.planner_prompts[1], needle


# ── the corrected task-completion denominator ───────────────────────────────────────────────────────────────

def test_required_task_completion_counts_every_required_task_for_every_run():
    for ds in live.DATASETS:
        n = len(live.tasks_for(ds))
        assert live.required_task_completion(ds, {}) == (0, n)                 # e.g. a plan_invalid run
        assert live.required_task_completion(ds, {k: "incorrect" for k in live.QUESTIONS[ds]}) == (n, n)


# ── the paired experiment runner (offline) ──────────────────────────────────────────────────────────────────

def _fake_make(tmp_path, budget):
    ledger = live_llm.Ledger(str(tmp_path / "exp.jsonl"))

    def make(label):
        return live_llm.BudgetedPort(live_llm.FakeProvider(live.oracle_answer(label.split("/")[1])),
                                     model="gemini-2.5-flash-lite", budget_usd=budget, ledger=ledger, max_calls=100,
                                     max_total_tokens=10 ** 8, run_label=label)
    return make


def test_the_experiment_pairs_every_baseline_multi_agent_slot_with_a_control(tmp_path):
    exp = live.run_recovery_experiment(_fake_make(tmp_path, 1.0), seed=20261124, log=lambda *a: None,
                                       incremental=str(tmp_path / "runs.jsonl"))
    base = [(ds, rep) for arch, ds, rep in live.schedule(live.DATASETS, 5, 20261124) if arch == "multi_agent"]
    assert [tuple(x) for x in exp["slots"]] == base and len(base) == 20          # the baseline's workload and order
    assert len(exp["results"]) == 40 and exp["skipped_controls"] == [] and exp["stopped"] is None
    for si in range(20):
        pair = [r for r in exp["results"] if r["slot"] == si]
        assert sorted(r["variant"] for r in pair) == ["control", "selective_recovery"]
        assert abs(pair[0]["order"] - pair[1]["order"]) == 1                    # adjacent: same provider conditions
    s = exp["summary"]
    assert s["matched_pairs"] == 20 and s["selective_recovery"]["required_task_completion_rate"] == 1.0
    assert s["control"]["complete_planner_failure_rate"] == 0 and s["selective_recovery"]["call_source"] == [live_llm.MOCK]
    assert len(open(tmp_path / "runs.jsonl").read().splitlines()) == 40


@pytest.mark.parametrize("budget,skips", [(0.010, True), (0.05, False)])
def test_controls_are_skipped_before_the_optimised_runs_lose_their_budget(tmp_path, budget, skips):
    exp = live.run_recovery_experiment(_fake_make(tmp_path, budget), seed=20261124, log=lambda *a: None,
                                       reserve_per_run=0.0012)
    opt = [r for r in exp["results"] if r["variant"] == "selective_recovery"]
    ctl = [r for r in exp["results"] if r["variant"] == "control"]
    assert exp["stopped"] is None and len(opt) == 20                          # the optimised arm always completes
    assert len(ctl) + len(exp["skipped_controls"]) == 20 and bool(exp["skipped_controls"]) is skips


# ── review fix: a repair must keep the original task's required claim keys ─────────────────────────────────

def test_a_repair_that_drops_a_required_question_is_not_treated_as_complete(tmp_path):
    ds = "student"
    plan = _oracle_plan(ds)
    bad = _defect("student_pass_rate_bool", plan)
    dropped = copy.deepcopy(next(t for t in plan["tasks"] if t["id"] == "stu_t1"))
    dropped["claims"] = [cl for cl in dropped["claims"] if cl["key"] != "pass_rate"]   # valid, but loses a question
    prof = scripted_profile(ds)
    kept = [t for t in plan["tasks"] if t["id"] != "stu_t1"]
    _, acc, rej = c.triage_plan({"tasks": [dropped]}, prof, kept=kept, allowed_ids={"stu_t1"},
                                required_keys={"stu_t1": ["row_count", "pass_rate", "mean_score", "grade_letter_counts"]})
    assert acc == [] and rej[0][2] == ["task 0: repair must keep the original claim keys; missing ['pass_rate']"]
    out, g, _ = _run(tmp_path, ds, [bad, {"tasks": [dropped]}], selective=True)
    assert out["status"] == "completed_with_failures" and out["plan_recovery"]["recovered_task_ids"] == []
    ab = out["plan_recovery"]["abandoned_tasks"]
    assert [a["id"] for a in ab] == ["stu_t1"] and "pass_rate" in ab[0]["claim_keys"]
    assert any("missing ['pass_rate']" in p for p in ab[0]["problems"])
    assert "stu_t1" not in out["tasks"] and g["per_key"]["pass_rate"] == "missing"
    assert live.required_task_completion(ds, g["per_key"]) == (2, 3)
    # a complete repair (all original keys, value fixed) is still accepted
    ok, g2, _ = _run(tmp_path, ds, [bad, _repaired_tasks(ds, bad)], selective=True)
    assert ok["status"] == "completed" and ok["plan_recovery"]["recovered_task_ids"] == ["stu_t1"]
