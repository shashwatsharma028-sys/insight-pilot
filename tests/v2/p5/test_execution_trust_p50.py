"""P5.0: execution ports and the code-trust gate. Untrusted code never reaches the legacy subprocess."""

import os
import subprocess

import pytest

import sandbox.executor
from config.feature_flags import load_flags
from multi_agent import execution as ex
from multi_agent.agents import DataAnalystAgent
from multi_agent.llm import LangChainChatAdapter, PROVIDER
from multi_agent.team import build_team
from tests.v2.golden.reference import DATA_DIR, DATASETS
from tests.v2.p5 import scripted

SALES = os.path.join(DATA_DIR, DATASETS["sales"])


class ProviderLikeLLM(scripted.ScriptedAgentLLM):
    """Scripted replies, but labelled like a provider model: its code is untrusted."""
    call_source = PROVIDER
    code_trust = ex.CodeTrust.UNTRUSTED


@pytest.fixture
def no_process(monkeypatch):
    started = []

    def refuse(*a, **k):
        started.append(a)
        raise AssertionError("a process was started")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return started


@pytest.mark.parametrize("cls", [ex.LegacyV1SubprocessExecution, ex.LazySeabornExecution])
def test_legacy_ports_refuse_untrusted_code_at_construction(cls):
    with pytest.raises(ex.UntrustedCodeRefused):
        cls(ex.CodeTrust.UNTRUSTED)
    with pytest.raises(ValueError):
        cls("anything goes")
    port = cls(ex.CodeTrust.SCRIPTED_FIXTURES)
    assert port.hardened is False and "not an isolation boundary" in port.isolation


def test_resolve_port_never_gives_untrusted_code_a_legacy_port():
    assert isinstance(ex.resolve_port(ex.CodeTrust.UNTRUSTED), ex.HardenedContainerExecution)
    assert isinstance(ex.resolve_port(ex.CodeTrust.UNTRUSTED, "lazy-seaborn"), ex.HardenedContainerExecution)
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES), ex.LegacyV1SubprocessExecution)
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES, "lazy-seaborn"), ex.LazySeabornExecution)
    with pytest.raises(ValueError):
        ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES, "fork-server")


def test_hardened_flags_force_the_hardened_port_even_for_fixtures():
    flags = load_flags({"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"})
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES, flags=flags), ex.HardenedContainerExecution)
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES, flags=load_flags({})),
                      ex.LegacyV1SubprocessExecution)


def test_hardened_port_fails_closed_without_starting_a_process(no_process):
    """P5.1A: without a HARDENED bootstrap the container port refuses; it never starts anything."""
    from multi_agent.dataset import sha256_file
    port = ex.HardenedContainerExecution()
    assert port.hardened is True and isinstance(port, ex.ContainerExecutionPort)
    with pytest.raises(ex.IsolationUnavailable):
        port.execute("print(1)", SALES, "t", dataset_sha256=sha256_file(SALES))
    from v2.execution.errors import DatasetNotAuthorizedError
    with pytest.raises(DatasetNotAuthorizedError):           # no profiled identity: refused before anything
        port.execute("print(1)", SALES, "t")
    assert no_process == []


def test_hardened_port_has_no_injectable_backend_or_own_execution_logic():
    """P5.0's injectable backend (an unvalidated second path) is gone: only the shared dispatcher remains."""
    import inspect
    assert list(inspect.signature(ex.ContainerExecutionPort).parameters) == ["strategy", "clock"]
    with pytest.raises(ValueError):
        ex.ContainerExecutionPort(object())
    src = inspect.getsource(ex.ContainerExecutionPort)
    for forbidden in ("subprocess", "Popen", "execute_code", ".run(", "backend"):
        assert forbidden not in src
    assert "hardened_dispatcher()" in src


def test_analyst_refuses_a_provider_model_on_a_legacy_port():
    port = ex.LegacyV1SubprocessExecution(ex.CodeTrust.SCRIPTED_FIXTURES)
    with pytest.raises(ex.UntrustedCodeRefused):
        DataAnalystAgent(ProviderLikeLLM("sales"), port)
    with pytest.raises(ex.UntrustedCodeRefused):
        build_team(ProviderLikeLLM("sales"), port, scripted.approve_all)


def test_trust_is_rechecked_at_run_time():
    port = ex.LegacyV1SubprocessExecution(ex.CodeTrust.SCRIPTED_FIXTURES)
    agent = DataAnalystAgent(scripted.ScriptedAgentLLM("sales"), port)
    agent.llm = ProviderLikeLLM("sales")            # swapped after construction
    with pytest.raises(ex.UntrustedCodeRefused):
        agent.run({"task": scripted.plan_for("sales")["tasks"][0], "profile": {"columns": []},
                   "dataset_path": SALES, "attempt": 1, "feedback": []})


def test_provider_model_with_hardened_port_fails_closed_end_to_end(no_process):
    """P5.1A: unavailable isolation terminates the run at the first attempt; it is never retried."""
    sup, calls = build_team(ProviderLikeLLM("sales", mode="literal"), ex.HardenedContainerExecution(),
                            scripted.approve_all)
    out = sup.run(SALES)
    f = out["final"]
    assert f["status"] == "isolation_unavailable" and f["run_complete"] is False
    assert f["accepted_claims"] == {} and f["verified_claims_for_review"] == {}
    assert f["tasks"]["sales_t1"]["status"] == "aborted" and f["tasks"]["sales_t1"]["attempts"] == 1
    assert all(t["attempts"] == 0 for k, t in f["tasks"].items() if k != "sales_t1")
    terms = [e for e in out["events"] if e["type"] == "execution_terminated"]
    assert [(e["category"], e["error_class"]) for e in terms] == [("isolation_unavailable", "IsolationUnavailable")]
    assert [e for e in out["events"] if e["type"] == "agent_error"] == []
    assert no_process == []
    assert [c["agent"] for c in calls] == ["planning_agent", "data_analyst_agent"]


def test_langchain_adapter_is_labelled_provider_and_untrusted():
    class FakeChat:
        def invoke(self, messages):
            self.messages = messages
            return type("M", (), {"content": "ok", "usage_metadata": {"input_tokens": 3, "output_tokens": 1}})()
    chat = FakeChat()
    a = LangChainChatAdapter(chat)
    assert a.call_source == PROVIDER and a.code_trust is ex.CodeTrust.UNTRUSTED
    assert a.complete("planner", "sys", "user").content == "ok"
    assert [m.content for m in chat.messages] == ["sys", "user"]


def test_legacy_port_matches_the_v1_executor_and_reports_safety_blocks():
    port = ex.LegacyV1SubprocessExecution(ex.CodeTrust.SCRIPTED_FIXTURES)
    code = 'print("CLAIM row_count=" + str(len(df)))'
    v1 = sandbox.executor.execute_code(code, SALES, "parity_v1")
    out = port.execute(code, SALES, "parity_port")
    assert out.ok is v1[0] is True and out.stdout == v1[1] == "CLAIM row_count=293"
    blocked = port.execute("import os\nprint(os.listdir('.'))", SALES, "blocked")
    assert blocked.ok is False and blocked.blocked_by_safety_scan and blocked.stdout == ""
    failed = port.execute('df["nope"]', SALES, "failed")
    assert failed.ok is False and failed.error_type == "data_error" and not failed.blocked_by_safety_scan


def test_ports_do_not_modify_the_v1_sandbox_module():
    before = (sandbox.executor.execute_code, sandbox.executor.wrap_code_safely, sandbox.executor.MAX_EXECUTION_TIME)
    ex.LegacyV1SubprocessExecution(ex.CodeTrust.SCRIPTED_FIXTURES)._impl()
    ex.LazySeabornExecution(ex.CodeTrust.SCRIPTED_FIXTURES)._impl()
    assert before == (sandbox.executor.execute_code, sandbox.executor.wrap_code_safely,
                      sandbox.executor.MAX_EXECUTION_TIME)
