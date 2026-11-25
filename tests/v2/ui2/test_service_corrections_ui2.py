"""
UI-2: regression tests for the UI-1 review corrections in v2/app_service.py (offline; injected mock providers).
"""

import gc
import json
import os
import time

import pytest

from multi_agent import live_llm
from multi_agent.supervisor import Supervisor
from tests.v2.ui1.test_app_service_ui1 import FLEET, GOAL, PLAN, Mock, _ready, _svc, _views_json
from v2 import app_service as appsvc
from v2.app_service import AnalysisService, InvalidInput, Phase, ServiceConfig, TransitionRefused, to_dict


# ── 1. the approval view is the exact plan: execution-critical arguments are never clipped or redacted ─────

def test_approval_shows_exact_claim_arguments_even_when_they_look_sensitive(tmp_path):
    value = "/srv/data/" + "x" * 180                                             # path-like, 190 chars: legal data
    csv = ("item,tag\n" + "\n".join(f"i{i},{value if i % 2 else 'plain'}" for i in range(10)) + "\n").encode()
    plan = {"tasks": [{"id": "t1", "title": "Tagged items", "description": "Count tagged items",
                       "analysis_type": "descriptive",
                       "claims": [{"key": "tagged", "op": "count_where_equals",
                                   "args": {"column": "tag", "value": value}}]}]}
    svc, _ = _svc(tmp_path, Mock(plans=[plan]))
    svc.upload("items.csv", csv)
    a = svc.start("How many items carry the long tag?")
    spec = a.tasks[0].claims[0]
    assert (spec.key, spec.op, spec.args) == ("tagged", "count_where_equals", {"column": "tag", "value": value})
    assert to_dict(a)["tasks"][0]["claims"][0]["args"]["value"] == value                       # exact, unredacted
    rebuilt = {"tasks": [{"id": t.task_id, "title": t.title, "description": t.description,
                          "analysis_type": t.analysis_type,
                          "claims": [{"key": c.key, "op": c.op, "args": c.args} for c in t.claims]} for t in a.tasks]}
    assert rebuilt == plan                                                        # the view IS the approved plan
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert r.verified_claims[0].value == 5 and r.verified_claims[0].spec.args["value"] == value


# ── 2. structured results keep their type; truncation is explicit ───────────────────────────────────────

def test_large_mappings_stay_mappings_and_are_marked_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(appsvc, "MAX_RESULT_ENTRIES", 1)
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    by_depot = next(c for c in r.verified_claims if c.key == "cost_by_depot")
    assert by_depot.result.kind == "mapping" and isinstance(by_depot.value, dict)
    assert by_depot.result.truncated and by_depot.result.total_entries == 2 and by_depot.result.shown_entries == 1
    rate = next(c for c in r.verified_claims if c.key == "serviced_rate")
    assert rate.result.kind == "number" and not rate.result.truncated and rate.value == 2 / 3


@pytest.mark.parametrize("v,kind,trunc", [(None, "null", False), (True, "boolean", False), (3, "integer", False),
                                          (2.5, "number", False), (float("nan"), "other", False),
                                          ("abc", "text", False), ("x" * 10_001, "text", True),
                                          ({"a": 1}, "mapping", False), ([1, 2], "other", False)])
def test_value_view_types(v, kind, trunc):
    vv = appsvc.value_view(v)
    assert vv.kind == kind and vv.truncated is trunc
    if kind == "mapping":
        assert isinstance(vv.value, dict) and vv.total_entries == 1


# ── 3. analytical data vs diagnostics: what is exact, what is sanitised ─────────────────────────────────

def test_diagnostics_are_sanitised_but_analytical_values_are_exact(tmp_path):
    insight = {"insights": [{"text": "Token Bearer sk-ABCDEFGH12345678 leaked", "cites": ["cost_by_depot"]}]}
    svc, _ = _ready(tmp_path, Mock(insight=insight))
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    text = _views_json(svc)
    assert "sk-ABCDEFGH12345678" not in text and "[redacted]" in text          # model prose: secrets redacted
    assert {c.key: c.value for c in r.verified_claims}["top_depot"] == "N"     # analytical value: exact


def test_failure_messages_never_leak_paths_or_secrets(tmp_path):
    err = live_llm.ProviderError("api_key=SUPERSECRET99 failed reading /etc/ip/secret.csv")
    svc, _ = _ready(tmp_path, Mock(fail={"business_insight": err}))
    a = svc.start(GOAL)
    svc.decide(a.run_id, a.plan_id, "approve")
    text = _views_json(svc)
    assert "SUPERSECRET99" not in text and "/etc/ip" not in text


# ── 4. mock vs provider-reported usage vs conservative charges ──────────────────────────────────────────

def test_mock_usage_is_labelled_as_mock(tmp_path):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert r.usage.usage_source == "mock" and "MOCK" in r.usage.usage_label and "not provider-reported" in r.usage.usage_label
    assert svc.session_usage().usage_source == "mock" and "MOCK" in svc.session_usage().usage_label
    assert "worst case" in r.usage.conservative_label


def test_provider_usage_and_conservative_charges_are_separate(tmp_path):
    class ProviderLike(live_llm.FakeProvider):
        call_source = live_llm.PROVIDER
    mock = Mock(fail={"business_insight": live_llm.ProviderError("HTTP 500")})
    svc = AnalysisService(ProviderLike(mock), base_dir=str(tmp_path))
    svc.upload("fleet.csv", FLEET)
    a = svc.start(GOAL)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert r.usage.usage_source == "provider" and "Provider-reported" in r.usage.usage_label
    assert r.usage.failed_provider_calls >= 1 and r.usage.estimated_cost_failed_attempts_usd > 0
    assert r.usage.estimated_cost_usd > 0                                       # successful calls only


# ── 5. malformed approval payloads and decision inputs fail closed ──────────────────────────────────────

class _Intr:
    def __init__(self, value):
        self.value = value


@pytest.mark.parametrize("plan", [
    {"tasks": [{"id": 1, "title": "t", "claims": []}]},                                  # non-string id
    {"tasks": [{"id": ["t1"], "title": "t", "claims": []}]},                             # unhashable id
    {"tasks": [{"id": "t1", "claims": []}, {"id": "t1", "claims": []}]},                 # duplicate id
    {"tasks": ["not a task"]}, {"tasks": []}, {"no": "tasks"},
    {"tasks": [{"id": "t1", "claims": [{"key": "k", "op": "sum", "args": {"column": float("nan")}}]}]},
    {"tasks": [{"id": "t1", "claims": [{"key": "k", "op": "sum", "args": ["column"]}]}]},
])
def test_malformed_interrupt_payloads_fail_closed(tmp_path, monkeypatch, plan):
    def fake_start(self, path, thread_id, goal=None, limits=None):
        return None, {"configurable": {"thread_id": thread_id}}, {"__interrupt__": [_Intr({"plan": plan})]}
    monkeypatch.setattr(Supervisor, "start_interactive", fake_start)
    svc, _ = _ready(tmp_path)
    r = svc.start(GOAL)
    assert svc.phase == Phase.FAILED and r.error_code == "invalid_plan_payload" and svc.approval is None
    monkeypatch.undo()
    assert svc.start(GOAL).tasks                                                  # not stuck: a new run works


@pytest.mark.parametrize("keep", [[["t1"]], [1], [None], [{"t1"}], ["t1", 2]])
def test_unhashable_or_non_string_task_ids_are_refused_cleanly(tmp_path, keep):
    svc, mock = _ready(tmp_path)
    a = svc.start(GOAL)
    with pytest.raises(TransitionRefused) as e:
        svc.decide(a.run_id, a.plan_id, "edit", keep_task_ids=keep)
    assert e.value.code == "bad_decision" and svc.phase == Phase.AWAITING_APPROVAL


@pytest.mark.parametrize("decision", [None, 1, ["approve"], {"decision": "approve"}])
def test_non_string_decisions_are_refused(tmp_path, decision):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    with pytest.raises(TransitionRefused):
        svc.decide(a.run_id, a.plan_id, decision)
    assert svc.phase == Phase.AWAITING_APPROVAL


# ── 6. unexpected exceptions never leave the service busy ───────────────────────────────────────────────

def test_a_failing_team_build_does_not_leave_planning(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cannot build")
    monkeypatch.setattr(appsvc, "build_team", boom)
    svc, _ = _ready(tmp_path)
    r = svc.start(GOAL)
    assert svc.phase == Phase.FAILED and r.error_code == "internal_error"
    svc.reset()                                                                   # usable again
    assert svc.phase == Phase.IDLE


def test_a_failing_result_mapping_does_not_leave_running(tmp_path, monkeypatch):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)

    def boom(*a, **k):
        raise ValueError("mapping broke at /opt/internal/x.py")
    monkeypatch.setattr(AnalysisService, "_result_view", boom)
    r = svc.decide(a.run_id, a.plan_id, "approve")
    assert svc.phase == Phase.FAILED and r.error_code == "internal_error" and "/opt/internal" not in _views_json(svc)


def test_a_failing_constructor_leaves_no_directory(tmp_path):
    before = set(os.listdir(tmp_path))
    with pytest.raises(KeyError):
        AnalysisService(live_llm.FakeProvider(Mock()), config=ServiceConfig(model="no-such-model"),
                        base_dir=str(tmp_path))
    assert set(os.listdir(tmp_path)) == before


def test_a_storage_failure_during_upload_keeps_nothing(tmp_path, monkeypatch):
    svc, _ = _svc(tmp_path)

    def broken_fdopen(*a, **k):
        raise OSError("No space left on device: /var/secret")
    monkeypatch.setattr(appsvc.os, "fdopen", broken_fdopen)
    with pytest.raises(InvalidInput) as e:
        svc.upload("fleet.csv", FLEET)
    monkeypatch.undo()
    assert e.value.code == "upload_failed" and "/var/secret" not in e.value.message
    assert os.listdir(svc._data_dir) == [] and svc.phase == Phase.IDLE and svc.dataset is None
    assert svc.upload("fleet.csv", FLEET).rows == 30                              # recovers


# ── 7. session cleanup ──────────────────────────────────────────────────────────────────────────────────

def test_garbage_collecting_a_service_deletes_its_files(tmp_path):
    svc, _ = _ready(tmp_path)
    d = svc._dir
    assert os.path.isdir(d)
    del svc
    gc.collect()
    assert not os.path.exists(d)


def test_stale_session_cleanup_removes_only_abandoned_session_directories(tmp_path):
    keep, _ = _ready(tmp_path)
    old, _ = _ready(tmp_path)
    old_dir = old._dir
    past = time.time() - 13 * 3600
    for name in ("ip-v2-session-foreign", "unrelated"):                        # no marker: never touched, even if
        os.makedirs(tmp_path / name)                                            # an old heartbeat is present
        hb = tmp_path / name / appsvc.HEARTBEAT
        hb.write_text("")
        os.utime(hb, (past, past))
    for f in (appsvc.HEARTBEAT, appsvc.SESSION_MARKER):
        p = os.path.join(old_dir, f)
        if os.path.exists(p):
            os.utime(p, (past, past))
    assert appsvc.cleanup_stale_sessions(str(tmp_path), max_idle_s=12 * 3600) == 1
    assert not os.path.exists(old_dir) and os.path.isdir(keep._dir)
    assert (tmp_path / "ip-v2-session-foreign").is_dir() and (tmp_path / "unrelated").is_dir()


def test_activity_refreshes_the_heartbeat(tmp_path):
    svc, _ = _ready(tmp_path)
    hb = os.path.join(svc._dir, appsvc.HEARTBEAT)
    os.utime(hb, (0, 0))
    svc.start(GOAL)
    assert os.path.getmtime(hb) > 1


# ── 8. cumulative usage survives analysis resets (re-checked with the corrected service) ────────────────

def test_cumulative_spend_survives_reset_and_new_dataset(tmp_path):
    svc, _ = _ready(tmp_path)
    a = svc.start(GOAL)
    svc.decide(a.run_id, a.plan_id, "approve")
    spent = svc.session_usage().estimated_spend_usd
    svc.reset(clear_dataset=True)
    svc.upload("fleet.csv", FLEET)
    assert svc.session_usage().estimated_spend_usd == spent and svc.session_usage().runs_started == 1
