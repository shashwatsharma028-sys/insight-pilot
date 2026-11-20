"""
P0: mocked end-to-end regression tests through the unmodified V1 LangGraph
pipeline and the real V1 sandbox subprocess.

Per dataset this proves: ingest -> plan -> code generation -> sandbox
execution -> self-correction retry -> interpretation -> reflection -> report
(Markdown, PDF, notebook) works offline, every golden answer printed by the
sandboxed code equals the independently computed value, and the run matches
the committed V1 behaviour snapshot.
"""

import json
import os

import pytest

from tests.v2.conftest import make_initial_state
from tests.v2.golden import reference as ref
from tests.v2.golden import scenarios as sc
from tests.v2.golden.compare import (
    check_snapshot, mismatches, normalize_run, parse_golden,
)

DATASETS = list(sc.SCENARIOS)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _status(task):
    return getattr(task["status"], "value", task["status"])


def _run_full_graph(dataset):
    from agent.graph import analyst_graph
    return analyst_graph.invoke(make_initial_state(dataset, safe_mode=False, max_retries=2))


@pytest.mark.parametrize("dataset", DATASETS)
def test_full_pipeline_matches_golden_answers_and_v1_snapshot(dataset, use_scenario, isolated_outputs):
    from agent.token_monitor import monitor

    controller = use_scenario(dataset)
    scenario = sc.SCENARIOS[dataset]
    final = _run_full_graph(dataset)

    plan = {t["id"]: t for t in final["analysis_plan"]}
    assert {tid: _status(t) for tid, t in plan.items()} == scenario["expected_status"]

    # Self-correction: retries happened exactly where scripted, nowhere else
    for tid, task in plan.items():
        assert task["retry_count"] == scenario["expected_retries"].get(tid, 0) or _status(task) == "failed", tid

    # Charts: produced where expected, real PNG files, inside the isolated dir
    for tid in scenario["expected_charts"]:
        path = plan[tid]["chart_path"]
        assert path and os.path.dirname(path) == str(isolated_outputs["charts"]), tid
        with open(path, "rb") as f:
            assert f.read(8) == PNG_MAGIC, tid

    # Golden answers: union of everything the sandbox printed vs independent values
    printed = {}
    for task in final["completed_tasks"]:
        printed.update(parse_golden(task.get("execution_result")))
    expected = ref.load_expected(dataset)["answers"]
    assert set(printed) == set(expected), "every golden answer must be produced exactly once"
    assert mismatches(printed, expected) == {}

    # Interpretations and insights for every completed task
    assert all(t.get("interpretation") for t in final["completed_tasks"])
    assert len(final["business_insights"]) == len(final["completed_tasks"])

    # Reports: Markdown, PDF and notebook written to the isolated reports dir
    reports = str(isolated_outputs["reports"])
    assert os.path.dirname(final["final_report_md"]) == reports
    with open(final["final_report_pdf"], "rb") as f:
        assert f.read(5) == b"%PDF-"
    with open(final["notebook_path"], encoding="utf-8") as f:
        assert json.load(f)["cells"]

    # Only scripted model calls happened (the offline guard blocks real ones)
    assert set(controller.calls_by_skill()) <= {"planner", "code_generator", "business_insights"}

    check_snapshot(f"{dataset}_graph_safe_mode_off",
                   normalize_run(final, controller, monitor.summary(), safe_mode=False))


def test_unsafe_generated_code_is_blocked_and_never_executes(use_scenario):
    """V1 AST gate: the sales 'Filesystem Inventory' task imports os every time."""
    use_scenario("sales")
    final = _run_full_graph("sales")
    failed = {t["id"]: t for t in final["failed_tasks"]}
    assert "sales_t4" in failed
    assert "SANDBOX SAFETY VIOLATION" in failed["sales_t4"]["error_message"]
    assert failed["sales_t4"]["retry_count"] == 3  # max_retries=2 -> 3 attempts, then give up
    assert not any("GOLDEN files=" in (t.get("execution_result") or "") for t in final["analysis_plan"])
    # Pipeline continued past the failure and still produced the report
    assert final["final_report_md"]


def test_self_correction_retry_recovers_from_data_error(use_scenario):
    use_scenario("sales")
    final = _run_full_graph("sales")
    t1 = next(t for t in final["analysis_plan"] if t["id"] == "sales_t1")
    assert _status(t1) == "completed" and t1["retry_count"] == 1
    retry_logs = [e for e in final["execution_log"]
                  if e.get("node") == "executor" and e.get("status") == "retry"]
    from agent.state import ErrorType
    assert any(f"[{ErrorType.DATA}]" in e["detail"] for e in retry_logs)


def test_reflection_lessons_written_to_isolated_store_only(use_scenario, isolated_outputs):
    use_scenario("sales")
    _run_full_graph("sales")
    store = isolated_outputs["lessons"] / "lessons.json"
    assert store.exists() and json.loads(store.read_text())


@pytest.mark.parametrize("dataset", DATASETS)
def test_safe_mode_stream_pauses_at_approval_before_any_code(dataset, use_scenario):
    """The UI stops streaming at await_approval; no code is generated before it."""
    from agent.graph import analyst_graph

    controller = use_scenario(dataset)
    nodes = []
    for step in analyst_graph.stream(make_initial_state(dataset, safe_mode=True)):
        node = list(step.keys())[0]
        nodes.append(node)
        if node == "await_approval":
            assert step[node]["plan_approved"] is False
            break
    assert nodes == ["ingest", "plan", "await_approval"]
    assert controller.calls_by_skill() == {"planner": 1}


def test_followup_chat_uses_completed_findings(use_scenario):
    from agent.memory.conversation import handle_followup_node

    controller = use_scenario("sales")
    final = _run_full_graph("sales")
    before = len(controller.calls)
    answered = handle_followup_node({**final, "current_user_message": "Which region leads?",
                                     "is_followup_query": True})
    assert answered["agent_response"] == sc.FOLLOWUP_REPLY
    assert [t["role"] for t in answered["conversation_history"]] == ["user", "agent"]
    assert controller.calls[before]["skill"] == "followup_chat"


def test_followup_context_compression_still_runs(use_scenario, monkeypatch):
    from agent.memory.conversation import handle_followup_node

    use_scenario("sales")
    monkeypatch.setenv("CONTEXT_WINDOW_TOKENS", "2000")
    state = make_initial_state("sales", safe_mode=True)
    state["conversation_history"] = [
        {"role": "user" if i % 2 == 0 else "agent", "message": "x" * 400,
         "timestamp": "2026-01-01T00:00:00", "related_task_id": None}
        for i in range(40)
    ]
    answered = handle_followup_node({**state, "current_user_message": "Summary?"})
    assert answered["agent_response"] == sc.FOLLOWUP_REPLY
    assert answered.get("context_summary")
    assert len(answered["conversation_history"]) < 42
