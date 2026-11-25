"""
UI-1: the V2 application service (v2/app_service.py), offline only. Every provider is an injected mock
(live_llm.FakeProvider or a local stand-in); no network, no key, no container, no V1 executor.
Datasets and goals are synthetic and new (not the benchmark's).
"""

import json
import os
import stat
import subprocess
import sys
import threading

import pytest

from multi_agent import live_llm, op_engine
from v2 import app_service as appsvc
from v2.app_service import AnalysisService, InvalidInput, Limits, Phase, ServiceConfig, TransitionRefused, to_dict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

FLEET = ("vehicle,depot,km,cost,serviced\n"
         + "\n".join(f"V{i},{'NS'[i % 2]},{100 + i * 7},{20 + i % 5 * 3}.5,{'True' if i % 3 else 'False'}"
                     for i in range(30)) + "\n").encode()
CLINIC = ("visit_date,clinic,wait_minutes,follow_up\n"
          + "\n".join(f"2024-0{1 + i % 6}-1{i % 9},{'ABC'[i % 3]},{5 + i % 11},{'True' if i % 4 == 0 else 'False'}"
                      for i in range(40)) + "\n").encode()
GOAL = "Which depot spends the most on maintenance, and how often are vehicles serviced?"

PLAN = {"tasks": [
    {"id": "t1", "title": "Cost by depot", "description": "Total maintenance cost per depot",
     "analysis_type": "comparative",
     "claims": [{"key": "cost_by_depot", "op": "group_sum", "args": {"by": "depot", "column": "cost"}},
                {"key": "top_depot", "op": "argmax_group_sum", "args": {"by": "depot", "column": "cost"}}]},
    {"id": "t2", "title": "Service rate", "description": "Share of vehicles serviced", "analysis_type": "descriptive",
     "claims": [{"key": "serviced_rate", "op": "rate_where_equals", "args": {"column": "serviced", "value": "True"}}]}]}


class Mock:
    """Injected offline provider: planner replies in order (the last one repeats); insights cite one claim."""

    def __init__(self, plans=None, insight=None, fail=None, gate=None):
        self.plans = list(plans or [PLAN])
        self.insight, self.fail, self.gate = insight, fail or {}, gate
        self.calls = []

    def __call__(self, skill, system, user):
        self.calls.append((skill, user))
        if skill in self.fail:
            raise self.fail[skill]
        if self.gate and skill == "business_insight":
            self.gate.wait(10)
        if skill == "planner":
            p = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return p if isinstance(p, str) else json.dumps(p)
        keys = list(json.loads(user.split("VERIFIED CLAIMS JSON: ", 1)[1].split("\n", 1)[0]))
        return json.dumps(self.insight or {"insights": [{"text": "The verified figures summarise this task.",
                                                         "cites": keys[:1]}]})

    def skills(self):
        return [s for s, _ in self.calls]


def _svc(tmp_path, mock=None, config=None):
    mock = mock or Mock()
    return AnalysisService(live_llm.FakeProvider(mock), config=config, base_dir=str(tmp_path)), mock


def _ready(tmp_path, mock=None, config=None, data=FLEET):
    svc, mock = _svc(tmp_path, mock, config)
    svc.upload("fleet.csv", data)
    return svc, mock


def _views_json(svc):
    out = [to_dict(svc.session_usage())]
    for v in (svc.dataset, svc.approval, svc.result):
        if v is not None:
            out.append(to_dict(v))
    return json.dumps(out)


# ── upload and goal validation ──────────────────────────────────────────────────────────────────────────

def test_upload_stores_a_private_file_under_a_random_name(tmp_path):
    svc, _ = _svc(tmp_path)
    view = svc.upload("../../etc/fleet.csv", FLEET)
    assert view.display_name == "fleet.csv" and view.rows == 30 and [c.name for c in view.columns][:2] == ["vehicle", "depot"]
    path = svc._dataset_path
    assert os.path.dirname(path) == svc._data_dir and os.path.basename(path).startswith("dataset-")
    assert "fleet" not in os.path.basename(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and stat.S_IMODE(os.stat(svc._dir).st_mode) == 0o700
    assert svc._dir not in _views_json(svc)


@pytest.mark.parametrize("name,data,code", [
    ("fleet.txt", FLEET, "not_csv"), ("fleet.csv", b"", "empty"),
    ("fleet.csv", b"\xff\xfe\x00bad", "not_utf8"), ("fleet.csv", b"a,b\n1,2\n3\n", "invalid_csv"),
    ("fleet.csv", b"a,b\n", "no_rows"), ("fleet.csv", b"a,a\n1,2\n", "invalid_csv"),
    ("fleet.csv", ("x" * 65 + ",b\n1,2\n").encode(), "bad_column_name"),
])
def test_invalid_uploads_are_rejected_and_nothing_is_kept(tmp_path, name, data, code):
    svc, _ = _svc(tmp_path)
    with pytest.raises(InvalidInput) as e:
        svc.upload(name, data)
    assert e.value.code == code and os.listdir(svc._data_dir) == [] and svc.dataset is None


def test_size_row_and_column_limits(tmp_path):
    svc, _ = _svc(tmp_path, config=ServiceConfig(limits=Limits(max_upload_bytes=100, max_rows=5, max_columns=3)))
    with pytest.raises(InvalidInput) as e:
        svc.upload("big.csv", FLEET)                                              # > 100 bytes: refused pre-write
    assert e.value.code == "too_large" and os.listdir(svc._data_dir) == []
    with pytest.raises(InvalidInput) as e:
        svc.upload("rows.csv", b"a\n" + b"1\n" * 6)
    assert e.value.code == "too_many_rows"
    with pytest.raises(InvalidInput) as e:
        svc.upload("cols.csv", b"a,b,c,d\n1,2,3,4\n")
    assert e.value.code == "too_many_columns" and os.listdir(svc._data_dir) == []


def test_a_new_upload_replaces_the_previous_file(tmp_path):
    svc, _ = _svc(tmp_path)
    svc.upload("fleet.csv", FLEET)
    first = svc._dataset_path
    svc.upload("clinic.csv", CLINIC)
    assert not os.path.exists(first) and len(os.listdir(svc._data_dir)) == 1 and svc.dataset.rows == 40


@pytest.mark.parametrize("goal", ["", "  ", "ab", "x" * 1001, "show\x00all", 42, None])
def test_invalid_goals_are_refused_before_any_call(tmp_path, goal):
    svc, mock = _ready(tmp_path)
    with pytest.raises(InvalidInput):
        svc.start(goal)
    assert mock.calls == [] and svc.phase == Phase.IDLE and svc.session_usage().runs_started == 0


def test_start_requires_a_dataset(tmp_path):
    svc, mock = _svc(tmp_path)
    with pytest.raises(TransitionRefused) as e:
        svc.start(GOAL)
    assert e.value.code == "no_dataset" and mock.calls == []


# ── the real Supervisor pauses for approval with no analysis done ──────────────────────────────────────

def test_start_pauses_for_approval_with_only_the_planner_called(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(op_engine.TrustedOpsAnalyst, "run", lambda self, req: ran.append(req) or 1 / 0)
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    assert svc.phase == Phase.AWAITING_APPROVAL and svc.approval == a
    assert mock.skills() == ["planner"] and ran == []                           # nothing analysed yet
    assert f"USER GOAL: {GOAL}" in mock.calls[0][1]                              # the user's real goal
    assert [t.task_id for t in a.tasks] == ["t1", "t2"] and a.tasks[0].claims[0].op == "group_sum"
    import hashlib
    assert a.plan_id == hashlib.sha256(json.dumps(svc._pending["plan"], sort_keys=True).encode()).hexdigest()
    assert svc._pending["plan"]["tasks"][0]["title"] == "Cost by depot"         # the exact plan shown


def test_approve_runs_the_plan_with_verification_and_does_not_replan(tmp_path):
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert svc.phase == Phase.COMPLETED and r.complete and r.error_code is None
    vals = {c.key: c.value for c in r.verified_claims}
    assert vals == {"cost_by_depot": {"N": 397.5, "S": 397.5}, "top_depot": "N", "serviced_rate": 2 / 3}
    assert r.verified_claims[0].spec.op in ("group_sum", "argmax_group_sum", "rate_where_equals")
    assert r.review_only_claims == () and len(r.insights) == 2 and all(i.cites for i in r.insights)
    assert mock.skills().count("planner") == 1                                  # resume did not re-plan
    assert [t.status for t in r.tasks] == ["completed", "completed"]
    assert "does not prove" in r.verification_scope
    assert r.usage.provider_calls == 3 and r.usage.calls_by_skill == {"planner": 1, "business_insight": 2}
    assert r.usage.input_tokens > 0 and "ESTIMATE" in r.usage.cost_label
    assert set(r.usage.seconds_by_agent) == {"planning_agent", "business_insight_agent"}


def test_reject_analyses_nothing(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(op_engine.TrustedOpsAnalyst, "run", lambda self, req: ran.append(1))
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "reject", note="not needed")
    assert svc.phase == Phase.REJECTED and r.phase == "rejected" and not r.complete
    assert ran == [] and mock.skills() == ["planner"] and r.verified_claims == r.review_only_claims == ()


def test_keep_a_subset_runs_only_those_tasks(tmp_path):
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "edit", keep_task_ids=["t2"])
    assert svc.phase == Phase.COMPLETED and {c.key for c in r.verified_claims} == {"serviced_rate"}
    assert [t.task_id for t in r.tasks] == ["t2"] and mock.skills().count("business_insight") == 1


@pytest.mark.parametrize("decision,keep", [("ok", None), ("edit", []), ("edit", ["t9"]), ("edit", ["t1", "t1"]),
                                           ("edit", "t1"), ("approve", ["t1"])])
def test_invalid_decisions_are_refused_with_no_effect(tmp_path, decision, keep):
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    with pytest.raises(TransitionRefused) as e:
        svc.decide(a.run_id, a.plan_id, decision, keep_task_ids=keep)
    assert e.value.code == "bad_decision" and svc.phase == Phase.AWAITING_APPROVAL and mock.skills() == ["planner"]
    assert svc.decide(a.run_id, a.plan_id, "approve").phase == "completed"      # a valid decision still works


def test_stale_and_duplicate_decisions_are_refused(tmp_path):
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    with pytest.raises(TransitionRefused) as e:
        svc.decide(a.run_id, "0" * 64, "approve")                               # a different plan
    assert e.value.code == "stale" and svc.phase == Phase.AWAITING_APPROVAL
    svc.decide(a.run_id, a.plan_id, "approve")
    with pytest.raises(TransitionRefused) as e:
        svc.decide(a.run_id, a.plan_id, "approve")                              # duplicate
    assert e.value.code == "not_awaiting"
    b = svc.start("How do vehicle costs compare between depots?")
    with pytest.raises(TransitionRefused) as e:
        svc.decide(a.run_id, a.plan_id, "approve")                              # stale run
    assert e.value.code == "stale" and svc.approval == b
    assert mock.skills().count("business_insight") == 2                         # nothing ran twice


def test_concurrent_decisions_and_reset_during_a_run_are_refused(tmp_path):
    gate = threading.Event()
    svc, mock = _ready(tmp_path, Mock(gate=gate))
    a = svc.start(GOAL)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", svc.decide(a.run_id, a.plan_id, "approve")))
    t.start()
    for _ in range(200):
        if svc.phase == Phase.RUNNING and mock.skills().count("business_insight"):
            break
        threading.Event().wait(0.01)
    assert svc.phase == Phase.RUNNING
    for action in (lambda: svc.decide(a.run_id, a.plan_id, "approve"), svc.reset, svc.close,
                   lambda: svc.start(GOAL), lambda: svc.upload("x.csv", FLEET)):
        with pytest.raises(TransitionRefused):
            action()
    gate.set()
    t.join(10)
    assert out["r"].phase == "completed" and mock.skills().count("planner") == 1


def test_concurrent_starts_are_refused_while_planning(tmp_path):
    gate = threading.Event()

    class SlowPlanner(Mock):
        def __call__(self, skill, system, user):
            if skill == "planner":
                gate.wait(10)
            return super().__call__(skill, system, user)
    svc, mock = _ready(tmp_path, SlowPlanner())
    t = threading.Thread(target=lambda: svc.start(GOAL))
    t.start()
    for _ in range(200):
        if svc.phase == Phase.PLANNING:
            break
        threading.Event().wait(0.01)
    assert svc.phase == Phase.PLANNING
    for action in (lambda: svc.start(GOAL), svc.reset):
        with pytest.raises(TransitionRefused):
            action()
    gate.set()
    t.join(10)
    assert svc.phase == Phase.AWAITING_APPROVAL and mock.skills().count("planner") == 1


# ── partial, failed and limited runs ────────────────────────────────────────────────────────────────────

def _bad_value_plan():
    p = json.loads(json.dumps(PLAN))
    p["tasks"][1]["claims"][0]["args"]["value"] = True                          # structurally invalid
    return p


def test_an_invalid_plan_fails_safely_before_approval(tmp_path):
    svc, mock = _ready(tmp_path, Mock(plans=[_bad_value_plan()]))
    r = svc.start(GOAL)
    assert svc.phase == Phase.FAILED and r.error_code == "plan_invalid" and not r.complete
    assert mock.skills() == ["planner", "planner"] and r.verified_claims == ()   # bounded planner retries


def test_selective_recovery_reports_abandoned_tasks_when_enabled(tmp_path):
    svc, mock = _ready(tmp_path, Mock(plans=[_bad_value_plan()]), ServiceConfig(selective_recovery=True))
    a = svc.start(GOAL)
    assert [x.task_id for x in a.abandoned_tasks] == ["t2"] and [t.task_id for t in a.tasks] == ["t1"]
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert svc.phase == Phase.COMPLETED_WITH_FAILURES and r.complete
    assert [x.task_id for x in r.abandoned_tasks] == ["t2"] and "serviced_rate" in r.abandoned_tasks[0].claim_keys
    assert {c.key for c in r.verified_claims} == {"cost_by_depot", "top_depot"}


def test_an_incomplete_run_exposes_review_only_results_never_accepted_ones(tmp_path):
    svc, _ = _ready(tmp_path, config=ServiceConfig(max_steps=6))               # stops after the first task
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert svc.phase == Phase.FAILED and r.error_code == "budget_exhausted" and not r.complete
    assert r.verified_claims == () and r.insights == ()
    assert {c.key for c in r.review_only_claims} == {"cost_by_depot", "top_depot"}


def test_provider_failure_is_reported_safely(tmp_path):
    err = live_llm.ProviderError("HTTP 503: Bearer sk-SECRETTOKEN123456 at /home/user/private/data.csv")
    svc, mock = _ready(tmp_path, Mock(fail={"planner": err}))
    r = svc.start(GOAL)
    assert svc.phase == Phase.FAILED and r.error_code == "provider_unavailable"
    text = _views_json(svc)
    assert "SECRETTOKEN" not in text and "/home/user/private" not in text and "Traceback" not in text


def test_session_budget_exhaustion_persists_across_resets(tmp_path):
    svc, mock = _ready(tmp_path, config=ServiceConfig(limits=Limits(session_budget_usd=1e-9)))
    r = svc.start(GOAL)
    assert svc.phase == Phase.FAILED and r.error_code == "usage_limit" and mock.calls == []
    svc.reset()
    with pytest.raises(TransitionRefused) as e:
        svc.start(GOAL)
    assert e.value.code == "usage_limit" and svc.session_usage().limit_reached


def test_cumulative_call_limit_is_not_reset_by_a_new_analysis(tmp_path):
    svc, mock = _ready(tmp_path, config=ServiceConfig(limits=Limits(max_session_calls=4)))
    a = svc.start(GOAL)
    svc.decide(a.run_id, a.plan_id, "approve")                                  # 3 calls
    svc.reset()
    assert svc.session_usage().provider_calls == 3 and not svc.session_usage().limit_reached
    b = svc.start("How often are vehicles serviced?")                           # 4th call: the planner
    r = svc.decide(b.run_id, b.plan_id, "approve")                              # insight calls are refused
    assert svc.session_usage().provider_calls == 4 and svc.session_usage().limit_reached
    assert r.error_code == "usage_limit"
    with pytest.raises(TransitionRefused):
        svc.start(GOAL)


def test_the_run_count_limit_is_cumulative(tmp_path):
    svc, _ = _ready(tmp_path, config=ServiceConfig(limits=Limits(max_runs_per_session=1)))
    a = svc.start(GOAL)
    svc.decide(a.run_id, a.plan_id, "reject")
    svc.reset()
    with pytest.raises(TransitionRefused) as e:
        svc.start(GOAL)
    assert e.value.code == "usage_limit"


def test_an_unexpected_exception_fails_closed_and_requires_a_new_run(tmp_path, monkeypatch):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    from multi_agent.supervisor import Supervisor

    def boom(*a, **k):
        raise RuntimeError("graph broke at /srv/secret/file.py")
    monkeypatch.setattr(Supervisor, "resume", staticmethod(boom))
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert svc.phase == Phase.FAILED and r.error_code == "internal_error" and "/srv/secret" not in _views_json(svc)
    with pytest.raises(TransitionRefused):
        svc.decide(a.run_id, a.plan_id, "approve")                              # no continuation
    monkeypatch.undo()
    b = svc.start(GOAL)                                                          # an explicit new run works
    assert svc.decide(b.run_id, b.plan_id, "approve").phase == "completed"


# ── session isolation and cleanup ───────────────────────────────────────────────────────────────────────

def test_two_sessions_with_the_same_filename_are_isolated(tmp_path):
    s1, m1 = _ready(tmp_path)
    s2, m2 = _svc(tmp_path, Mock(plans=[{"tasks": [{"id": "c1", "title": "Wait", "description": "Mean wait",
                                                    "analysis_type": "descriptive",
                                                    "claims": [{"key": "mean_wait", "op": "mean",
                                                                "args": {"column": "wait_minutes"}}]}]}]))
    s2.upload("fleet.csv", CLINIC)                                               # same name, other content
    assert s1._dir != s2._dir and s1._dataset_path != s2._dataset_path
    a1, a2 = s1.start(GOAL), s2.start("What is the average waiting time?")
    assert s1._cfg["configurable"]["thread_id"] != s2._cfg["configurable"]["thread_id"]
    r2 = s2.decide(a2.run_id, a2.plan_id, "approve")
    r1 = s1.decide(a1.run_id, a1.plan_id, "approve")
    assert {c.key for c in r1.verified_claims} == {"cost_by_depot", "top_depot", "serviced_rate"}
    assert {c.key for c in r2.verified_claims} == {"mean_wait"}
    assert s1.session_usage().provider_calls == 3 and s2.session_usage().provider_calls == 2
    assert s1._port.ledger.path != s2._port.ledger.path


def test_each_analysis_gets_a_new_thread_and_checkpoint(tmp_path):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    t1, g1 = svc._cfg["configurable"]["thread_id"], svc._graph
    svc.reset()
    svc.start(GOAL)
    assert svc._cfg["configurable"]["thread_id"] != t1 and svc._graph is not g1


def test_reset_and_close_clean_up(tmp_path):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    svc.reset()
    assert svc.phase == Phase.IDLE and svc.approval is None and svc._graph is None and svc.dataset is not None
    svc.reset(clear_dataset=True)
    assert svc.dataset is None and os.listdir(svc._data_dir) == []
    d = svc._dir
    svc.close()
    assert not os.path.exists(d)
    with pytest.raises(TransitionRefused):
        svc.upload("fleet.csv", FLEET)


# ── optimisations: off by default, independently switchable ────────────────────────────────────────────

def test_every_optimisation_is_off_by_default(tmp_path):
    svc, mock = _ready(tmp_path)
    assert svc.config.supervisor_limits() == {"max_steps": 200} and svc._port.transient_retries == 0
    svc.start(GOAL)
    prompt = mock.calls[0][1]
    assert "CLAIM ARGUMENT SCHEMA" not in prompt and "CLAIM OP RESULTS" not in prompt


@pytest.mark.parametrize("flag,marker", [("schema_guidance", "CLAIM ARGUMENT SCHEMA"),
                                         ("semantic_guidance", "CLAIM OP RESULTS")])
def test_prompt_optimisations_switch_independently(tmp_path, flag, marker):
    svc, mock = _ready(tmp_path, config=ServiceConfig(**{flag: True}))
    svc.start(GOAL)
    other = {"CLAIM ARGUMENT SCHEMA", "CLAIM OP RESULTS"} - {marker}
    assert marker in mock.calls[0][1] and not any(m in mock.calls[0][1] for m in other)


def test_provider_retries_are_configurable(tmp_path):
    svc, mock = _ready(tmp_path, Mock(fail={}), ServiceConfig(provider_retries=2))
    assert svc._port.transient_retries == 2


# ── runtime purity: no tests, harnesses, V1 or executors ────────────────────────────────────────────────

def test_the_service_imports_no_tests_harness_v1_or_executor_modules():
    code = ("import sys; sys.path.insert(0, %r); import v2.app_service; "
            "print([m for m in sys.modules if m.split('.')[0] in "
            "('tests', 'evaluation', 'agent', 'sandbox', 'sandbox_v2', 'ui', 'utils')])" % ROOT)
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).stdout.strip()
    assert out == "[]"
    import ast
    tree = ast.parse(open(os.path.join(ROOT, "v2", "app_service.py")).read())
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
            {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {n for n in names if n and n.split(".")[0] in ("tests", "evaluation", "agent", "sandbox", "ui")}
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    code_words = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
                 {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
                 {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and id(n) not in docstrings}
    for forbidden in ("approve_all", "golden", "tasks_for", "multi_goal", "CLAIM_SPECS", "GOOGLE_API_KEY", "getenv",
                      "environ", "oracle_answer", "scripted"):
        assert not any(forbidden in w for w in code_words), forbidden


def test_a_full_run_never_reaches_a_v1_executor(tmp_path, monkeypatch):
    import sandbox.executor as v1

    def trip(*a, **k):
        raise AssertionError("V1 executor reached")
    for name in dir(v1):
        if callable(getattr(v1, name)) and not name.startswith("_"):
            monkeypatch.setattr(v1, name, trip, raising=False)
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    assert svc.decide(a.run_id, a.plan_id, "approve").phase == "completed"


# SEC (isolated branch feature/v2-ai-security): the ONLY files under these protected paths that may differ from
# ff618163 are the reviewed security-control files below. V1 folders, config, requirements.txt, sandbox_v2,
# v2/launch_app.py and v2/execution must still be byte-identical.
SEC_RUNTIME_FILES = {
    "multi_agent/security/__init__.py", "multi_agent/security/audit.py", "multi_agent/security/detect.py",
    "multi_agent/security/outbound.py", "multi_agent/security/policy.py", "multi_agent/supervisor.py",
    "multi_agent/team.py", "multi_agent/llm.py", "multi_agent/op_engine.py", "multi_agent/prompts.py",
    "multi_agent/live_llm.py", "multi_agent/execution.py",
}


def test_v1_and_protected_modules_are_unchanged_from_ff618163():
    paths = ["ui", "agent", "sandbox", "sandbox_v2", "utils", "config", "requirements.txt", "multi_agent",
             "v2/launch_app.py", "v2/execution"]
    out = subprocess.run(["git", "diff", "--name-only", "ff618163", "--"] + paths, capture_output=True, text=True,
                         cwd=ROOT, check=True).stdout
    changed = set(out.split())
    assert changed <= SEC_RUNTIME_FILES, sorted(changed - SEC_RUNTIME_FILES)
    assert not {f for f in changed if not f.startswith("multi_agent/")}


def test_views_never_contain_paths_tracebacks_or_raw_state(tmp_path):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    svc.decide(a.run_id, a.plan_id, "approve")
    text = _views_json(svc)
    assert svc._dir not in text and "Traceback" not in text and "__interrupt__" not in text
    assert "messages" not in to_dict(svc.result) and "lessons" not in to_dict(svc.result)
