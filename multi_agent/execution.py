"""
ExecutionPort: the only way the Data Analyst Agent can run code.

Ports and what they guarantee:

- LegacyV1SubprocessExecution: sandbox.executor.execute_code, unchanged (AST
  safety scan, temp-file subprocess, 30 s timeout). It is NOT an isolation
  boundary: the subprocess has the user's file-system and network access.
- LazySeabornExecution: the P4.1 sandbox_v2.executor.execute_code, the same
  process model with the lazy-seaborn prelude (an optional execution strategy).
- HardenedContainerExecution: the integration point for the P3 hardened
  container backend. Not wired in P5.0, so it FAILS CLOSED: execute() raises
  IsolationUnavailable and never starts a process.

Trust rule: the two legacy ports run only code of trust SCRIPTED_FIXTURES
(hand-written test fixtures reviewed in the repository). Model-generated code
is UNTRUSTED and can only be given to a hardened port. This is checked twice:
when a port is constructed and again by the Data Analyst Agent, which compares
the port with the provenance of its LLM. There is no fallback from a hardened
port to a legacy one.

No port changes subprocess permissions, isolation or the V1 sandbox module.
"""

import enum
import time
from dataclasses import dataclass


class CodeTrust(str, enum.Enum):
    SCRIPTED_FIXTURES = "scripted-fixtures"
    UNTRUSTED = "untrusted-model-output"


class UntrustedCodeRefused(PermissionError):
    """Untrusted code was about to reach an execution port without OS-level isolation."""


class IsolationUnavailable(RuntimeError):
    """A hardened backend was requested but is not available; nothing was executed."""


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

    def execute(self, code: str, dataset_path: str, task_id: str) -> ExecutionOutcome:  # pragma: no cover
        raise NotImplementedError


class _LegacyPort(ExecutionPort):
    isolation = "none: V1 subprocess with an AST safety scan (not an isolation boundary)"
    hardened = False

    def __init__(self, code_trust: CodeTrust, clock=time.perf_counter):
        if CodeTrust(code_trust) is not CodeTrust.SCRIPTED_FIXTURES:
            raise UntrustedCodeRefused(
                f"{self.name} has no OS-level isolation and only runs reviewed scripted fixtures; "
                "untrusted model-generated code needs a hardened backend")
        self.code_trust = CodeTrust.SCRIPTED_FIXTURES
        self.clock = clock

    def _impl(self):  # pragma: no cover
        raise NotImplementedError

    def execute(self, code, dataset_path, task_id):
        t0 = self.clock()
        ok, output, chart, error_type = self._impl()(code, dataset_path, task_id)
        seconds = self.clock() - t0
        output = output or ""
        return ExecutionOutcome(
            ok=bool(ok), stdout=output if ok else "", error="" if ok else output,
            error_type=getattr(error_type, "value", error_type) or "", chart_path=chart or "",
            backend=self.name, isolation=self.isolation, seconds=seconds,
            blocked_by_safety_scan=(not ok) and output.startswith("SANDBOX SAFETY VIOLATION"))


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


class HardenedContainerExecution(ExecutionPort):
    """P3 integration point. Until the container backend is wired in, every call fails closed."""
    name = "hardened-container"
    isolation = "container (P3); not available in P5.0"
    hardened = True

    def __init__(self, backend=None):
        self.backend = backend     # a future P3 backend object with run(code, dataset_path, task_id)

    def execute(self, code, dataset_path, task_id):
        if self.backend is None:
            raise IsolationUnavailable("hardened container backend is not configured; refusing to execute")
        return self.backend.run(code, dataset_path, task_id)


STRATEGIES = {"v1": LegacyV1SubprocessExecution, "lazy-seaborn": LazySeabornExecution}


def resolve_port(code_trust, strategy="v1", flags=None) -> ExecutionPort:
    """
    Choose a port. Untrusted code, or flags requesting hardened execution, always get the
    hardened port (which fails closed in P5.0); never a legacy one.
    """
    trust = CodeTrust(code_trust)
    if flags is not None:
        from config.feature_flags import validate
        validate(flags)
        if flags.sandbox_hardened:
            return HardenedContainerExecution()
    if trust is CodeTrust.UNTRUSTED:
        return HardenedContainerExecution()
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown execution strategy {strategy!r}; expected one of {sorted(STRATEGIES)}")
    return STRATEGIES[strategy](trust)
