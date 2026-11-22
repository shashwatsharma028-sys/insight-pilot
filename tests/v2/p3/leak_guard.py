"""
Leak guard for V2 process-global state (P3.2b).

tests/v2/p3 is collected BEFORE the P0 tests (including the 13 strict-xfail
V1 security cases and the golden end-to-end runs), all in one process. A
binding, tripwire or Streamlit option left behind by a P3 test would change
what those V1 tests exercise. This autouse fixture therefore, before and
after every P3 test:

- resets the V2 bootstrap to UNINITIALIZED (silently: refusal states are a
  legitimate test outcome);
- detects, REPAIRS and then FAILS the test for: an installed executor
  binding, unacknowledged V1-tripwire hits, a changed V1 executor identity,
  or changed Streamlit server options.

Repair happens before the failure is reported, so the next test always
starts from the original V1 state. tests/v2/p3/test_bootstrap_p3.py proves,
in subprocesses, that a deliberate leak is caught and that without this
guard it would break a following V1 test.
"""

import sys

import pytest

import agent.nodes.executor as _node_module
import sandbox.executor as _v1_module
import sandbox_v2.executor as _lazy_module       # P5.1A: the tripwire also covers the P4.1 executor

ORIGINAL_V1_EXECUTE_CODE = _v1_module.execute_code
ORIGINAL_LAZY_EXECUTE_CODE = _lazy_module.execute_code
assert ORIGINAL_LAZY_EXECUTE_CODE.__module__ == "sandbox_v2.executor", "P4.1 executor already rebound at import"
assert _node_module.execute_code is ORIGINAL_V1_EXECUTE_CODE, "V1 executor already rebound at import"
assert ORIGINAL_V1_EXECUTE_CODE.__module__ == "sandbox.executor"

GUARDED_STREAMLIT_OPTIONS = ("server.fileWatcherType", "server.runOnSave")


def _streamlit_options():
    from streamlit import config
    return {key: config.get_option(key) for key in GUARDED_STREAMLIT_OPTIONS}


BASELINE_STREAMLIT_OPTIONS = _streamlit_options()


def restore_and_report() -> list:
    """Restore every guarded global; return what had leaked."""
    problems = []
    binding = sys.modules.get("v2.execution.binding")
    bootstrap = sys.modules.get("v2.execution.bootstrap")

    if binding is not None and binding.is_installed():
        problems.append("the V2 executor binding was left installed")
    if bootstrap is not None:
        bootstrap._reset_for_tests()          # also uninstalls any binding
    elif binding is not None:
        binding.uninstall()
    if binding is not None and binding.tripwire_hits():
        problems.append(f"the V1 executor was reached {binding.tripwire_hits()} time(s) (tripwire)")
        binding._reset_tripwire_hits_for_tests()

    if _v1_module.execute_code is not ORIGINAL_V1_EXECUTE_CODE:
        problems.append("sandbox.executor.execute_code is not the original V1 function")
        _v1_module.execute_code = ORIGINAL_V1_EXECUTE_CODE
    if _node_module.execute_code is not ORIGINAL_V1_EXECUTE_CODE:
        problems.append("agent.nodes.executor.execute_code is not the original V1 function")
        _node_module.execute_code = ORIGINAL_V1_EXECUTE_CODE
    if _lazy_module.execute_code is not ORIGINAL_LAZY_EXECUTE_CODE:
        problems.append("sandbox_v2.executor.execute_code is not the original P4.1 function")
        _lazy_module.execute_code = ORIGINAL_LAZY_EXECUTE_CODE

    current = _streamlit_options()
    if current != BASELINE_STREAMLIT_OPTIONS:
        problems.append(f"Streamlit options changed: {current} != {BASELINE_STREAMLIT_OPTIONS}")
        from streamlit import config
        for key, value in BASELINE_STREAMLIT_OPTIONS.items():
            config.set_option(key, value)
    return problems


@pytest.fixture(autouse=True)
def v2_global_state_guard(monkeypatch):
    leaked_before = restore_and_report()
    if leaked_before:
        pytest.fail("V2 global state leaked INTO this test (repaired): " + "; ".join(leaked_before),
                    pytrace=False)
    yield
    # The parent conftest's autouse fixtures already use monkeypatch, so its
    # automatic undo would only run AFTER this teardown. Undo it now: only
    # state that survives the test's own cleanup counts as a leak.
    monkeypatch.undo()
    leaked_after = restore_and_report()
    if leaked_after:
        pytest.fail("V2 global state leaked OUT of this test (repaired): " + "; ".join(leaked_after),
                    pytrace=False)
