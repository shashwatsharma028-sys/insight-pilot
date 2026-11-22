"""
Hardened-execution contracts (P3.1): types and interfaces only.

These modules do not execute generated code, start processes, invoke
containers, open network connections, import LLM clients or read
credentials. tests/v2/p3/test_contract_purity.py enforces that.
"""

from v2.execution.errors import (
    ARTIFACT_REJECT_REASONS,
    ArtifactRejectedError,
    DatasetNotAuthorizedError,
    SandboxPolicyError,
    SandboxProtocolError,
    SandboxUnavailableError,
)
from v2.execution.isolation import IsolationLevel, isolation_for_backend
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import ExecutionOutcome, ExecutionResult, is_valid_run_id

__all__ = [
    "ARTIFACT_REJECT_REASONS", "ArtifactRejectedError", "DatasetNotAuthorizedError",
    "SandboxPolicyError", "SandboxProtocolError", "SandboxUnavailableError",
    "IsolationLevel", "isolation_for_backend",
    "DEFAULT_LIMITS", "Limits",
    "ExecutionOutcome", "ExecutionResult", "is_valid_run_id",
]
