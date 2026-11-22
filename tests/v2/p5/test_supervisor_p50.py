"""P5.0: Supervisor orchestration: delegation, handoff validation, bounded retries, HITL, termination."""

import copy
import json
import os

import pytest

from evaluation import multi_agent as ma
from multi_agent import contracts as c
from multi_agent import dataset as dsmod
from multi_agent.agents import VerificationAgent
from multi_agent.team import build_team
from tests.v2.golden.reference import DATA_DIR, DATASETS, ROOT
from tests.v2.p5 import scripted


def _path(d):
    return os.path.join(DATA_DIR, DATASETS[d])


def _team(dataset, faults=None, approval=scripted.approve_all, **kw):
    llm = scripted.ScriptedAgentLLM(dataset, faults, mode="literal")
    port = scripted.LiteralPrintExecution()
    sup, calls = build_team(llm, port, approval, **kw)
    return sup, calls, llm, port


# ── every evaluation case, fast mode ─────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(ma.CASES))
def test_case_meets_its_expectations(name, tmp_path):
    r = ma.run_case(name, str(tmp_path), mode="literal")
    assert r["expectations"]["met"], r["expectations"]["problems"]
    assert r["verification"]["fn"] == 0 and r["final_answers"]["incorrect_accepted"] == []
    assert r["status"] in ("completed", "completed_with_failures", "rejected_by_human", "plan_invalid",
                           "approval_failed", "budget_exhausted", "profile_failed")


def test_fault_suite_detects_every_injected_wrong_claim_and_rejects_no_correct_one(tmp_path):
    suite = ma.run_suite(str(tmp_path), mode="literal")
    s = suite["summary"]
    assert s["expectations_met"] == s["cases"] == len(ma.CASES)
    assert s["verification"]["fn"] == 0 and s["verification"]["fp"] == 0 and s["verification"]["tp"] >= 12
    assert s["insight_checks"]["fn"] == 0 and s["insight_checks"]["fp"] == 0 and s["insight_checks"]["tp"] >= 3
    assert s["incorrect_claims_accepted"] == 0
    assert s["clean_runs_golden_correct"] == s["clean_runs_golden_expected"] == 28
    assert s["provider_calls"] == 0


# ── message log and handoff discipline ───────────────────────────────────────

def test_message_log_is_a_strict_request_reply_alternation():
    sup, calls, _, _ = _team("sales")
    out = sup.run(_path("sales"))
    msgs = out["messages"]
    assert [m["seq"] for m in msgs] == list(range(1, len(msgs) + 1))
    for req, rep in zip(msgs[0::2], msgs[1::2]):
        assert req["sender"] == c.SUPERVISOR and rep["recipient"] == c.SUPERVISOR
        assert rep["reply_to"] == req["seq"] and rep["sender"] == req["recipient"]
        assert rep["kind"] in (c.REPLY_KIND[req["kind"]], "agent_error")
        assert (rep["task_id"], rep["attempt"]) == (req["task_id"], req["attempt"])
    json.dumps(out["messages"])                         # the whole log is serialisable


def test_the_graph_only_connects_agents_through_the_supervisor():
    sup, *_ = _team("sales")
    g = sup.build().get_graph()
    edges = {(e.source, e.target) for e in g.edges}
    for a in c.AGENTS:
        assert (c.SUPERVISOR, a) in edges and (a, c.SUPERVISOR) in edges
        assert not [e for e in edges if e[0] == a and e[1] != c.SUPERVISOR]
    assert ("finalize", "__end__") in edges


class _Wrapper:
    def __init__(self, inner, mutate):
        self.inner, self.mutate, self.spec = inner, mutate, inner.spec

    def run(self, *args):
        return self.mutate(copy.deepcopy(self.inner.run(*args)))


def test_a_reply_for_another_task_is_rejected_and_consumes_an_attempt():
    sup, *_ = _team("student")
    sup.analyst = _Wrapper(sup.analyst, lambda p: {**p, "task_id": "stu_t2"} if p["task_id"] == "stu_t1" else p)
    out = sup.run(_path("student"))
    f = out["final"]
    assert f["tasks"]["stu_t1"]["status"] == "failed" and f["tasks"]["stu_t1"]["attempts"] == 3
    assert f["handoffs_rejected"] == 3 and "row_count" not in f["accepted_claims"]
    assert f["tasks"]["stu_t2"]["status"] == "completed"


def test_an_analyst_smuggling_extra_fields_is_rejected():
    sup, *_ = _team("ecommerce")
    sup.analyst = _Wrapper(sup.analyst, lambda p: {**p, "verdict": "accepted"})
    f = sup.run(_path("ecommerce"))["final"]
    assert f["accepted_claims"] == {} and f["handoffs_rejected"] == 9


def test_a_lying_verifier_cannot_get_claims_accepted():
    """A verifier reply whose verdict contradicts its own per-claim statuses fails closed."""
    def lie(p):
        if "verdict" in p and "claims" in p:
            p["verdict"] = "accepted"
        return p
    golden = scripted.golden_answers("sales")
    faults = {"analysis": {"sales_t3": {a: {"target_attainment_pct": ["scale", 2]} for a in (1, 2, 3)}}}
    sup, *_ = _team("sales", faults)
    sup.verifier = _Wrapper(VerificationAgent(), lie)
    f = sup.run(_path("sales"))["final"]
    assert f["tasks"]["sales_t3"]["status"] == "failed"
    assert "verification unavailable" in f["tasks"]["sales_t3"]["failure"]
    assert "target_attainment_pct" not in f["accepted_claims"]
    assert f["accepted_claims"]["total_sales_amount"] == golden["total_sales_amount"]


def test_agent_exceptions_become_typed_errors_and_are_bounded():
    class Boom:
        spec = None

        def run(self, req):
            raise RuntimeError("model unavailable")
    sup, *_ = _team("attendance")
    sup.insight = Boom()
    out = sup.run(_path("attendance"))
    f = out["final"]
    assert f["status"] == "completed_with_failures"
    assert all(t["status"] == "insight_rejected" and t["insight_attempts"] == 2 for t in f["tasks"].values())
    assert len(f["accepted_claims"]) == 7         # verified claims are kept even without insights
    assert f["agent_errors"] == 6


# ── context boundaries ───────────────────────────────────────────────────────

def _cell_values(dataset):
    ds = dsmod.load(_path(dataset))
    return {v for r in ds.rows for v in r if len(v) >= 4 and not v.replace(".", "").isdigit()} - set(ds.header)


@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_planning_and_analyst_prompts_contain_no_raw_cell_values(dataset):
    sup, calls, llm, _ = _team(dataset)
    sup.run(_path(dataset))
    values = _cell_values(dataset)
    assert len(values) > 10
    plan_text = json.dumps(scripted.plan_for(dataset))
    for skill, prompt in llm.prompts:
        if skill == "planner":                  # the planner sees the profile only
            leaked = [v for v in values if v in prompt]
        elif skill == "data_analyst":           # the analyst sees its task, so only values the plan itself names
            leaked = [v for v in values if v in prompt and v not in plan_text]
        else:
            continue
        assert not leaked, (skill, leaked[:5])
        assert DATA_DIR not in prompt


def test_insight_agent_sees_only_verified_values_and_no_code_or_paths():
    faults = {"analysis": {"sales_t3": {1: {"target_attainment_pct": ["scale", 1.01]}}}}
    sup, calls, llm, _ = _team("sales", faults)
    out = sup.run(_path("sales"))
    wrong = json.dumps(scripted.golden_answers("sales")["target_attainment_pct"] * 1.01)
    insight_prompts = [p for s, p in llm.prompts if s == "business_insight"]
    assert insight_prompts
    for p in insight_prompts:
        assert wrong not in p and "CLAIM " not in p and "print(" not in p and DATA_DIR not in p
        assert "refuted" not in p
    req = [m for m in out["messages"] if m["kind"] == "insight_request"]
    assert all(set(m["payload"]) == {"task_id", "title", "description", "attempt", "verified_claims", "feedback"}
               for m in req)


def test_analyst_feedback_names_the_failure_but_not_the_recomputed_value():
    faults = {"analysis": {"sales_t3": {1: {"target_attainment_pct": ["scale", 1.01]}}}}
    sup, calls, llm, _ = _team("sales", faults)
    sup.run(_path("sales"))
    retry = [p for s, p in llm.prompts if s == "data_analyst" and "TASK ID: sales_t3" in p and "ATTEMPT: 2" in p]
    assert len(retry) == 1
    assert "target_attainment_pct: refuted" in retry[0]
    assert "93.13" not in retry[0]


def test_verifier_requests_carry_no_code_or_stdout():
    sup, *_ = _team("student")
    out = sup.run(_path("student"))
    for m in out["messages"]:
        if m["kind"] == "verification_request":
            assert set(m["payload"]) == {"task_id", "attempt", "claims_spec", "submitted", "claim_errors",
                                         "unplanned", "dataset_path", "dataset_sha256"}


# ── human approval gate ──────────────────────────────────────────────────────

def test_interactive_run_pauses_before_any_analysis_and_resumes_with_approval():
    sup, calls, _, port = _team("student", approval=None)
    graph, cfg, paused = sup.start_interactive(_path("student"), "thread-a")
    intr = paused["__interrupt__"][0].value
    assert intr["kind"] == "plan_approval" and [t["id"] for t in intr["plan"]["tasks"]] == ["stu_t1", "stu_t2", "stu_t3"]
    assert port.calls == [] and {c["agent"] for c in calls} == {"planning_agent"}
    assert "final" not in paused or paused.get("final") is None
    done = sup.resume(graph, cfg, {"decision": "approve"})
    assert done["final"]["status"] == "completed" and len(done["final"]["accepted_claims"]) == 7


def test_interactive_reject_and_edit():
    sup, _, _, port = _team("sales", approval=None)
    g, cfg, _ = sup.start_interactive(_path("sales"), "thread-r")
    out = sup.resume(g, cfg, {"decision": "reject", "reviewer_note": "out of scope"})
    assert out["final"]["status"] == "rejected_by_human" and out["final"]["approval_note"] == "out of scope"
    assert port.calls == []
    sup, _, _, port = _team("sales", approval=None)
    g, cfg, _ = sup.start_interactive(_path("sales"), "thread-e")
    out = sup.resume(g, cfg, {"decision": "edit", "keep_task_ids": ["sales_t2"]})
    assert out["final"]["status"] == "completed" and out["final"]["approved_task_ids"] == ["sales_t2"]
    assert set(out["final"]["accepted_claims"]) == {"sales_by_region", "top_region"}
    assert port.calls == ["sales_t2_a1"]


@pytest.mark.parametrize("decision", [{"decision": "edit", "keep_task_ids": ["sales_t9"]}, {"decision": "yes"},
                                      "approve please", {"decision": "approve", "add_tasks": [1]}])
def test_invalid_human_decisions_fail_closed(decision):
    sup, _, _, port = _team("sales", approval=None)
    g, cfg, _ = sup.start_interactive(_path("sales"), "thread-x")
    out = sup.resume(g, cfg, decision)
    assert out["final"]["status"] == "approval_failed" and port.calls == []


def test_no_approver_fails_closed():
    sup, _, _, port = _team("sales", approval=None)
    out = sup.run(_path("sales"))
    assert out["final"]["status"] == "approval_failed" and port.calls == []


def test_an_approval_port_cannot_modify_the_plan_it_approves():
    def sneaky(req):
        req["plan"]["tasks"][0]["claims"] = []
        return {"decision": "approve"}
    sup, *_ = _team("student", approval=sneaky)
    out = sup.run(_path("student"))
    assert out["final"]["status"] == "completed"
    assert out["plan"]["tasks"][0]["claims"]


# ── termination and reflection ───────────────────────────────────────────────

@pytest.mark.parametrize("steps", [1, 2, 3, 7, 13])
def test_any_step_budget_terminates_with_budget_exhausted(steps):
    sup, *_ = _team("sales")
    out = sup.run(_path("sales"), limits={"max_steps": steps})
    assert out["final"]["status"] == "budget_exhausted" and out["final"]["steps"] == steps


def test_unreadable_dataset_terminates_with_profile_failed(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1\n", encoding="utf-8")
    sup, calls, *_ = _team("sales")
    out = sup.run(str(bad))
    assert out["final"]["status"] == "profile_failed" and calls == []


def test_retry_budget_follows_v1_semantics():
    faults = {"analysis": {"stu_t2": {a: {"top_subject_by_mean_score": ["replace", "Art"]} for a in range(1, 6)}}}
    for retries, attempts in ((0, 1), (1, 2), (2, 3), (4, 5)):
        sup, *_ = _team("student", faults)
        f = sup.run(_path("student"), limits={"max_retries": retries})["final"]
        assert f["tasks"]["stu_t2"]["attempts"] == attempts and f["tasks"]["stu_t2"]["status"] == "failed"


def test_reflection_lessons_are_recorded_and_fed_back_without_touching_repository_lessons():
    lessons_dir = os.path.join(ROOT, "lessons")
    before = sorted(os.listdir(lessons_dir))
    faults = {"analysis": {"att_t2": {1: {"mean_hours_worked": ["add", 3]}}}}
    sup, calls, llm, _ = _team("attendance", faults)
    out = sup.run(_path("attendance"))
    assert [ls for ls in out["lessons"] if ls["task_id"] == "att_t2"]
    retry = [p for s, p in llm.prompts if s == "data_analyst" and "TASK ID: att_t2" in p and "ATTEMPT: 2" in p][0]
    assert "attempt 1: claim mean_hours_worked: refuted" in retry
    assert sorted(os.listdir(lessons_dir)) == before


def test_runs_are_deterministic_apart_from_timings():
    def run():
        sup, *_ = _team("sales", {"analysis": {"sales_t2": {1: {"top_region": ["replace", "West"]}}}})
        out = sup.run(_path("sales"))
        return json.dumps({k: out[k] for k in ("messages", "events", "lessons", "final")}, sort_keys=True)
    assert run() == run()
