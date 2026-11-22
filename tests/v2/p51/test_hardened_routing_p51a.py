"""
P5.1A: P5 through the ONE shared P3 hardened dispatcher (test-double runtime only), routing controls,
terminating execution failures, dataset identity and the review-only rule for incomplete runs.

No test here executes generated code: the runtime is LiteralFakeRuntime (tests/v2/p51/conftest.py).
"""

import os
import subprocess

import pytest

from multi_agent import execution as ex
from multi_agent.dataset import sha256_file
from multi_agent.team import build_team
from multi_agent.verification import values_match
from tests.v2.golden.reference import DATA_DIR, DATASETS
from tests.v2.p5 import scripted
from tests.v2.p51.conftest import literal_responder

EXPECTED_STATUS = {"sales": "completed_with_failures", "attendance": "completed", "student": "completed",
                   "ecommerce": "completed"}


def _path(d):
    return os.path.join(DATA_DIR, DATASETS[d])


def _team(dataset, faults=None, strategy="v1", approval=scripted.approve_all, **kw):
    llm = scripted.ScriptedAgentLLM(dataset, faults, mode="literal")
    port = ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES, strategy)
    sup, calls = build_team(llm, port, approval, **kw)
    return sup, calls, llm, port


@pytest.fixture
def no_process(monkeypatch):
    started = []

    def refuse(*a, **k):
        started.append(a)
        raise AssertionError("a process was started")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    return started


# ── 1. one dispatcher, two callers ───────────────────────────────────────────

@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_p5_runs_end_to_end_through_the_shared_hardened_dispatcher(hardened_p5, dataset, no_process):
    sup, calls, _, port = _team(dataset)
    assert isinstance(port, ex.ContainerExecutionPort)            # resolve_port in a HARDENED process
    f = sup.run(_path(dataset))["final"]
    golden = scripted.golden_answers(dataset)
    assert f["status"] == EXPECTED_STATUS[dataset] and f["run_complete"] is True
    assert set(f["accepted_claims"]) == set(golden)
    assert all(values_match(f["accepted_claims"][k], golden[k]) for k in golden)
    reqs = hardened_p5.runtime.requests
    assert reqs, "the runtime was never asked"
    pin = sha256_file(_path(dataset))
    for r in reqs:
        assert (r.dataset.name, r.dataset.sha256, r.dataset.size) == (
            DATASETS[dataset], pin, os.path.getsize(_path(dataset)))
        assert DATA_DIR not in repr(r) and "/" not in r.dataset.name   # no host path reaches the runtime
    assert hardened_p5.binding.tripwire_hits() == 0 and no_process == []


def test_the_port_and_executor_node_share_one_dispatcher(hardened_p5):
    import agent.nodes.executor as node
    port = ex.ContainerExecutionPort()
    assert port._dispatcher() is hardened_p5.dispatcher is node.execute_code


def test_scanner_blocks_happen_in_the_dispatcher_before_the_runtime(hardened_p5):
    sup, *_ = _team("sales")
    f = sup.run(_path("sales"))["final"]
    assert f["tasks"]["sales_t4"]["status"] == "failed" and f["tasks"]["sales_t4"]["attempts"] == 3
    assert not [r for r in hardened_p5.runtime.requests if r.task_id.startswith("sales_t4")]


def test_lazy_seaborn_strategy_selects_the_trusted_prelude_not_a_legacy_executor(hardened_p5, no_process):
    from v2.execution.runtime import PreludeId
    sup, _, _, port = _team("student", strategy="lazy-seaborn")
    assert isinstance(port, ex.ContainerExecutionPort) and port.strategy == "lazy-seaborn"
    assert sup.run(_path("student"))["final"]["status"] == "completed"
    assert {r.prelude for r in hardened_p5.runtime.requests} == {PreludeId.LAZY_SEABORN}
    assert all("_ip_register_seaborn_colormaps" not in r.code for r in hardened_p5.runtime.requests)


def test_ordinary_code_failures_are_still_retried_with_feedback(hardened_p5):
    from v2.execution.isolation import IsolationLevel
    from v2.execution.results import ExecutionOutcome, ExecutionResult
    seen = []

    def responder(request):
        seen.append(request.task_id)
        if request.task_id == "stu_t2_a1":
            return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                                   outcome=ExecutionOutcome.MEMORY_LIMIT, stdout="", stderr="")
        if request.task_id == "stu_t3_a1":
            return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                                   outcome=ExecutionOutcome.TIMED_OUT, stdout="", stderr="")
        return literal_responder(request)
    hardened_p5.runtime.responder = responder
    sup, _, llm, _ = _team("student")
    f = sup.run(_path("student"))["final"]
    assert f["status"] == "completed" and f["run_complete"] is True
    assert f["tasks"]["stu_t2"]["attempts"] == 2 and f["tasks"]["stu_t3"]["attempts"] == 2
    retry = [p for s, p in llm.prompts if s == "data_analyst" and "TASK ID: stu_t2" in p and "ATTEMPT: 2" in p][0]
    assert "memory limit" in retry


# ── 2. no legacy executor in a hardened process ──────────────────────────────

@pytest.mark.parametrize("cls", [ex.LegacyV1SubprocessExecution, ex.LazySeabornExecution])
def test_legacy_ports_cannot_be_built_in_a_hardened_process_even_for_fixtures(hardened_p5, cls):
    with pytest.raises(ex.UntrustedCodeRefused, match="hardened execution is active"):
        cls(ex.CodeTrust.SCRIPTED_FIXTURES)
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES), ex.ContainerExecutionPort)


@pytest.mark.parametrize("cls", [ex.LegacyV1SubprocessExecution, ex.LazySeabornExecution])
def test_a_legacy_port_built_earlier_refuses_once_the_process_is_hardened(
        cls, p5_hardened_env, hardened_streamlit_options, no_process):
    from tests.v2.p51.conftest import LiteralFakeRuntime
    from v2.execution import bootstrap
    port = cls(ex.CodeTrust.SCRIPTED_FIXTURES)          # before hardening: allowed (reviewed fixtures)
    bootstrap.install_for_tests(LiteralFakeRuntime())
    try:
        with pytest.raises(ex.UntrustedCodeRefused):
            port.execute("print(1)", _path("sales"), "t")
    finally:
        bootstrap._reset_for_tests()
    assert no_process == []


def test_both_subprocess_executors_are_tripwired(hardened_p5, no_process):
    import sandbox.executor
    import sandbox_v2.executor
    from v2.execution.binding import V1ExecutorReached, v1_executor_tripwire
    assert sandbox.executor.execute_code is v1_executor_tripwire
    assert sandbox_v2.executor.execute_code is v1_executor_tripwire
    for fn in (sandbox.executor.execute_code, sandbox_v2.executor.execute_code):
        with pytest.raises(V1ExecutorReached):
            fn("print(1)", _path("sales"), "t")
    from evaluation.harness import _lazy_seaborn_executor
    with pytest.raises(V1ExecutorReached):                 # the P4.1 benchmark variant is covered too
        _lazy_seaborn_executor()("print(1)", _path("sales"), "t")
    assert hardened_p5.binding.tripwire_hits() == 3 and no_process == []
    hardened_p5.binding._reset_tripwire_hits_for_tests()


def test_lazy_executor_drift_is_detected_repaired_and_restored_on_uninstall(p5_hardened_env,
                                                                          hardened_streamlit_options):
    import sandbox_v2.executor as lazy
    from tests.v2.p51.conftest import LiteralFakeRuntime
    from v2.execution import binding, bootstrap
    original = lazy.execute_code
    bootstrap.install_for_tests(LiteralFakeRuntime())
    try:
        lazy.execute_code = original                         # e.g. a reload brought back the real function
        assert binding.verify() == ["sandbox_v2.executor.execute_code is not the V1 tripwire"]
        from v2.execution.errors import SandboxUnavailableError
        with pytest.raises(SandboxUnavailableError, match="not intact"):
            bootstrap.hardened_dispatcher()                  # P5 refuses to dispatch while the binding drifts
        binding.repair()
        assert binding.verify() == [] and lazy.execute_code is binding.v1_executor_tripwire
    finally:
        bootstrap._reset_for_tests()
    assert lazy.execute_code is original


def test_hardened_dispatcher_accessor_fails_closed_outside_hardened_mode():
    from v2.execution import bootstrap
    from v2.execution.errors import SandboxUnavailableError
    with pytest.raises(SandboxUnavailableError, match="not active"):
        bootstrap.hardened_dispatcher()
    with pytest.raises(ex.IsolationUnavailable):
        ex.ContainerExecutionPort().execute("print(1)", _path("sales"), "t", dataset_sha256="0" * 64)


# ── 3. infrastructure and policy failures terminate the run (no retry) ───────

def _raise_at(task_prefix, error):
    def responder(request):
        if request.task_id.startswith(task_prefix):
            raise error
        return literal_responder(request)
    return responder


def test_infrastructure_failure_terminates_and_keeps_verified_claims_for_review_only(hardened_p5, no_process):
    hardened_p5.runtime.responder = _raise_at("sales_t2", RuntimeError("container died"))
    sup, calls, _, _ = _team("sales")
    out = sup.run(_path("sales"))
    f = out["final"]
    assert f["status"] == "isolation_unavailable" and f["run_complete"] is False
    assert f["accepted_claims"] == {} and f["insights"] == []
    golden = scripted.golden_answers("sales")
    assert set(f["verified_claims_for_review"]) == {"row_count", "total_sales_amount", "total_returns",
                                                     "mean_customer_satisfaction"}
    assert all(values_match(v, golden[k]) for k, v in f["verified_claims_for_review"].items())
    assert f["insights_for_review"] and all(i["task_id"] == "sales_t1" for i in f["insights_for_review"])
    t2 = f["tasks"]["sales_t2"]
    assert t2["status"] == "aborted" and t2["attempts"] == 1 and t2["verified_claims"] is None
    assert f["tasks"]["sales_t3"]["attempts"] == 0 and f["tasks"]["sales_t4"]["attempts"] == 0
    assert [r.task_id for r in hardened_p5.runtime.requests].count("sales_t2_a1") == 1
    assert not [r for r in hardened_p5.runtime.requests if r.task_id.startswith(("sales_t3", "sales_t4"))]
    term = [e for e in out["events"] if e["type"] == "execution_terminated"]
    assert [(e["category"], e["error_class"]) for e in term] == [("isolation_unavailable", "SandboxUnavailableError")]
    assert f["execution_terminations"] == 1 and f["agent_errors"] == 0 and no_process == []


def _bad_result(kind):
    from v2.execution.isolation import IsolationLevel
    from v2.execution.results import ExecutionOutcome, ExecutionResult

    def responder(request):
        if kind == "other_run":
            return ExecutionResult(run_id="f" * 32, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                                   outcome=ExecutionOutcome.SUCCEEDED, stdout="", stderr="")
        if kind == "unisolated":
            return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.V1_UNISOLATED,
                                   outcome=ExecutionOutcome.SUCCEEDED, stdout="", stderr="")
        return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                               outcome=ExecutionOutcome.SUCCEEDED, stdout="", stderr="", chart_path="/tmp/x.png")
    return responder


@pytest.mark.parametrize("kind", ["other_run", "unisolated", "chart_before_extraction"])
def test_protocol_violations_terminate_as_isolation_unavailable(hardened_p5, kind):
    hardened_p5.runtime.responder = _bad_result(kind)
    sup, *_ = _team("attendance")
    out = sup.run(_path("attendance"))
    f = out["final"]
    assert f["status"] == "isolation_unavailable" and f["run_complete"] is False
    assert len(hardened_p5.runtime.requests) == 1
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == ["SandboxProtocolError"]


class _StaleReferencePort(ex.ExecutionPort):
    """Simulates a stale call site that still reaches the V1 executor by name."""
    name, isolation, hardened = "stale", "none", False

    def execute(self, code, dataset_path, task_id, dataset_sha256=None):
        import sandbox.executor
        return sandbox.executor.execute_code(code, dataset_path, task_id)


def test_reaching_the_tripwire_terminates_the_run(hardened_p5, no_process):
    sup, _ = build_team(scripted.ScriptedAgentLLM("student", mode="literal"), _StaleReferencePort(),
                        scripted.approve_all)
    out = sup.run(_path("student"))
    assert out["final"]["status"] == "isolation_unavailable"
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == ["V1ExecutorReached"]
    assert hardened_p5.binding.tripwire_hits() == 1 and no_process == []
    hardened_p5.binding._reset_tripwire_hits_for_tests()


class _BrokenPort(ex.ExecutionPort):
    name, isolation, hardened = "broken", "none", False

    def execute(self, code, dataset_path, task_id, dataset_sha256=None):
        raise KeyError("unexpected")


def test_unknown_port_errors_fail_closed_as_infrastructure():
    sup, _ = build_team(scripted.ScriptedAgentLLM("ecommerce", mode="literal"), _BrokenPort(), scripted.approve_all)
    out = sup.run(_path("ecommerce"))
    assert out["final"]["status"] == "isolation_unavailable"
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == [
        "ExecutionInfrastructureError"]


def test_a_non_demo_dataset_is_refused_before_the_runtime(hardened_p5, tmp_path):
    copy = tmp_path / "sales_data.csv"
    copy.write_bytes(open(_path("sales"), "rb").read())
    sup, *_ = _team("sales")
    out = sup.run(str(copy))
    f = out["final"]
    assert f["status"] == "execution_refused" and f["run_complete"] is False
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == [
        "DatasetNotAuthorizedError"]
    assert hardened_p5.runtime.requests == []
    assert f["tasks"]["sales_t1"]["attempts"] == 1


def test_a_profiled_identity_that_differs_from_the_verified_bytes_is_refused(hardened_p5):
    from multi_agent import dataset as dsmod

    def wrong_profile_loader(path):
        ds = dsmod.load(path)
        return dsmod.Dataset(ds.path, "0" * 64, ds.header, ds.rows)    # profiled digest != real bytes
    sup, *_ = _team("student", ds_loader=wrong_profile_loader)
    out = sup.run(_path("student"))
    assert out["final"]["status"] == "execution_refused"
    assert hardened_p5.runtime.requests == []


def test_an_untrusted_model_swapped_in_mid_run_is_refused_not_retried(monkeypatch):
    sup, calls, llm, port = _team("attendance")
    swapped = []
    original_run = sup.analyst.run

    def run(req):
        if not swapped:
            swapped.append(1)
            sup.analyst.llm = type("P", (), {"call_source": "provider", "code_trust": ex.CodeTrust.UNTRUSTED})()
        return original_run(req)
    monkeypatch.setattr(sup.analyst, "run", run)
    out = sup.run(_path("attendance"))
    assert out["final"]["status"] == "execution_refused"
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == ["UntrustedCodeRefused"]


def test_incomplete_runs_never_promote_findings():
    sup, *_ = _team("sales")
    f = sup.run(_path("sales"), limits={"max_steps": 13})["final"]
    assert f["status"] == "budget_exhausted" and f["run_complete"] is False
    assert f["accepted_claims"] == {} and f["insights"] == []
    assert f["verified_claims_for_review"]          # sales_t1 had been verified before the budget ran out
    sup, *_ = _team("sales")
    f = sup.run(_path("sales"))["final"]
    assert f["run_complete"] is True and f["verified_claims_for_review"] == {} and f["insights_for_review"] == []


# ── 4. handoff contract for terminations ─────────────────────────────────────

def test_execution_terminated_contract():
    from multi_agent import contracts as c
    ok = {"category": "isolation_unavailable", "error_class": "SandboxProtocolError", "message": "x"}
    assert c.validate_execution_terminated(ok) == []
    for bad in ({**ok, "category": "retry_please"}, {**ok, "error_class": "DatasetNotAuthorizedError"},
                {**ok, "category": "execution_refused"}, {**ok, "message": "m" * 501}, {**ok, "extra": 1},
                {"category": "isolation_unavailable", "error_class": "SandboxProtocolError"}):
        assert c.validate_execution_terminated(bad), bad
    for sender in (c.PLANNING, c.VERIFIER, c.INSIGHT, c.APPROVAL):
        with pytest.raises(c.HandoffRejected, match="route not allowed"):
            c.envelope(1, sender, c.SUPERVISOR, "execution_terminated", ok)


def test_a_malformed_termination_still_fails_closed():
    from tests.v2.p5.test_supervisor_p50 import _mid_analysis_state
    from multi_agent import contracts as c
    sup, state = _mid_analysis_state()
    state["pending"] = c.envelope(6, c.ANALYST, c.SUPERVISOR, "execution_terminated",
                                  {"category": "retry_please", "error_class": "X", "message": ""}, "stu_t1", 1,
                                  reply_to=5)
    out = sup.supervise(state)
    assert out["status"] == "isolation_unavailable" and out["pending"] is None and out["route"] == "finalize"
    assert out["tasks"]["stu_t1"]["status"] == "aborted"


def test_failure_classification():
    from config.feature_flags import InsecureConfigurationError
    from v2.execution.binding import V1ExecutorReached
    from v2.execution.errors import (ArtifactRejectedError, DatasetNotAuthorizedError, SandboxProtocolError,
                                     SandboxUnavailableError)
    cases = {SandboxUnavailableError("x"): ("isolation_unavailable", "SandboxUnavailableError"),
             SandboxProtocolError("x"): ("isolation_unavailable", "SandboxProtocolError"),
             V1ExecutorReached("x"): ("isolation_unavailable", "V1ExecutorReached"),
             ex.IsolationUnavailable("x"): ("isolation_unavailable", "IsolationUnavailable"),
             InsecureConfigurationError("x"): ("isolation_unavailable", "InsecureConfigurationError"),
             DatasetNotAuthorizedError("x"): ("execution_refused", "DatasetNotAuthorizedError"),
             ArtifactRejectedError("bad_crc"): ("execution_refused", "ArtifactRejectedError"),
             ex.UntrustedCodeRefused("x"): ("execution_refused", "UntrustedCodeRefused")}
    for exc, expected in cases.items():
        assert ex.classify_execution_failure(exc) == expected, exc
    for ordinary in (ValueError("x"), KeyError("x"), RuntimeError("x")):
        assert ex.classify_execution_failure(ordinary) is None


# ── 5. human approval still precedes any execution ───────────────────────────

def test_the_approval_interrupt_fires_before_any_runtime_call(hardened_p5):
    sup, *_ = _team("student", approval=None)
    graph, cfg, paused = sup.start_interactive(_path("student"), "p51a")
    assert "__interrupt__" in paused and hardened_p5.runtime.requests == []
    done = sup.resume(graph, cfg, {"decision": "edit", "keep_task_ids": ["stu_t3"]})
    assert done["final"]["status"] == "completed"
    assert {r.task_id for r in hardened_p5.runtime.requests} == {"stu_t3_a1"}


# ── 6. dataset identity (read-only) ──────────────────────────────────────────

@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_identify_is_read_only_and_matches_the_pin(dataset, tmp_path):
    from v2.execution.datasets import DEMO_DATASETS, DatasetAuthorizer
    path = _path(dataset)
    before = os.stat(path)
    listing = sorted(os.listdir(DATA_DIR))
    ident = DatasetAuthorizer().identify(path)
    assert (ident.name, ident.sha256, ident.size) == (DATASETS[dataset], DEMO_DATASETS[DATASETS[dataset]],
                                                      before.st_size)
    after = os.stat(path)
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (before.st_mtime_ns, before.st_size, before.st_ino)
    assert sorted(os.listdir(DATA_DIR)) == listing


def test_identify_refuses_like_authorize_into(tmp_path):
    from v2.execution.datasets import DEMO_DATASETS, DatasetAuthorizer
    from v2.execution.errors import DatasetNotAuthorizedError
    with pytest.raises(DatasetNotAuthorizedError, match="only the four bundled"):
        DatasetAuthorizer().identify(str(tmp_path / "other.csv"))
    with pytest.raises(DatasetNotAuthorizedError, match="not normalised"):
        DatasetAuthorizer().identify(DATA_DIR + "/../data/sales_data.csv")
    wrong_pin = dict(DEMO_DATASETS, **{"sales_data.csv": "0" * 64})
    with pytest.raises(DatasetNotAuthorizedError, match="does not match the approved SHA-256"):
        DatasetAuthorizer(approved=wrong_pin).identify(_path("sales"))
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    real = data / "real.csv"
    real.write_text("a\n1\n", encoding="utf-8")
    os.symlink(real, data / "sales_data.csv")
    with pytest.raises(DatasetNotAuthorizedError, match="cannot be opened safely"):
        DatasetAuthorizer(data_dir=str(data)).identify(str(data / "sales_data.csv"))


def test_dispatcher_refuses_an_identity_mismatch_before_the_runtime(hardened_p5):
    from v2.execution.errors import DatasetNotAuthorizedError
    with pytest.raises(DatasetNotAuthorizedError, match="differs from the profiled identity"):
        hardened_p5.dispatcher("print(1)", _path("sales"), "t", expected_sha256="1" * 64)
    assert hardened_p5.runtime.requests == []


# ── added after mutation testing (N3, N20) ───────────────────────────────────

@pytest.mark.parametrize("bad", ["/home/user/data/sales_data.csv", "data/sales_data.csv", "../sales_data.csv",
                                 "sales_data.csv\n", "SALES.csv"])
def test_a_dataset_identity_can_never_carry_a_path(bad):
    from v2.execution.runtime import DatasetIdentity
    with pytest.raises(ValueError):
        DatasetIdentity(bad, "0" * 64, 1)
    assert DatasetIdentity("sales_data.csv", "0" * 64, 1).name == "sales_data.csv"


class _LyingHardenedPort(ex.ExecutionPort):
    """Declares itself hardened but returns a result that does not report container isolation."""
    name, isolation, hardened = "liar", "rootless_container", True

    def execute(self, code, dataset_path, task_id, dataset_sha256=None):
        return ex.ExecutionOutcome(True, "CLAIM row_count=293", "", "", "", self.name,
                                   "none: V1 subprocess", 0.0, False)


def test_a_hardened_port_result_without_container_isolation_terminates_the_run():
    sup, _ = build_team(scripted.ScriptedAgentLLM("sales", mode="literal"), _LyingHardenedPort(),
                        scripted.approve_all)
    out = sup.run(_path("sales"))
    assert out["final"]["status"] == "isolation_unavailable" and out["final"]["accepted_claims"] == {}
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == [
        "ExecutionInfrastructureError"]
