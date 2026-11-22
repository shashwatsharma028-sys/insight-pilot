"""
Execution-result contract (P3.1).

ExecutionResult describes one hardened execution. It is a data contract only:
the planned dispatcher (P3.6) will build it and convert it to V1's
(success, output, chart_path, error_type) tuple. Nothing builds one yet.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from v2.execution.errors import ARTIFACT_REJECT_REASONS
from v2.execution.isolation import IsolationLevel

_RUN_ID = re.compile(r"[0-9a-f]{32}")


def is_valid_run_id(value) -> bool:
    """A run id is exactly 32 lowercase hex characters (128 random bits)."""
    return isinstance(value, str) and _RUN_ID.fullmatch(value) is not None


class ExecutionOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    CODE_ERROR = "code_error"                  # generated code exited non-zero
    TIMED_OUT = "timed_out"                    # code wall clock exceeded
    MEMORY_LIMIT = "memory_limit"              # memory cgroup OOM kill
    BLOCKED_BY_SCANNER = "blocked_by_scanner"  # V1 AST scan refused it; never executed


@dataclass(frozen=True)
class ExecutionResult:
    run_id: str
    isolation: IsolationLevel
    outcome: ExecutionOutcome
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    chart_path: Optional[str] = None
    chart_rejected_reason: Optional[str] = None
    duration_ms: int = 0

    def __post_init__(self):
        if not is_valid_run_id(self.run_id):
            raise ValueError("ExecutionResult.run_id must be 32 lowercase hex characters")
        if not isinstance(self.isolation, IsolationLevel):
            raise TypeError("ExecutionResult.isolation must be an IsolationLevel")
        if not isinstance(self.outcome, ExecutionOutcome):
            raise TypeError("ExecutionResult.outcome must be an ExecutionOutcome")
        for name in ("stdout", "stderr"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"ExecutionResult.{name} must be str")
        for name in ("stdout_truncated", "stderr_truncated"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ExecutionResult.{name} must be bool")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int) \
                or self.duration_ms < 0:
            raise ValueError("ExecutionResult.duration_ms must be a non-negative int")
        if self.chart_path is not None:
            if not isinstance(self.chart_path, str) or not self.chart_path:
                raise TypeError("ExecutionResult.chart_path must be a non-empty str or None")
            if self.outcome is not ExecutionOutcome.SUCCEEDED:
                raise ValueError("a chart is only accepted from a successful execution")
            if self.chart_rejected_reason is not None:
                raise ValueError("chart_path and chart_rejected_reason are mutually exclusive")
        if self.chart_rejected_reason is not None \
                and self.chart_rejected_reason not in ARTIFACT_REJECT_REASONS:
            raise ValueError(f"unknown chart_rejected_reason: {self.chart_rejected_reason!r}")
        if self.outcome is ExecutionOutcome.BLOCKED_BY_SCANNER and self.duration_ms != 0:
            raise ValueError("scanner-blocked code never ran, so duration_ms must be 0")

    @property
    def succeeded(self) -> bool:
        return self.outcome is ExecutionOutcome.SUCCEEDED
