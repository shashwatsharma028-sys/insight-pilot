"""
Execution-runtime interface (P3.2b contract).

The hardened dispatcher hands an ExecutionRequest to an ExecutionRuntime and
gets back an ExecutionResult. This module defines types only.

In this build there is NO production runtime: no validated container backend
exists yet, so hardened mode cannot start outside the offline test suite.
Test doubles live in tests/ and declare is_test_double = True; they are
accepted only by bootstrap.install_for_tests().

P5.1A contract additions (no runtime behaviour):
- ExecutionRequest.dataset: the IDENTITY of the authorised dataset (approved
  file name, SHA-256 verified by the dispatcher, size). It never carries a
  host path: the planned runtime (P3.6) stages the bytes itself through
  DatasetAuthorizer.authorize_into and must re-check the digest against this
  identity.
- ExecutionRequest.prelude: which TRUSTED prelude the in-image runner applies
  (V1-equivalent, or the P4.1 lazy-seaborn variant). The prelude is image
  content, never generated code; the generated code stays verbatim.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from v2.execution.isolation import IsolationLevel
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import ExecutionResult, is_valid_run_id


_DATASET_NAME = re.compile(r"[a-z0-9_]{1,64}\.csv")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class PreludeId(str, Enum):
    """Trusted in-image prelude selector (an allowlisted enum, never code)."""
    V1 = "v1"                        # equivalent to sandbox.executor.wrap_code_safely
    LAZY_SEABORN = "lazy_seaborn"    # the P4.1 variant (sandbox_v2.lazy_prelude)


@dataclass(frozen=True)
class DatasetIdentity:
    """What the runtime may know about the dataset: no host path."""
    name: str
    sha256: str
    size: int

    def __post_init__(self):
        if not isinstance(self.name, str) or not _DATASET_NAME.fullmatch(self.name):
            raise ValueError("DatasetIdentity.name must be an approved-style file name, not a path")
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise ValueError("DatasetIdentity.sha256 must be 64 lowercase hex characters")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            raise ValueError("DatasetIdentity.size must be a positive int")


@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    task_id: str
    code: str
    limits: Limits = DEFAULT_LIMITS
    dataset: Optional[DatasetIdentity] = None
    prelude: PreludeId = PreludeId.V1

    def __post_init__(self):
        if not is_valid_run_id(self.run_id):
            raise ValueError("ExecutionRequest.run_id must be 32 lowercase hex characters")
        if not isinstance(self.task_id, str):
            raise TypeError("ExecutionRequest.task_id must be str")
        if not isinstance(self.code, str):
            raise TypeError("ExecutionRequest.code must be str")
        if not isinstance(self.limits, Limits):
            raise TypeError("ExecutionRequest.limits must be Limits")
        if self.dataset is not None and not isinstance(self.dataset, DatasetIdentity):
            raise TypeError("ExecutionRequest.dataset must be a DatasetIdentity or None")
        if not isinstance(self.prelude, PreludeId):
            raise TypeError("ExecutionRequest.prelude must be a PreludeId")


@runtime_checkable
class ExecutionRuntime(Protocol):
    name: str
    isolation: IsolationLevel
    is_test_double: bool

    def preflight(self) -> None:
        """Raise SandboxUnavailableError if isolation cannot be provided."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Run the request in isolation. Never falls back to the V1 executor."""
