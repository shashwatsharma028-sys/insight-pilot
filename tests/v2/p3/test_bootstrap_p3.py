"""
P3.2b: V2 bootstrap, fail-closed dispatch binding and integration tests.

Offline. Hardened mode is entered ONLY through bootstrap.install_for_tests()
with tests/v2/p3/fakes.FakeRuntime, which never executes code. Every test
is wrapped by the leak guard (tests/v2/p3/leak_guard.py).

Covers: production fail-closed decisions; no environment-driven runtime;
binding and tripwire; dispatcher mapping and fail-closed errors; both
execution paths (LangGraph graph and Streamlit post-approval loop) through
the single executor_node boundary; reruns; stale imports; sticky failure;
no mode switching; concurrency; deliberate leak / V1-fallback detection.
"""

import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from tests.v2.conftest import make_initial_state
from tests.v2.golden.reference import DATA_DIR
from tests.v2.p3.conftest import HARDENED_ENV, HARDENED_OPTIONS
from tests.v2.p3.fakes import FAKE_MARKER, FakeRuntime, fake_success

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
LAUNCHER = os.path.join(ROOT, "v2", "launch_app.py")
SALES_CSV = os.path.join(DATA_DIR, "sales_data.csv")
BAD_OPTIONS = {"server.fileWatcherType": "auto", "server.runOnSave": True}
V2_EXEC_SOURCES = ["v2/execution/bootstrap.py", "v2/execution/binding.py",
                   "v2/execution/dispatch.py", "v2/execution/runtime.py", "v2/launch_app.py"]


@pytest.fixture
def bootstrap():
    from v2.execution import bootstrap as module
    return module


@pytest.fixture
def binding():
    from v2.execution import binding as module
    return module


def _originals():
    import agent.nodes.executor
    import sandbox.executor
    from tests.v2.p3.leak_guard import ORIGINAL_V1_EXECUTE_CODE
    return agent.nodes.executor, sandbox.executor, ORIGINAL_V1_EXECUTE_CODE


@pytest.fixture
def v1_subprocess_spy(monkeypatch):
    """Records any attempt of the V1 sandbox to start `python <tmp>.py`."""
    import sandbox.executor
    calls = []
    real_run = sandbox.executor.subprocess.run

    def spy(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and args[0] == sys.executable \
                and len(args) == 2 and str(args[1]).endswith(".py"):
            calls.append(list(args))
        return real_run(args, *a, **k)

    monkeypatch.setattr(sandbox.executor.subprocess, "run", spy)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# 1. Production bootstrap: fail closed
# ─────────────────────────────────────────────────────────────────────────────

def test_default_flags_initialise_v1_compatible_and_bind_nothing(bootstrap, binding):
    node, v1, original = _originals()
    assert bootstrap.ensure_ready(env={}) is bootstrap.Mode.V1_COMPATIBLE
    assert bootstrap.ensure_ready(env={}) is bootstrap.Mode.V1_COMPATIBLE
    assert bootstrap.status()["init_count"] == 1
    assert not binding.is_installed()
    assert node.execute_code is original and v1.execute_code is original


def test_production_hardened_request_fails_closed_reporting_all_prerequisites(bootstrap, binding):
    with pytest.raises(bootstrap.BootstrapRefused) as info:
        bootstrap.ensure_ready(env=HARDENED_ENV, options=BAD_OPTIONS)
    message = str(info.value)
    assert message.startswith("V2 features were requested that this build cannot provide yet: "
                              "IP_FF_SANDBOX_HARDENED, IP_SANDBOX_BACKEND.")
    assert "server.fileWatcherType must be 'none'" in message
    assert "server.runOnSave must be False" in message
    assert "no validated container backend exists in this build" in message
    assert "never used as a fallback" in message
    assert bootstrap.mode() is bootstrap.Mode.FAILED
    assert not binding.is_installed()
    node, v1, original = _originals()
    assert node.execute_code is original and v1.execute_code is original


def test_production_hardened_refused_even_when_settings_are_correct(bootstrap, binding):
    with pytest.raises(bootstrap.BootstrapRefused) as info:
        bootstrap.ensure_ready(env=HARDENED_ENV, options=HARDENED_OPTIONS)
    assert "fileWatcherType" not in str(info.value)
    assert "no validated container backend exists in this build" in str(info.value)
    assert not binding.is_installed()


def test_secure_profile_refused_until_p2_guardrails_exist(bootstrap):
    env = {**HARDENED_ENV, "IP_DEPLOYMENT_PROFILE": "secure", "IP_FF_GUARDRAILS": "enforce"}
    with pytest.raises(bootstrap.BootstrapRefused) as info:
        bootstrap.ensure_ready(env=env, options=HARDENED_OPTIONS)
    message = str(info.value)
    assert "needs the P2 guardrails" in message
    assert "a hardened backend alone does not make the application secure" in message
    assert bootstrap.P2_GUARDRAILS_IMPLEMENTED is False


def test_secure_profile_refused_even_with_an_injected_runtime(bootstrap, binding, monkeypatch):
    env = {**HARDENED_ENV, "IP_DEPLOYMENT_PROFILE": "secure", "IP_FF_GUARDRAILS": "enforce"}
    runtime = FakeRuntime()
    with pytest.raises(bootstrap.BootstrapRefused, match="P2 guardrails"):
        bootstrap.install_for_tests(runtime, env=env, options=HARDENED_OPTIONS)
    assert not binding.is_installed() and runtime.requests == []


@pytest.mark.parametrize("env,expected", [
    ({"IP_FF_TRACING": "1"}, "not integrated yet: IP_FF_TRACING"),
    ({"IP_FF_VERIFIER": "shadow"}, "not integrated yet: IP_FF_VERIFIER"),
    ({**HARDENED_ENV, "IP_FF_TRACING": "1"}, "not integrated yet: IP_FF_TRACING"),
    ({"IP_FF_TRACING": "maybe"}, "Invalid V2 feature-flag configuration"),
    ({"IP_FF_SANDBOX_HARDENED": "1"}, "Invalid V2 feature-flag configuration"),
])
def test_other_v2_requests_are_refused(bootstrap, env, expected):
    with pytest.raises(bootstrap.BootstrapRefused, match=expected):
        bootstrap.ensure_ready(env=env, options=HARDENED_OPTIONS)


def test_failure_is_sticky_for_the_process(bootstrap):
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.ensure_ready(env=HARDENED_ENV, options=HARDENED_OPTIONS)
    for env in (HARDENED_ENV, {}):
        with pytest.raises(bootstrap.BootstrapRefused, match="restart the application"):
            bootstrap.ensure_ready(env=env, options=HARDENED_OPTIONS)
    assert bootstrap.status()["init_count"] == 1


def test_no_setting_or_environment_variable_can_supply_a_runtime(bootstrap):
    assert list(inspect.signature(bootstrap.ensure_ready).parameters) == ["env", "options"]
    # A would-be switch is an unknown reserved flag: rejected by strict parsing.
    for var in ("IP_SANDBOX_RUNTIME", "IP_SANDBOX_FAKE", "IP_FF_TEST_RUNTIME"):
        with pytest.raises(bootstrap.BootstrapRefused, match="Unknown V2 flag"):
            bootstrap._reset_for_tests()
            bootstrap.ensure_ready(env={**HARDENED_ENV, var: "fake"}, options=HARDENED_OPTIONS)
    for rel in V2_EXEC_SOURCES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
               {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not {"environ", "getenv", "putenv", "FakeRuntime"} & used, rel
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                  {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m and m.split(".")[0] == "tests" for m in modules), rel


def test_test_only_entry_points_are_called_only_from_tests():
    names = {"install_for_tests", "_reset_for_tests", "_reset_tripwire_hits_for_tests"}
    offenders = []
    for folder in ("agent", "sandbox", "utils", "ui", "v2", "config"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        fn = node.func
                        called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                        if called in names:
                            offenders.append((os.path.relpath(path, ROOT), called))
    # bootstrap._reset_for_tests itself calls binding.uninstall, not a test entry point.
    assert offenders == []


def test_binding_install_is_reachable_only_from_install_for_tests():
    with open(os.path.join(ROOT, "v2", "execution", "bootstrap.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    callers = [fn.name for fn in tree.body if isinstance(fn, ast.FunctionDef)
               for node in ast.walk(fn) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "install"]
    # P3.6-3 (approved R5): install_for_validation is the one additional, validation-only entry point.
    assert callers == ["install_for_tests", "install_for_validation"]


def test_install_for_tests_refuses_outside_pytest():
    code = textwrap.dedent(f"""
        import sys; sys.path.insert(0, {ROOT!r})
        from v2.execution import bootstrap
        class R:
            name = "x"; is_test_double = True
            from v2.execution.isolation import IsolationLevel
            isolation = IsolationLevel.ROOTLESS_CONTAINER
            def preflight(self): pass
            def execute(self, r): raise AssertionError
        try:
            bootstrap.install_for_tests(R(), env={{}}, options={{}})
        except RuntimeError as e:
            print("REFUSED:", e)
        print("pytest loaded:", "pytest" in sys.modules, "mode:", bootstrap.mode().value)
    """)
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                         env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "REFUSED: this bootstrap function is for the offline test suite only" in out.stdout
    assert "pytest loaded: False mode: uninitialized" in out.stdout


class _NotADouble(FakeRuntime):
    is_test_double = False


class _NoIsolation(FakeRuntime):
    from v2.execution.isolation import IsolationLevel as _L
    isolation = _L.V1_UNISOLATED


@pytest.mark.parametrize("case", ["not_a_double", "no_isolation", "preflight_fails",
                                  "bad_options", "not_hardened"])
def test_install_for_tests_runs_every_prerequisite_check(bootstrap, binding, case):
    from v2.execution.errors import SandboxUnavailableError
    runtime = {"not_a_double": _NotADouble(), "no_isolation": _NoIsolation(),
               "preflight_fails": FakeRuntime(preflight_error=SandboxUnavailableError("attestation failed")),
               "bad_options": FakeRuntime(), "not_hardened": FakeRuntime()}[case]
    env = {} if case == "not_hardened" else HARDENED_ENV
    options = BAD_OPTIONS if case == "bad_options" else HARDENED_OPTIONS
    expected = {"not_a_double": (ValueError, "is_test_double = True"),
                "no_isolation": (bootstrap.BootstrapRefused, "rootless container isolation"),
                "preflight_fails": (bootstrap.BootstrapRefused, "attestation failed"),
                "bad_options": (bootstrap.BootstrapRefused, "fileWatcherType"),
                "not_hardened": (bootstrap.BootstrapRefused, "requires IP_FF_SANDBOX_HARDENED=1")}[case]
    with pytest.raises(expected[0], match=expected[1]):
        bootstrap.install_for_tests(runtime, env=env, options=options)
    assert not binding.is_installed()
    assert runtime.requests == []
    if case != "not_a_double":
        assert bootstrap.mode() is bootstrap.Mode.FAILED


def test_install_for_tests_cannot_reinitialise(hardened_test_mode):
    with pytest.raises(RuntimeError, match="already initialised"):
        hardened_test_mode.bootstrap.install_for_tests(FakeRuntime())


# ─────────────────────────────────────────────────────────────────────────────
# 2. Binding and tripwire
# ─────────────────────────────────────────────────────────────────────────────

def test_binding_routes_one_name_and_trips_the_other(hardened_test_mode):
    node, v1, original = _originals()
    b = hardened_test_mode.binding
    assert node.execute_code is hardened_test_mode.dispatcher
    assert v1.execute_code is b.v1_executor_tripwire
    assert node.executor_node.__globals__ is vars(node)     # name looked up at call time
    assert b.verify() == []


def test_uninstall_restores_the_exact_original_functions(hardened_test_mode):
    node, v1, original = _originals()
    hardened_test_mode.bootstrap._reset_for_tests()
    assert node.execute_code is original and v1.execute_code is original
    assert not hardened_test_mode.binding.is_installed()


def test_tripwire_raises_and_counts(hardened_test_mode):
    from config.feature_flags import InsecureConfigurationError
    import sandbox.executor
    b = hardened_test_mode.binding
    with pytest.raises(b.V1ExecutorReached) as info:
        sandbox.executor.execute_code(code="print(1)", csv_path=SALES_CSV, task_id="t")
    assert isinstance(info.value, InsecureConfigurationError)
    assert "no fallback to unisolated execution" in str(info.value)
    assert b.tripwire_hits() == 1
    b._reset_tripwire_hits_for_tests()


def test_install_refuses_an_unexpected_binding_state(binding, monkeypatch):
    import agent.nodes.executor
    from v2.execution.errors import SandboxUnavailableError
    monkeypatch.setattr(agent.nodes.executor, "execute_code", lambda **k: None)
    with pytest.raises(SandboxUnavailableError, match="unexpected executor binding state"):
        binding.install(lambda **k: None)
    assert not binding.is_installed()


def test_verify_detects_drift_and_repair_restores(hardened_test_mode):
    import agent.nodes.executor
    b = hardened_test_mode.binding
    agent.nodes.executor.execute_code = lambda **k: None
    assert b.verify() == ["agent.nodes.executor.execute_code is not the hardened dispatcher"]
    b.repair()
    assert b.verify() == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dispatcher: V1 contract, never a fallback
# ─────────────────────────────────────────────────────────────────────────────

def _dispatcher(responder=None, **kw):
    from v2.execution.dispatch import HardenedDispatcher
    runtime = FakeRuntime(responder=responder, **kw)
    return HardenedDispatcher(runtime), runtime


def _result(request, outcome, stdout="", stderr="", **kw):
    from v2.execution.isolation import IsolationLevel
    from v2.execution.results import ExecutionResult
    return ExecutionResult(run_id=request.run_id, isolation=kw.pop("isolation", IsolationLevel.ROOTLESS_CONTAINER),
                           outcome=outcome, stdout=stdout, stderr=stderr, **kw)


def test_scanner_verdict_and_message_are_identical_to_v1(v1_subprocess_spy):
    from agent.rules_engine import scan_code_safety
    from agent.state import ErrorType
    dispatch, runtime = _dispatcher()
    code = "import os\nprint(os.listdir('.'))"
    assert dispatch(code, SALES_CSV, "t1") == (False, scan_code_safety(code), None, ErrorType.RUNTIME)
    assert runtime.requests == [] and v1_subprocess_spy == []


def test_success_maps_to_the_v1_tuple_and_ignores_chart_markers():
    from v2.execution.results import ExecutionOutcome
    dispatch, runtime = _dispatcher(lambda r: _result(
        r, ExecutionOutcome.SUCCEEDED, stdout="rows 293\nCHART_SAVED:/etc/passwd\n"))
    assert dispatch("print(1)", SALES_CSV, "t1") == (True, "rows 293", None, None)
    dispatch2, _ = _dispatcher(lambda r: _result(r, ExecutionOutcome.SUCCEEDED, stdout="  \n"))
    assert dispatch2("print(1)", SALES_CSV, "t1") == (True, "Execution completed successfully.", None, None)
    assert runtime.requests[0].code == "print(1)" and runtime.requests[0].task_id == "t1"


@pytest.mark.parametrize("stderr,expected", [
    ("Traceback\nKeyError: 'Revenue'", "DATA"), ("SyntaxError: invalid syntax", "SYNTAX"),
    ("NameError: name 'x' is not defined", "RUNTIME"), ("boom", "UNKNOWN"),
])
def test_code_errors_are_classified_with_the_v1_classifier(stderr, expected):
    from agent.state import ErrorType
    from v2.execution.results import ExecutionOutcome
    import sandbox.executor
    dispatch, _ = _dispatcher(lambda r: _result(r, ExecutionOutcome.CODE_ERROR, stderr=stderr))
    ok, out, chart, err = dispatch("print(1)", SALES_CSV, "t1")
    assert (ok, out, chart) == (False, stderr, None)
    assert err == getattr(ErrorType, expected) == sandbox.executor.classify_error(stderr)


def test_timeout_and_memory_messages():
    from agent.state import ErrorType
    from v2.execution.results import ExecutionOutcome
    import sandbox.executor
    dispatch, _ = _dispatcher(lambda r: _result(r, ExecutionOutcome.TIMED_OUT))
    assert dispatch("print(1)", SALES_CSV, "t") == (
        False, f"Execution timed out after {sandbox.executor.MAX_EXECUTION_TIME}s.", None, ErrorType.RUNTIME)
    dispatch, _ = _dispatcher(lambda r: _result(r, ExecutionOutcome.MEMORY_LIMIT))
    assert dispatch("print(1)", SALES_CSV, "t") == (
        False, "Execution exceeded the memory limit (1024 MiB).", None, ErrorType.RUNTIME)


@pytest.mark.parametrize("error", [RuntimeError("container died"), OSError("socket gone"),
                                   ValueError("bad attestation"), KeyError("x")])
def test_any_runtime_error_fails_closed_without_v1(error, v1_subprocess_spy):
    from v2.execution.errors import SandboxUnavailableError

    def boom(request):
        raise error
    dispatch, runtime = _dispatcher(boom)
    with pytest.raises(SandboxUnavailableError, match="failed"):
        dispatch("print(1)", SALES_CSV, "t1")
    assert len(runtime.requests) == 1 and v1_subprocess_spy == []


def test_infrastructure_errors_propagate_unchanged():
    from v2.execution.errors import SandboxProtocolError, SandboxUnavailableError
    for error in (SandboxUnavailableError("busy"), SandboxProtocolError("frame")):
        def boom(request, error=error):
            raise error
        dispatch, _ = _dispatcher(boom)
        with pytest.raises(type(error)) as info:
            dispatch("print(1)", SALES_CSV, "t1")
        assert info.value is error


def _bad_results():
    from v2.execution.isolation import IsolationLevel
    from v2.execution.results import ExecutionOutcome, ExecutionResult
    return {
        "not_a_result": lambda r: ("ok", 1),
        "other_run": lambda r: ExecutionResult(
            run_id="f" * 32, isolation=IsolationLevel.ROOTLESS_CONTAINER,
            outcome=ExecutionOutcome.SUCCEEDED, stdout="", stderr=""),
        "unisolated": lambda r: _result(r, ExecutionOutcome.SUCCEEDED, isolation=IsolationLevel.V1_UNISOLATED),
        "claims_scanner_block": lambda r: _result(r, ExecutionOutcome.BLOCKED_BY_SCANNER),
        "chart_before_extraction_exists": lambda r: _result(r, ExecutionOutcome.SUCCEEDED,
                                                            chart_path="/tmp/x.png"),
    }


@pytest.mark.parametrize("case", sorted(_bad_results()))
def test_unexpected_results_are_protocol_errors(case):
    from v2.execution.errors import SandboxProtocolError
    dispatch, _ = _dispatcher(_bad_results()[case])
    with pytest.raises(SandboxProtocolError):
        dispatch("print(1)", SALES_CSV, "t1")


def test_oversized_code_is_refused_before_the_runtime():
    from agent.state import ErrorType
    dispatch, runtime = _dispatcher()
    ok, out, _, err = dispatch("x = 1\n" * 300_000, SALES_CSV, "t1")
    assert (ok, err) == (False, ErrorType.RUNTIME) and "size limit" in out
    assert runtime.requests == []


def test_dispatcher_requires_a_container_runtime():
    from v2.execution.dispatch import HardenedDispatcher
    from v2.execution.errors import SandboxUnavailableError
    with pytest.raises(SandboxUnavailableError):
        HardenedDispatcher(_NoIsolation())


def test_dispatcher_never_references_the_v1_executor():
    with open(os.path.join(ROOT, "v2", "execution", "dispatch.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "execute_code" not in attrs | names
    assert not {"subprocess", "Popen", "system", "exec", "eval"} & (attrs | names)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Both execution paths use the single executor_node boundary
# ─────────────────────────────────────────────────────────────────────────────

def _executor_entries(state):
    return [e for e in state["execution_log"] if e.get("node") == "executor"]


def _scanner_blocked(entries):
    return [e for e in entries if "SANDBOX SAFETY VIOLATION" in e.get("detail", "")]


def test_path1_langgraph_graph_uses_the_hardened_dispatcher(hardened_test_mode, use_scenario,
                                                            v1_subprocess_spy):
    from agent.graph import analyst_graph
    controller = use_scenario("sales")
    final = analyst_graph.invoke(make_initial_state("sales", safe_mode=False, max_retries=2))
    runtime, b = hardened_test_mode.runtime, hardened_test_mode.binding

    entries = _executor_entries(final)
    assert len(runtime.requests) == len(entries) - len(_scanner_blocked(entries)) > 0
    assert hardened_test_mode.dispatcher.calls == len(entries)
    statuses = {t["id"]: getattr(t["status"], "value", t["status"]) for t in final["analysis_plan"]}
    assert statuses == {"sales_t1": "completed", "sales_t2": "completed",
                        "sales_t3": "completed", "sales_t4": "failed"}
    for task in final["completed_tasks"]:
        assert FAKE_MARKER in task["execution_result"]          # the fake answered
        assert "GOLDEN" not in task["execution_result"]          # the code never ran
    failed = {t["id"]: t for t in final["failed_tasks"]}
    assert "SANDBOX SAFETY VIOLATION" in failed["sales_t4"]["error_message"]
    assert b.tripwire_hits() == 0 and v1_subprocess_spy == []
    assert final["final_report_md"]
    assert controller.calls_by_skill()["code_generator"] >= len(entries)


def _labelled(widgets, label):
    matches = [w for w in widgets if w.label == label]
    assert len(matches) == 1, [w.label for w in widgets]
    return matches[0]


def _button(at, label):
    return _labelled(at.button, label)


def test_path2_streamlit_post_approval_loop_uses_the_hardened_dispatcher(
        hardened_test_mode, use_scenario, v1_subprocess_spy):
    from streamlit.testing.v1 import AppTest
    use_scenario("sales")
    runtime, b = hardened_test_mode.runtime, hardened_test_mode.binding
    at = AppTest.from_file(LAUNCHER, default_timeout=240)
    at.run()
    assert not at.error, [e.value for e in at.error]
    at.button(key="ds_sales_data.csv").click().run()
    _button(at, "Run autonomous analysis").click().run()
    assert at.session_state["awaiting_approval"] is True
    assert runtime.requests == []                       # nothing ran before approval

    _button(at, "Approve and run").click().run()
    assert not at.exception and not at.error
    assert at.session_state["analysis_complete"] is True
    state = at.session_state["agent_state"]
    entries = _executor_entries(state)
    assert len(runtime.requests) == len(entries) - len(_scanner_blocked(entries)) > 0
    assert all(FAKE_MARKER in t["execution_result"] for t in state["completed_tasks"])
    assert b.tripwire_hits() == 0 and v1_subprocess_spy == []
    assert hardened_test_mode.bootstrap.status()["init_count"] == 1
    assert hardened_test_mode.bootstrap.mode() is hardened_test_mode.bootstrap.Mode.HARDENED


def test_ui_graph_path_with_safe_mode_off_uses_the_hardened_dispatcher(
        hardened_test_mode, use_scenario, v1_subprocess_spy):
    from streamlit.testing.v1 import AppTest
    use_scenario("sales")
    runtime = hardened_test_mode.runtime
    at = AppTest.from_file(LAUNCHER, default_timeout=240)
    at.run()
    at.button(key="ds_sales_data.csv").click().run()
    _labelled(at.toggle, "Review plan before running").set_value(False).run()
    _button(at, "Run autonomous analysis").click().run()
    assert at.session_state["analysis_complete"] is True
    entries = _executor_entries(at.session_state["agent_state"])
    assert len(runtime.requests) == len(entries) - len(_scanner_blocked(entries)) > 0
    assert hardened_test_mode.binding.tripwire_hits() == 0 and v1_subprocess_spy == []


def test_executor_node_call_site_census():
    """Only executor_node calls execute_code, so the one binding covers every path."""
    references = []
    for folder in ("agent", "ui", "utils"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for fn in ast.walk(tree):
                    for node in ast.walk(fn) if isinstance(fn, ast.FunctionDef) else []:
                        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "execute_code":
                            references.append((os.path.relpath(path, ROOT), fn.name))
                    if isinstance(fn, (ast.Import, ast.ImportFrom)) and any(
                            a.name == "execute_code" for a in fn.names):
                        references.append((os.path.relpath(path, ROOT), "import"))
    assert sorted(set(references)) == [("agent/nodes/executor.py", "executor_node"),
                                       ("agent/nodes/executor.py", "import")]


# ─────────────────────────────────────────────────────────────────────────────
# 5. Reruns, stale imports, failure states, mode switching, concurrency
# ─────────────────────────────────────────────────────────────────────────────

def test_reruns_keep_one_initialisation_and_an_intact_binding(hardened_test_mode, use_scenario):
    from streamlit.testing.v1 import AppTest
    use_scenario("sales")
    at = AppTest.from_file(LAUNCHER, default_timeout=240)
    at.run()
    at.button(key="ds_sales_data.csv").click().run()
    for _ in range(15):
        at.run()
        assert not at.error and not at.exception
        assert hardened_test_mode.binding.verify() == []
    status = hardened_test_mode.bootstrap.status()
    assert status["init_count"] == 1 and status["repair_count"] == 0 and status["mode"] == "hardened"


def _single_task_state(code="print('x')"):
    return {"current_task_index": 0, "max_retries": 1, "csv_path": SALES_CSV,
            "execution_log": [], "chart_paths": [], "failed_tasks": [],
            "analysis_plan": [{"id": "t1", "title": "Probe", "generated_code": code,
                               "retry_count": 0, "status": "pending"}]}


@pytest.fixture
def restore_module():
    """Snapshot module dicts so a real importlib.reload is fully undone."""
    saved = []

    def snapshot(module):
        saved.append((module, dict(vars(module))))
        return module
    yield snapshot
    for module, contents in reversed(saved):
        vars(module).clear()
        vars(module).update(contents)


def test_stale_executor_module_reload_fails_closed_then_is_repaired(hardened_test_mode, restore_module):
    import agent.nodes.executor as node
    b, boot, runtime = hardened_test_mode.binding, hardened_test_mode.bootstrap, hardened_test_mode.runtime
    restore_module(node)
    importlib.reload(node)                              # re-imports execute_code = the tripwire
    assert node.execute_code is b.v1_executor_tripwire
    with pytest.raises(b.V1ExecutorReached):
        node.executor_node(_single_task_state())
    assert b.tripwire_hits() == 1 and runtime.requests == []

    assert boot.ensure_ready(options=HARDENED_OPTIONS) is boot.Mode.HARDENED   # next rerun
    assert boot.status()["repair_count"] == 1 and b.verify() == []
    result = node.executor_node(_single_task_state())
    assert len(runtime.requests) == 1 and result["next_action"] == "interpret"
    b._reset_tripwire_hits_for_tests()


def test_stale_v1_module_reload_is_detected_and_repaired(hardened_test_mode, restore_module):
    import sandbox.executor as v1
    b, boot = hardened_test_mode.binding, hardened_test_mode.bootstrap
    restore_module(v1)
    importlib.reload(v1)                                # a fresh, unpatched V1 execute_code
    assert v1.execute_code is not b.v1_executor_tripwire
    assert b.verify() == ["sandbox.executor.execute_code is not the V1 tripwire"]
    boot.ensure_ready(options=HARDENED_OPTIONS)
    assert v1.execute_code is b.v1_executor_tripwire and boot.status()["repair_count"] == 1


def test_failed_preflight_is_sticky_and_the_app_never_runs(hardened_env, hardened_streamlit_options,
                                                         use_scenario):
    from streamlit.testing.v1 import AppTest
    from v2.execution import binding, bootstrap
    from v2.execution.errors import SandboxUnavailableError
    controller = use_scenario("sales")
    runtime = FakeRuntime(preflight_error=SandboxUnavailableError("attestation mismatch"))
    with pytest.raises(bootstrap.BootstrapRefused):
        bootstrap.install_for_tests(runtime)
    at = AppTest.from_file(LAUNCHER, default_timeout=60)
    for _ in range(3):
        at.run()
        assert len(at.error) == 1 and "attestation mismatch" in at.error[0].value
        assert len(at.button) == 0
    assert not binding.is_installed() and runtime.requests == [] and controller.calls == []


def test_v1_mode_never_switches_to_hardened(bootstrap, binding):
    assert bootstrap.ensure_ready(env={}) is bootstrap.Mode.V1_COMPATIBLE
    with pytest.raises(bootstrap.BootstrapRefused, match="never switches"):
        bootstrap.ensure_ready(env=HARDENED_ENV, options=HARDENED_OPTIONS)
    assert not binding.is_installed()
    with pytest.raises(bootstrap.BootstrapRefused, match="restart"):
        bootstrap.ensure_ready(env={})


def test_hardened_mode_never_silently_switches_to_v1(hardened_test_mode, monkeypatch):
    boot, b = hardened_test_mode.bootstrap, hardened_test_mode.binding
    for key in HARDENED_ENV:
        monkeypatch.delenv(key)
    with pytest.raises(boot.BootstrapRefused, match="never switches"):
        boot.ensure_ready(options=HARDENED_OPTIONS)
    # The binding stays in place: the V1 executor remains closed for anything still running.
    import sandbox.executor
    assert b.is_installed() and sandbox.executor.execute_code is b.v1_executor_tripwire


def test_hardened_settings_are_rechecked_on_every_run(hardened_test_mode):
    boot = hardened_test_mode.bootstrap
    with pytest.raises(boot.BootstrapRefused, match="no longer hold"):
        boot.ensure_ready(options=BAD_OPTIONS)


def test_concurrent_first_initialisation_happens_once(bootstrap):
    results, barrier = [], threading.Barrier(16)

    def worker():
        barrier.wait()
        results.append(bootstrap.ensure_ready(env={}))
    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [bootstrap.Mode.V1_COMPATIBLE] * 16
    assert bootstrap.status()["init_count"] == 1


def test_concurrent_executions_are_independent(hardened_test_mode):
    import agent.nodes.executor as node
    runtime = hardened_test_mode.runtime
    runtime.delay = 0.05
    outputs, errors = {}, []

    def worker(i):
        try:
            state = _single_task_state()
            state["analysis_plan"][0]["id"] = f"task{i}"
            result = node.executor_node(state)
            outputs[i] = result["analysis_plan"][0]["execution_result"]
        except Exception as exc:       # pragma: no cover - reported below
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker, args=(i,), name=f"session-{i}") for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert all(f"task=task{i} " in outputs[i] for i in range(8))
    run_ids = [r.run_id for r in runtime.requests]
    assert len(run_ids) == len(set(run_ids)) == 8
    assert len(set(runtime.threads)) == 8
    assert hardened_test_mode.binding.tripwire_hits() == 0


def test_interleaved_sessions_share_one_mode(hardened_test_mode, use_scenario):
    from streamlit.testing.v1 import AppTest
    use_scenario("sales")
    sessions = [AppTest.from_file(LAUNCHER, default_timeout=240) for _ in range(2)]
    for at in sessions:
        at.run()
        at.button(key="ds_sales_data.csv").click().run()
        _button(at, "Run autonomous analysis").click().run()
    for at in sessions:
        _button(at, "Approve and run").click().run()
        assert at.session_state["analysis_complete"] is True
        assert all(FAKE_MARKER in t["execution_result"] for t in at.session_state["agent_state"]["completed_tasks"])
    boot = hardened_test_mode.bootstrap
    assert boot.status()["init_count"] == 1 and boot.mode() is boot.Mode.HARDENED
    assert hardened_test_mode.binding.tripwire_hits() == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6. Deliberate leaks and V1 fallbacks make tests fail (subprocess pytest runs)
# ─────────────────────────────────────────────────────────────────────────────

_PRELUDE = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
import pytest
OPTIONS = {HARDENED_OPTIONS!r}
ENV = {HARDENED_ENV!r}
SALES = {SALES_CSV!r}
from tests.v2.p3.fakes import FakeRuntime
"""

_LEAKY = _PRELUDE + """
__GUARD_IMPORT__
def test_a_leaks_the_binding():
    from v2.execution import bootstrap
    bootstrap.install_for_tests(FakeRuntime(), env=ENV, options=OPTIONS)   # never reset
"""

_V1_AFTER = _PRELUDE + """
def test_b_v1_regression_still_runs_the_original_executor():
    import sandbox.executor
    assert sandbox.executor.execute_code.__module__ == "sandbox.executor"
    ok, out, _, _ = sandbox.executor.execute_code(code='print("v1-ok", len(df))', csv_path=SALES, task_id="probe")
    assert ok and "v1-ok 293" in out, out
"""

_FALLBACK = _PRELUDE + """
from tests.v2.p3.leak_guard import v2_global_state_guard  # noqa: F401
from v2.execution.results import ExecutionOutcome

class FallsBackToV1(FakeRuntime):
    def execute(self, request):
        import sandbox.executor          # a "helpful" fallback to the legacy executor
        return sandbox.executor.execute_code(code=request.code, csv_path=SALES, task_id=request.task_id)

def test_c_runtime_falling_back_to_v1_is_caught():
    from v2.execution import bootstrap
    import agent.nodes.executor as node
    bootstrap.install_for_tests(FallsBackToV1(), env=ENV, options=OPTIONS)
    try:
        ok, out, _, _ = node.execute_code(code="print(1)", csv_path=SALES, task_id="t1")
        assert ok, out
    finally:
        bootstrap._reset_for_tests()
"""


def _pytest_run(tmp_path, files):
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    env = {k: v for k, v in os.environ.items()
           if k != "GOOGLE_API_KEY" and not k.startswith(("IP_", "PYTEST_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run([sys.executable, "-m", "pytest", str(tmp_path), "-q", "-p", "no:cacheprovider",
                          "-p", "no:logging", "-rA", "--rootdir", str(tmp_path)],
                         capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=600)
    return out.returncode, out.stdout + out.stderr


def test_a_deliberate_binding_leak_fails_and_is_repaired_before_v1_tests(tmp_path):
    guard = "from tests.v2.p3.leak_guard import v2_global_state_guard  # noqa: F401"
    code, output = _pytest_run(tmp_path, {"test_a_leak.py": _LEAKY.replace("__GUARD_IMPORT__", guard),
                                          "test_b_v1.py": _V1_AFTER})
    assert code != 0, output
    assert "ERROR at teardown of test_a_leaks_the_binding" in output, output
    assert "V2 global state leaked OUT of this test (repaired)" in output
    assert "the V2 executor binding was left installed" in output
    assert "PASSED test_b_v1.py::test_b_v1_regression_still_runs_the_original_executor" in output


def test_control_without_the_guard_the_leak_breaks_the_next_v1_test(tmp_path):
    code, output = _pytest_run(tmp_path, {"test_a_leak.py": _LEAKY.replace("__GUARD_IMPORT__", ""),
                                          "test_b_v1.py": _V1_AFTER})
    assert code != 0, output
    assert "PASSED test_a_leak.py::test_a_leaks_the_binding" in output
    assert "FAILED test_b_v1.py::test_b_v1_regression_still_runs_the_original_executor" in output
    assert "V1ExecutorReached" in output or "AssertionError" in output


def test_a_deliberate_v1_fallback_fails(tmp_path):
    code, output = _pytest_run(tmp_path, {"test_c_fallback.py": _FALLBACK})
    assert code != 0, output
    assert "FAILED test_c_fallback.py::test_c_runtime_falling_back_to_v1_is_caught" in output
    assert "SandboxUnavailableError" in output
    assert "the V1 executor was reached 1 time(s) (tripwire)" in output
