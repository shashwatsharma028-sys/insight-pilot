"""
Fixtures for the P5.1A integration tests.

- The P3 leak guard is autouse here too: every test starts and ends with the
  original V1 and P4.1 executors, no V2 binding and an uninitialised bootstrap.
- hardened_p5: HARDENED bootstrap mode (IP_FF_SANDBOX_HARDENED=1,
  IP_SANDBOX_BACKEND=container, IP_FF_ORCHESTRATOR=v2) with an injected
  LiteralFakeRuntime. That double NEVER executes code: it interprets only
  print("...") / fail("...") statements with string literals (the scripted
  P5 fixtures' literal mode) and returns a real ExecutionResult.
"""

import ast
from types import SimpleNamespace

import pytest

from tests.v2.p3.conftest import HARDENED_ENV, hardened_streamlit_options  # noqa: F401  (fixture)
from tests.v2.p3.fakes import FakeRuntime
from tests.v2.p3.leak_guard import v2_global_state_guard  # noqa: F401  (autouse fixture)

P5_HARDENED_ENV = {**HARDENED_ENV, "IP_FF_ORCHESTRATOR": "v2"}


def literal_responder(request, overrides=None):
    """Interpret literal print/fail calls only. Nothing is evaluated or executed."""
    from v2.execution.isolation import IsolationLevel
    from v2.execution.results import ExecutionOutcome, ExecutionResult
    printed = []
    for stmt in ast.parse(request.code).body:
        call = stmt.value if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) else None
        if not (call is not None and isinstance(call.func, ast.Name) and call.func.id in ("print", "fail")
                and len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)):
            return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                                   outcome=ExecutionOutcome.CODE_ERROR, stdout="",
                                   stderr="LogicError: unsupported statement in the literal fake runtime")
        if call.func.id == "fail":
            return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                                   outcome=ExecutionOutcome.CODE_ERROR, stdout="",
                                   stderr="Traceback (most recent call last):\n" + call.args[0].value)
        printed.append(call.args[0].value)
    return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                           outcome=ExecutionOutcome.SUCCEEDED, stdout="\n".join(printed), stderr="")


class LiteralFakeRuntime(FakeRuntime):
    """FakeRuntime (is_test_double = True) answering with literal_responder unless a responder is given."""

    def __init__(self, responder=None, **kw):
        super().__init__(responder=responder or literal_responder, **kw)


@pytest.fixture
def p5_hardened_env(monkeypatch):
    for key, value in P5_HARDENED_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(P5_HARDENED_ENV)


@pytest.fixture
def hardened_p5(p5_hardened_env, hardened_streamlit_options):  # noqa: F811
    from v2.execution import binding, bootstrap
    runtime = LiteralFakeRuntime()
    dispatcher = bootstrap.install_for_tests(runtime)
    yield SimpleNamespace(runtime=runtime, dispatcher=dispatcher, bootstrap=bootstrap, binding=binding)
    bootstrap._reset_for_tests()
