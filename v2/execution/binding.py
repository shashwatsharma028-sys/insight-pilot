"""
Executor binding and V1-executor tripwire (P3.2b).

Routing only, NOT a security boundary. Rebinding Python names decides which
function the unchanged executor_node calls; it isolates nothing. Isolation
must come from the container backend (planned). In this build the binding
is installed only by bootstrap.install_for_tests() with a test double.

While installed:
- agent.nodes.executor.execute_code -> the hardened dispatcher. This one name
  serves both execution paths: the LangGraph "execute" node and the
  Streamlit post-approval loop both call executor_node, which looks the name
  up at call time.
- sandbox.executor.execute_code -> v1_executor_tripwire. Anything that
  reaches the V1 subprocess executor (a module reload, a stale reference, a
  new call site) raises instead of running code without isolation.
- sandbox_v2.executor.execute_code -> v1_executor_tripwire as well (P5.1A).
  The P4.1 lazy-seaborn executor is the V1 process model with another
  prelude and starts its own subprocess, so it must be covered too. The
  module is imported at install time so a later import cannot bring back an
  unpatched function; verify() and repair() cover it.

Every change is recorded and reversible (uninstall). Process-global by
design: one server process has one execution mode.
"""

import threading

from config.feature_flags import InsecureConfigurationError
from v2.execution.errors import SandboxUnavailableError

_LOCK = threading.RLock()
_installed = None          # {"original": <V1 execute_code>, "dispatcher": <callable>}
_tripwire_hits = 0


class V1ExecutorReached(InsecureConfigurationError):
    """The V1 subprocess executor was reached while hardened mode is active."""


def v1_executor_tripwire(*args, **kwargs):
    global _tripwire_hits
    with _LOCK:
        _tripwire_hits += 1
    raise V1ExecutorReached(
        "The V1 subprocess executor was reached while hardened execution is active. "
        "Refusing: there is no fallback to unisolated execution.")


def _modules():
    import agent.nodes.executor as node_module
    import sandbox.executor as v1_module
    return node_module, v1_module


def _lazy_module():
    """The P4.1 V2 executor module, or None on a build without it."""
    try:
        import sandbox_v2.executor as lazy_module
    except ImportError:
        return None
    return lazy_module


def install(dispatcher) -> None:
    global _installed
    if not callable(dispatcher):
        raise TypeError("dispatcher must be callable")
    with _LOCK:
        if _installed is not None:
            raise RuntimeError("executor binding is already installed")
        node_module, v1_module = _modules()
        lazy_module = _lazy_module()
        original = v1_module.execute_code
        lazy_original = lazy_module.execute_code if lazy_module is not None else None
        if (original is v1_executor_tripwire
                or getattr(original, "__module__", None) != "sandbox.executor"
                or node_module.execute_code is not original
                or (lazy_module is not None
                    and getattr(lazy_original, "__module__", None) != "sandbox_v2.executor")):
            raise SandboxUnavailableError(
                "unexpected executor binding state; refusing to install the hardened binding")
        node_module.execute_code = dispatcher
        v1_module.execute_code = v1_executor_tripwire
        if lazy_module is not None:
            lazy_module.execute_code = v1_executor_tripwire
        _installed = {"original": original, "dispatcher": dispatcher,
                      "lazy_module": lazy_module, "lazy_original": lazy_original}


def is_installed() -> bool:
    with _LOCK:
        return _installed is not None


def verify() -> list:
    """Return the drift found (empty list: the binding is intact)."""
    with _LOCK:
        if _installed is None:
            return ["binding is not installed"]
        node_module, v1_module = _modules()
        problems = []
        if node_module.execute_code is not _installed["dispatcher"]:
            problems.append("agent.nodes.executor.execute_code is not the hardened dispatcher")
        if v1_module.execute_code is not v1_executor_tripwire:
            problems.append("sandbox.executor.execute_code is not the V1 tripwire")
        lazy_module = _installed["lazy_module"]
        if lazy_module is not None and lazy_module.execute_code is not v1_executor_tripwire:
            problems.append("sandbox_v2.executor.execute_code is not the V1 tripwire")
        return problems


def repair() -> None:
    with _LOCK:
        if _installed is None:
            raise RuntimeError("executor binding is not installed")
        node_module, v1_module = _modules()
        node_module.execute_code = _installed["dispatcher"]
        v1_module.execute_code = v1_executor_tripwire
        if _installed["lazy_module"] is not None:
            _installed["lazy_module"].execute_code = v1_executor_tripwire


def uninstall() -> None:
    """Restore the recorded V1 functions. Used by tests; never in production."""
    global _installed
    with _LOCK:
        if _installed is None:
            return
        node_module, v1_module = _modules()
        node_module.execute_code = _installed["original"]
        v1_module.execute_code = _installed["original"]
        if _installed["lazy_module"] is not None:
            _installed["lazy_module"].execute_code = _installed["lazy_original"]
        _installed = None


def tripwire_hits() -> int:
    with _LOCK:
        return _tripwire_hits


def _reset_tripwire_hits_for_tests() -> None:
    global _tripwire_hits
    with _LOCK:
        _tripwire_hits = 0
