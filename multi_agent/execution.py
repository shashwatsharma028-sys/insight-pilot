"""
ExecutionPort: the only way the Data Analyst Agent can run code.

Ports and what they guarantee:

- LegacyV1SubprocessExecution: sandbox.executor.execute_code, unchanged (AST
  safety scan, temp-file subprocess, 30 s timeout). It is NOT an isolation
  boundary: the subprocess has the user's file-system and network access.
- LazySeabornExecution: the P4.1 sandbox_v2.executor.execute_code, the same
  process model with the lazy-seaborn prelude (an optional execution strategy).
- ContainerExecutionPort (alias HardenedContainerExecution): the P3 hardened
  path. It owns NO execution logic of its own: every call goes through the one
  process-wide v2.execution.dispatch.HardenedDispatcher obtained from
  v2.execution.bootstrap.hardened_dispatcher(), the same dispatcher the V1
  graph uses (P5.1A, "one dispatcher, two callers"). Unless the bootstrap is in
  HARDENED mode with an intact binding it raises IsolationUnavailable and
  starts nothing. In this build production can never reach HARDENED (no
  validated container backend exists), so it always fails closed outside the
  offline test suite.

Trust rule: the two legacy ports run only code of trust SCRIPTED_FIXTURES
(hand-written test fixtures reviewed in the repository). Model-generated code
is UNTRUSTED and can only be given to the container port. This is checked:
- when a port is constructed and on every legacy execution;
- again by the Data Analyst Agent, which compares the port with the provenance
  of its LLM;
- and (P5.1A) legacy ports refuse to be constructed or used at all in a
  process whose V2 bootstrap is HARDENED, while the P3 binding also tripwires
  both legacy executors. There is no fallback from the container port to a
  legacy one.

Failure families (P5.1A), see classify_execution_failure():
- infrastructure ("isolation_unavailable"): config.feature_flags.
  InsecureConfigurationError and its subclasses (SandboxUnavailableError,
  SandboxProtocolError, V1ExecutorReached, IsolationUnavailable,
  ExecutionInfrastructureError);
- policy ("execution_refused"): SandboxPolicyError (e.g. DatasetNotAuthorizedError)
  and UntrustedCodeRefused.
Both terminate the run; neither is retried by asking the model for new code.

No port changes subprocess permissions, isolation or the V1 sandbox module.
"""

import enum
import sys
import time
from dataclasses import dataclass

from config.feature_flags import InsecureConfigurationError
from v2.execution.errors import DatasetNotAuthorizedError, SandboxPolicyError, SandboxUnavailableError


class CodeTrust(str, enum.Enum):
    SCRIPTED_FIXTURES = "scripted-fixtures"
    UNTRUSTED = "untrusted-model-output"


class UntrustedCodeRefused(PermissionError):
    """Code was about to reach an execution port that is not allowed to run it (policy)."""


class IsolationUnavailable(SandboxUnavailableError):
    """Hardened execution was requested but is not available; nothing was executed."""


class ExecutionInfrastructureError(SandboxUnavailableError):
    """An execution port failed in an unexpected way; treated as infrastructure (fail closed)."""


ISOLATION_UNAVAILABLE = "isolation_unavailable"
EXECUTION_REFUSED = "execution_refused"

# error_class values the Supervisor accepts in an execution_terminated handoff
INFRASTRUCTURE_CLASSES = ("SandboxUnavailableError", "SandboxProtocolError", "V1ExecutorReached",
                          "IsolationUnavailable", "ExecutionInfrastructureError", "InsecureConfigurationError")
POLICY_CLASSES = ("DatasetNotAuthorizedError", "ArtifactRejectedError", "SandboxPolicyError",
                  "UntrustedCodeRefused")


def classify_execution_failure(exc):
    """(category, error_class) for a terminating execution failure, or None for an ordinary one."""
    if isinstance(exc, (SandboxPolicyError, UntrustedCodeRefused)):
        return EXECUTION_REFUSED, _known_name(exc, POLICY_CLASSES, "SandboxPolicyError")
    if isinstance(exc, InsecureConfigurationError):
        return ISOLATION_UNAVAILABLE, _known_name(exc, INFRASTRUCTURE_CLASSES, "InsecureConfigurationError")
    return None


def _known_name(exc, known, fallback):
    for cls in type(exc).__mro__:
        if cls.__name__ in known:
            return cls.__name__
    return fallback


@dataclass
class ExecutionOutcome:
    ok: bool
    stdout: str
    error: str
    error_type: str
    chart_path: str
    backend: str
    isolation: str
    seconds: float
    blocked_by_safety_scan: bool

    def as_dict(self) -> dict:
        return dict(self.__dict__)


class ExecutionPort:
    name = "abstract"
    isolation = "none"
    hardened = False

    def execute(self, code: str, dataset_path: str, task_id: str,
                dataset_sha256: str = None) -> ExecutionOutcome:  # pragma: no cover
        raise NotImplementedError


def hardened_process_active() -> bool:
    """True when this process's V2 bootstrap is in HARDENED mode (it is never imported by V1)."""
    bootstrap = sys.modules.get("v2.execution.bootstrap")
    return bootstrap is not None and bootstrap.mode() is bootstrap.Mode.HARDENED


def _outcome_from_v1_tuple(result, name, isolation, seconds):
    ok, output, chart, error_type = result
    output = output or ""
    return ExecutionOutcome(
        ok=bool(ok), stdout=output if ok else "", error="" if ok else output,
        error_type=getattr(error_type, "value", error_type) or "", chart_path=chart or "",
        backend=name, isolation=isolation, seconds=seconds,
        blocked_by_safety_scan=(not ok) and output.startswith("SANDBOX SAFETY VIOLATION"))


class _LegacyPort(ExecutionPort):
    isolation = "none: V1 subprocess with an AST safety scan (not an isolation boundary)"
    hardened = False

    def __init__(self, code_trust: CodeTrust, clock=time.perf_counter):
        if CodeTrust(code_trust) is not CodeTrust.SCRIPTED_FIXTURES:
            raise UntrustedCodeRefused(
                f"{self.name} has no OS-level isolation and only runs reviewed scripted fixtures; "
                "untrusted model-generated code needs a hardened backend")
        self._refuse_in_hardened_process()
        self.code_trust = CodeTrust.SCRIPTED_FIXTURES
        self.clock = clock

    def _refuse_in_hardened_process(self):
        if hardened_process_active():
            raise UntrustedCodeRefused(
                f"{self.name} is disabled: hardened execution is active in this process, and "
                "unisolated executors never run code alongside it")

    def _impl(self):  # pragma: no cover
        raise NotImplementedError

    def execute(self, code, dataset_path, task_id, dataset_sha256=None):
        self._refuse_in_hardened_process()
        t0 = self.clock()
        result = self._impl()(code, dataset_path, task_id)
        return _outcome_from_v1_tuple(result, self.name, self.isolation, self.clock() - t0)


class LegacyV1SubprocessExecution(_LegacyPort):
    name = "legacy-v1-subprocess"

    def _impl(self):
        import sandbox.executor
        return sandbox.executor.execute_code


class LazySeabornExecution(_LegacyPort):
    name = "v2-lazy-seaborn-subprocess"

    def _impl(self):
        from sandbox_v2.executor import execute_code
        return execute_code


STRATEGIES = {"v1": LegacyV1SubprocessExecution, "lazy-seaborn": LazySeabornExecution}
PRELUDES = ("v1", "lazy-seaborn")       # container-side trusted prelude for each strategy


class ContainerExecutionPort(ExecutionPort):
    """The P3 hardened path through the shared dispatcher. Fails closed unless HARDENED."""
    name = "rootless-container (P3 hardened dispatcher)"
    isolation = "rootless_container"
    hardened = True

    def __init__(self, strategy="v1", clock=time.perf_counter):
        if strategy not in PRELUDES:
            raise ValueError(f"unknown execution strategy {strategy!r}; expected one of {list(PRELUDES)}")
        self.strategy, self.clock = strategy, clock

    def _dispatcher(self):
        from v2.execution import bootstrap
        try:
            return bootstrap.hardened_dispatcher()
        except SandboxUnavailableError as exc:
            raise IsolationUnavailable(f"hardened container execution is unavailable: {exc}") from exc

    def execute(self, code, dataset_path, task_id, dataset_sha256=None):
        from v2.execution.runtime import PreludeId
        if dataset_sha256 is None:
            raise DatasetNotAuthorizedError(
                "Dataset not authorised: container execution needs the profiled dataset identity")
        dispatcher = self._dispatcher()
        prelude = PreludeId.LAZY_SEABORN if self.strategy == "lazy-seaborn" else PreludeId.V1
        t0 = self.clock()
        result = dispatcher(code, dataset_path, task_id, expected_sha256=dataset_sha256, prelude=prelude)
        if not (isinstance(result, tuple) and len(result) == 4):
            raise ExecutionInfrastructureError("the hardened dispatcher returned an unexpected result")
        if result[2] is not None and not (isinstance(result[2], str) and result[2]):
            raise ExecutionInfrastructureError("the hardened dispatcher returned an invalid chart path")
        return _outcome_from_v1_tuple(result, self.name, self.isolation, self.clock() - t0)


# P5.0 name kept for compatibility; it is the same class (no separate backend injection exists).
HardenedContainerExecution = ContainerExecutionPort


def resolve_port(code_trust, strategy="v1", flags=None) -> ExecutionPort:
    """
    Choose a port. Untrusted code, flags requesting hardened execution, or a process whose
    bootstrap is HARDENED, always get the container port (which fails closed unless the
    hardened dispatcher is available); never a legacy one.
    """
    trust = CodeTrust(code_trust)
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown execution strategy {strategy!r}; expected one of {sorted(STRATEGIES)}")
    if flags is not None:
        from config.feature_flags import validate
        validate(flags)
        if flags.sandbox_hardened:
            return ContainerExecutionPort(strategy)
    if trust is CodeTrust.UNTRUSTED or hardened_process_active():
        return ContainerExecutionPort(strategy)
    return STRATEGIES[strategy](trust)
