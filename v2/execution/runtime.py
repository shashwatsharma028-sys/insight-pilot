"""
Execution-runtime interface (P3.2b contract).

The hardened dispatcher hands an ExecutionRequest to an ExecutionRuntime and
gets back an ExecutionResult. This module defines types only.

In this build there is NO production runtime: no validated container backend
exists yet, so hardened mode cannot start outside the offline test suite.
Test doubles live in tests/ and declare is_test_double = True; they are
accepted only by bootstrap.install_for_tests().

The request deliberately carries no dataset path yet: dataset authorisation
(P3.3) must exist before any runtime may receive data.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from v2.execution.isolation import IsolationLevel
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import ExecutionResult, is_valid_run_id


@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    task_id: str
    code: str
    limits: Limits = DEFAULT_LIMITS

    def __post_init__(self):
        if not is_valid_run_id(self.run_id):
            raise ValueError("ExecutionRequest.run_id must be 32 lowercase hex characters")
        if not isinstance(self.task_id, str):
            raise TypeError("ExecutionRequest.task_id must be str")
        if not isinstance(self.code, str):
            raise TypeError("ExecutionRequest.code must be str")
        if not isinstance(self.limits, Limits):
            raise TypeError("ExecutionRequest.limits must be Limits")


@runtime_checkable
class ExecutionRuntime(Protocol):
    name: str
    isolation: IsolationLevel
    is_test_double: bool

    def preflight(self) -> None:
        """Raise SandboxUnavailableError if isolation cannot be provided."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run the request in isolation. Never falls back to the V1 executor."""
