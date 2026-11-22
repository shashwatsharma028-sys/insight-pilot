"""P5.0: handoff envelopes, routes, payload validators and the claim vocabulary."""

import copy
import os

import pytest

from multi_agent import contracts as c
from multi_agent import dataset as dsmod
from multi_agent.claims import OPS, validate_claim
from tests.v2.golden.reference import DATA_DIR, DATASETS
from tests.v2.p5 import scripted


@pytest.fixture(scope="module")
def profiles():
    return {d: dsmod.profile(dsmod.load(os.path.join(DATA_DIR, f))) for d, f in DATASETS.items()}


# ── envelopes and routes ─────────────────────────────────────────────────────

def test_every_agent_talks_only_to_the_supervisor():
    for sender, recipient, _ in c.ROUTES:
        assert c.SUPERVISOR in (sender, recipient)
        assert sender != recipient


def test_every_request_kind_has_exactly_one_reply_kind_on_the_reverse_route():
    for req, rep in c.REPLY_KIND.items():
        fwd = [r for r in c.ROUTES if r[2] == req]
        assert fwd and all(r[0] == c.SUPERVISOR for r in fwd)
        for _, agent, _ in fwd:
            assert (agent, c.SUPERVISOR, rep) in c.ROUTES


@pytest.mark.parametrize("route", [
    (c.ANALYST, c.VERIFIER, "analysis_result"),          # agents may not hand off to each other
    (c.INSIGHT, c.VERIFIER, "insight_report"),
    (c.SUPERVISOR, c.VERIFIER, "analysis_request"),       # wrong kind for the recipient
    (c.PLANNING, c.SUPERVISOR, "verification_report"),
    (c.SUPERVISOR, "shell", "plan_request"),
])
def test_disallowed_routes_are_rejected(route):
    with pytest.raises(c.HandoffRejected, match="route not allowed"):
        c.envelope(1, route[0], route[1], route[2], {})


def test_envelope_structure_checks():
    env = c.envelope(3, c.SUPERVISOR, c.PLANNING, "plan_request", {"x": 1})
    c.check_envelope(env, recipient=c.PLANNING, kinds={"plan_request"})
    for mutate, msg in [
        (lambda e: e.update(extra=1), "unknown fields"),
        (lambda e: e.pop("reply_to"), "missing"),
        (lambda e: e.update(seq=-1), "seq"),
        (lambda e: e.update(seq=True), "seq"),
        (lambda e: e.update(attempt="1"), "attempt"),
        (lambda e: e.update(payload=[1]), "payload"),
    ]:
        bad = copy.deepcopy(env)
        mutate(bad)
        with pytest.raises(c.HandoffRejected, match=msg):
            c.check_envelope(bad)
    with pytest.raises(c.HandoffRejected, match="addressed to"):
        c.check_envelope(env, recipient=c.ANALYST)
    with pytest.raises(c.HandoffRejected, match="unexpected kind"):
        c.check_envelope(env, kinds={"analysis_request"})
    reply = c.envelope(4, c.PLANNING, c.SUPERVISOR, "plan", {}, reply_to=3)
    c.check_envelope(reply, reply_to=3)
    with pytest.raises(c.HandoffRejected, match="reply_to"):
        c.check_envelope(reply, reply_to=2)
    with pytest.raises(c.HandoffRejected):
        c.check_envelope("not an envelope")


# ── plans ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_scripted_plans_are_valid(dataset, profiles):
    assert c.validate_plan(scripted.plan_for(dataset), profiles[dataset]) == []


def test_scripted_plans_cover_all_28_golden_answers():
    for d in DATASETS:
        keys = {cl["key"] for t in scripted.plan_for(d)["tasks"] for cl in t["claims"]}
        assert set(scripted.golden_answers(d)) <= keys


def _plan(dataset="sales"):
    return scripted.plan_for(dataset)


@pytest.mark.parametrize("mutate,msg", [
    (lambda p: p.update(tasks=[]), "1 to 8 tasks"),
    (lambda p: p.update(tasks=p["tasks"] * 3), "1 to 8 tasks"),
    (lambda p: p.update(extra=1), "unknown fields"),
    (lambda p: p["tasks"][1].update(id=p["tasks"][0]["id"]), "duplicate id"),
    (lambda p: p["tasks"][0].update(id="Bad Id!"), "invalid id"),
    (lambda p: p["tasks"][0].update(title=" "), "title"),
    (lambda p: p["tasks"][0].update(analysis_type="magic"), "analysis_type"),
    (lambda p: p["tasks"][0].update(claims=[]), "needs 1 to"),
    (lambda p: p["tasks"][0].pop("claims"), "missing"),
    (lambda p: p["tasks"][1]["claims"].append(dict(p["tasks"][0]["claims"][0])), "duplicate claim key"),
    (lambda p: p["tasks"][0]["claims"][0].update(op="median_of_everything"), "unknown op"),
    (lambda p: p["tasks"][0]["claims"][1].update(args={"column": "no_such_column"}), "unknown column"),
    (lambda p: p["tasks"][0]["claims"][1].update(args={"column": "region"}), "is text"),
    (lambda p: p["tasks"][0]["claims"][1].update(args={}), "requires argument"),
    (lambda p: p["tasks"][0]["claims"][1]["args"].update(rows="all"), "unexpected argument"),
    (lambda p: p["tasks"][0]["claims"][1].update(key="Total Sales"), "must match"),
    (lambda p: p["tasks"][0]["claims"][1].update(note="x"), "unknown fields"),
])
def test_invalid_plans_are_rejected_with_reasons(mutate, msg, profiles):
    plan = _plan()
    mutate(plan)
    problems = c.validate_plan(plan, profiles["sales"])
    assert problems and any(msg in p for p in problems), problems


def test_claim_vocabulary_argument_rules(profiles):
    p = profiles["ecommerce"]
    ok = {"key": "m", "op": "argmax_group_sum", "args": {"by": "date", "column": "revenue", "by_prefix": 7}}
    assert validate_claim(ok, p) == []
    for prefix in (0, 65, 7.0, True, "7"):
        bad = copy.deepcopy(ok)
        bad["args"]["by_prefix"] = prefix
        assert validate_claim(bad, p), prefix
    assert validate_claim({"key": "x", "op": "count_where_equals",
                           "args": {"column": "is_weekend", "value": True}}, p)      # value is raw CSV text
    assert validate_claim({"key": "x", "op": "pearson", "args": {"x": "revenue", "y": "date"}}, p)
    assert validate_claim("not a claim", p) == ["claim must be an object"]
    assert set(OPS) >= {"count_rows", "sum", "mean", "pearson", "value_counts"}


# ── approvals ────────────────────────────────────────────────────────────────

def test_approval_decisions():
    plan = _plan()
    assert c.validate_approval({"decision": "approve"}, plan) == []
    assert c.validate_approval({"decision": "reject", "reviewer_note": ""}, plan) == []
    assert c.validate_approval({"decision": "edit", "keep_task_ids": ["sales_t1"]}, plan) == []
    for bad in ({"decision": "approve_all_and_add"}, {"decision": "edit"}, {"decision": "edit", "keep_task_ids": []},
                {"decision": "edit", "keep_task_ids": ["sales_t9"]},            # an edit cannot add tasks
                {"decision": "edit", "keep_task_ids": ["sales_t1", "sales_t1"]},
                {"decision": "approve", "keep_task_ids": ["sales_t1"]},
                {"decision": "approve", "plan": {"tasks": []}}, {}):
        assert c.validate_approval(bad, plan), bad


# ── analysis results and reports ─────────────────────────────────────────────

def _task():
    return _plan()["tasks"][0]


def _result(**over):
    base = {"task_id": "sales_t1", "attempt": 1, "code": "x", "claims": {"row_count": 293}, "claim_errors": {},
            "unplanned": [], "execution": {"ok": True, "stdout": "", "error": "", "error_type": "", "chart_path": "",
                                           "backend": "b", "isolation": "i", "seconds": 0.0,
                                           "blocked_by_safety_scan": False}}
    base.update(over)
    return base


def test_analysis_result_validation():
    t = _task()
    assert c.validate_analysis_result(_result(), t) == []
    assert c.validate_analysis_result(_result(task_id="sales_t2"), t)
    assert c.validate_analysis_result(_result(claims={"other_task_key": 1}), t)
    assert c.validate_analysis_result(_result(claims={"row_count": object()}), t)
    assert c.validate_analysis_result(_result(claim_errors={"row_count": 1}), t)
    assert c.validate_analysis_result(_result(unplanned="x"), t)
    assert c.validate_analysis_result(_result(execution={"ok": "yes"}), t)
    big = _result()
    big["execution"]["stdout"] = "x" * (c.MAX_STDOUT_CHARS + 1)
    assert c.validate_analysis_result(big, t)


def test_verification_report_must_be_internally_consistent():
    t = _task()
    keys = [cl["key"] for cl in t["claims"]]
    report = {"task_id": t["id"], "attempt": 1, "verdict": "accepted", "unplanned": [], "dataset_integrity": True,
              "feedback": [], "claims": {k: {"status": "verified"} for k in keys}}
    assert c.validate_verification_report(report, t) == []
    lying = copy.deepcopy(report)
    lying["claims"][keys[0]]["status"] = "refuted"
    assert any("inconsistent" in p for p in c.validate_verification_report(lying, t))
    partial = copy.deepcopy(report)
    partial["claims"].pop(keys[0])
    assert c.validate_verification_report(partial, t)
    odd = copy.deepcopy(report)
    odd["claims"][keys[0]]["status"] = "probably_fine"
    assert c.validate_verification_report(odd, t)


def test_insight_report_and_check_validation():
    t = _task()
    good = {"task_id": t["id"], "insights": [{"text": "Rows: 293.", "cites": ["row_count"]}]}
    assert c.validate_insight_report(good, t) == []
    assert c.validate_insight_report({"task_id": t["id"], "insights": [{"text": "x" * 601, "cites": []}]}, t)
    assert c.validate_insight_report({"task_id": t["id"], "insights": [{"text": "a", "cites": "row_count"}]}, t)
    assert c.validate_insight_report({"task_id": t["id"], "insights": [good["insights"][0]] * 9}, t)
    assert c.validate_insight_check_report({"task_id": t["id"], "verdict": "accepted", "problems": []}, t) == []
    assert c.validate_insight_check_report({"task_id": t["id"], "verdict": "accepted", "problems": ["x"]}, t)
    assert c.validate_agent_error({"error_type": "X", "message": "m"}) == []
    assert c.validate_agent_error({"error_type": "", "message": "m"})
