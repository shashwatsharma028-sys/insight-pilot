"""
Fixtures for the V2 baseline suite (P0).

Guarantees for every test in tests/v2:
- OFFLINE: the real Gemini chat client cannot be constructed, GOOGLE_API_KEY
  is removed from the environment (also from sandbox subprocesses, which
  inherit it), and in-process sockets may only connect to loopback / UNIX
  sockets. Zero paid API calls are possible from this suite.
  Limitation: the socket guard covers this test process only, not the
  sandbox subprocess; sandbox network isolation is a P3 deliverable.
- ISOLATED: charts, reports and lessons are written to a per-test temp
  directory, never to the repository's reports/ or lessons/ folders.
  The demo datasets in data/ are only read.
"""

import ipaddress
import os
import socket

import pytest

from tests.v2.golden import scenarios as sc
from tests.v2.golden.reference import DATA_DIR


class OfflineViolation(RuntimeError):
    pass


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    import utils.llm

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    class _BlockedChatModel:
        def __init__(self, *args, **kwargs):
            raise OfflineViolation("Real LLM client constructed in an offline test")

    monkeypatch.setattr(utils.llm, "ChatGoogleGenerativeAI", _BlockedChatModel)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _allowed(sock, address):
        return sock.family == getattr(socket, "AF_UNIX", None) or _is_loopback(address)

    def guarded_connect(sock, address):
        if not _allowed(sock, address):
            raise OfflineViolation(f"Network connection blocked in offline test: {address!r}")
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _allowed(sock, address):
            raise OfflineViolation(f"Network connection blocked in offline test: {address!r}")
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield


@pytest.fixture(autouse=True)
def isolated_outputs(tmp_path, monkeypatch):
    import sandbox.executor
    import agent.nodes.report_generator as report_generator
    from agent.token_monitor import monitor

    out = tmp_path / "v2_outputs"
    charts = out / "reports" / "charts"
    lessons = out / "lessons"
    charts.mkdir(parents=True)
    lessons.mkdir(parents=True)

    monkeypatch.setattr(sandbox.executor, "CHARTS_DIR", str(charts))
    monkeypatch.setattr(report_generator, "REPORTS_DIR", str(out / "reports"))
    monkeypatch.setenv("LESSONS_DIR", str(lessons))
    monitor.reset()
    yield {"root": out, "charts": charts, "reports": out / "reports", "lessons": lessons}
    monitor.reset()


@pytest.fixture
def use_scenario(monkeypatch):
    """Install the scripted LLM for a named golden scenario."""
    from tests.v2.mock_llm import MockLLMController

    def _install(name: str) -> MockLLMController:
        controller = MockLLMController(sc.SCENARIOS[name])
        controller.install(monkeypatch)
        return controller

    return _install


def make_initial_state(dataset: str, safe_mode: bool, max_retries: int = 2) -> dict:
    """Same shape as the state ui/app.py builds for a run."""
    scenario = sc.SCENARIOS[dataset]
    return {
        "csv_path": os.path.join(DATA_DIR, scenario["file"]),
        "dataset_name": scenario["dataset_name"],
        "df_summary": None, "df_dtypes": None, "df_shape": None, "df_sample": None,
        "data_quality_report": None,
        "analysis_plan": [], "plan_approved": False, "user_edits_to_plan": None,
        "current_task_index": 0, "completed_tasks": [], "failed_tasks": [],
        "max_retries": max_retries,
        "chart_paths": [], "execution_log": [],
        "final_report_md": None, "final_report_pdf": None, "notebook_path": None,
        "business_insights": [],
        "conversation_history": [],
        "current_user_message": None, "agent_response": None,
        "safe_mode": safe_mode,
        "is_followup_query": False,
        "error_message": None, "next_action": None,
    }
