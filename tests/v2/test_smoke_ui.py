"""
P0: Streamlit smoke tests of the unmodified ui/app.py (offline, scripted LLM).

Drives the real app with streamlit.testing.v1.AppTest:
pick a demo dataset -> run -> plan approval (approve / edit / discard) ->
the UI's own post-approval execution loop -> every results page -> follow-up
chat. This covers the second execution path in ui/app.py that the LangGraph
tests do not reach.
"""

import os
import warnings

import pytest

from tests.v2.golden import reference as ref
from tests.v2.golden import scenarios as sc
from tests.v2.golden.compare import check_snapshot, mismatches, normalize_run, parse_golden

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   "ui", "app.py")
NAV = ["Overview", "Insights", "Data Quality", "Agent Activity",
       "Execution Log", "Follow-up Chat", "Downloads"]


def _button(at, label):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"button {label!r} not rendered; have {[b.label for b in at.button]}"
    return matches[0]


def _no_errors(at):
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


SESSION_KEYS = ["agent_state", "analysis_complete", "awaiting_approval", "uploaded_path",
                "dataset_name", "chat_input", "run_count", "theme", "nav", "confirm_reset"]


def _continue_session(at):
    """
    Re-render the current session. Streamlit 1.59's AppTest (the exam stack)
    keeps the approval page's un-keyed text_area in its element tree after
    st.rerun() and then raises KeyError('$$ID-...') while collecting widget
    state, even though the app state is correct. Streamlit 1.67 does not.
    On exactly that harness error, restart AppTest with the same session
    state and warn, so the workaround is visible in test output.
    See docs/v2/BASELINE.md, "Platform differences".
    """
    try:
        at.run()
        return at
    except KeyError as e:
        if "$$ID-" not in str(e):
            raise
        warnings.warn("Streamlit AppTest stale-widget defect hit; restarting harness "
                      "with identical session state (see docs/v2/BASELINE.md)")
        fresh = AppTest.from_file(APP, default_timeout=240)
        for key in SESSION_KEYS:
            if key in at.session_state:
                fresh.session_state[key] = at.session_state[key]
        fresh.run()
        return fresh


def _to_approval(at):
    at.run()
    _no_errors(at)
    at.button(key="ds_sales_data.csv").click().run()
    _no_errors(at)
    _button(at, "Run autonomous analysis").click().run()
    _no_errors(at)
    assert at.session_state["awaiting_approval"] is True
    assert at.session_state["analysis_complete"] is False


@pytest.fixture
def app(use_scenario):
    controller = use_scenario("sales")
    return AppTest.from_file(APP, default_timeout=240), controller


def test_app_loads_without_dataset(app):
    at, _ = app
    at.run()
    _no_errors(at)
    assert at.session_state["uploaded_path"] is None


def test_full_ui_flow_approve_results_pages_and_chat(app):
    at, controller = app
    _to_approval(at)
    # Only planning happened before approval
    assert controller.calls_by_skill() == {"planner": 1}

    _button(at, "Approve and run").click().run()
    _no_errors(at)
    assert at.session_state["analysis_complete"] is True

    state = at.session_state["agent_state"]
    statuses = {t["id"]: getattr(t["status"], "value", t["status"]) for t in state["analysis_plan"]}
    assert statuses == sc.SALES["expected_status"]
    printed = {}
    for t in state["completed_tasks"]:
        printed.update(parse_golden(t.get("execution_result")))
    assert mismatches(printed, ref.load_expected("sales")["answers"]) == {}
    assert state["final_report_md"] and state["final_report_pdf"] and state["notebook_path"]

    from agent.token_monitor import monitor
    check_snapshot("sales_ui_approve_safe_mode_on",
                   normalize_run(state, controller, monitor.summary(), safe_mode=True))

    at = _continue_session(at)
    _no_errors(at)
    for label in NAV:
        at.button(key=f"nav_{label}").click().run()
        _no_errors(at)
        assert at.session_state["nav"] == label

    at.button(key="nav_Follow-up Chat").click().run()
    at.text_input(key="chat_full").input("Which region leads?")
    at.button(key="send_full").click().run()
    _no_errors(at)
    history = at.session_state["agent_state"]["conversation_history"]
    assert [t["role"] for t in history] == ["user", "agent"]
    assert history[-1]["message"] == sc.FOLLOWUP_REPLY


def test_ui_plan_edit_replans_before_running(app, isolated_outputs):
    at, controller = app
    _to_approval(at)
    at.text_area[0].input("Skip the filesystem inventory task")
    _button(at, "Approve and run").click().run()
    _no_errors(at)
    state = at.session_state["agent_state"]
    assert [t["id"] for t in state["analysis_plan"]] == ["sales_t1", "sales_t2", "sales_t3"]
    assert state["failed_tasks"] == []
    assert controller.calls_by_skill()["planner"] == 2
    # The user correction was stored as a lesson in the isolated store
    assert (isolated_outputs["lessons"] / "lessons.json").exists()


def test_ui_discard_plan_runs_nothing(app):
    at, controller = app
    _to_approval(at)
    _button(at, "Discard plan").click().run()
    _no_errors(at)
    assert at.session_state["awaiting_approval"] is False
    assert at.session_state["agent_state"]["analysis_plan"] == []
    assert "code_generator" not in controller.calls_by_skill()
