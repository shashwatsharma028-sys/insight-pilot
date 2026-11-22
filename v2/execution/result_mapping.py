"""
Decoded frame -> ExecutionResult, and chart handling (P3.6-1). Host side, no process.

result_from_frame() is the ONLY translation from the untrusted, already-decoded IPX1 frame
(v2.execution.frame.decode_frame) into the ExecutionResult contract:

    frame status                          outcome
    timed_out                             TIMED_OUT
    oom_killed                            MEMORY_LIMIT
    child_exit == 0                       SUCCEEDED
    child_exit != 0, or a child signal    CODE_ERROR

Text sections go through frame.display_text (UTF-8 with replacement; NUL and C0 controls
except \\n and \\t removed; CHART_SAVED lines dropped). A chart is considered only for a
SUCCEEDED run: its bytes pass the host stage-B validator and re-encoder
(artifacts.reencode_png) and are published by ChartStore (no overwrite, server-generated
name). The stage-A reason from the container, or the stage-B reason, is carried as
chart_rejected_reason (a fixed code; never attacker text).
"""

import logging

from v2.execution import frame as frame_mod
from v2.execution.errors import ArtifactRejectedError, SandboxProtocolError
from v2.execution.isolation import IsolationLevel
from v2.execution.limits import DEFAULT_LIMITS
from v2.execution.results import ExecutionOutcome, ExecutionResult

_log = logging.getLogger("insightpilot.v2.result_mapping")


def outcome_of(decoded) -> ExecutionOutcome:
    if decoded.timed_out:
        return ExecutionOutcome.TIMED_OUT
    if decoded.oom_killed:
        return ExecutionOutcome.MEMORY_LIMIT
    if decoded.child_exit == 0 and decoded.child_signal is None:
        return ExecutionOutcome.SUCCEEDED
    return ExecutionOutcome.CODE_ERROR


def publish_chart(decoded, chart_store, task_id, run_id, limits=DEFAULT_LIMITS):
    """(chart_path, chart_rejected_reason) for a SUCCEEDED frame; never raises for bad chart bytes."""
    from v2.execution.artifacts import reencode_png
    from v2.execution.chart_store import ChartPublishedWithCleanupError
    if decoded.chart_reject_reason is not None:
        return None, decoded.chart_reject_reason
    if not decoded.chart:
        return None, None
    try:
        trusted = reencode_png(decoded.chart, limits)
    except ArtifactRejectedError as exc:
        return None, exc.reason
    try:
        return chart_store.publish(trusted, task_id, run_id), None
    except ChartPublishedWithCleanupError as exc:      # published; a cleanup step failed: keep the chart
        _log.warning("chart published with a cleanup warning: %s", exc)
        return exc.path, None
    except ArtifactRejectedError as exc:
        return None, exc.reason


def result_from_frame(decoded, *, run_id, duration_ms, chart_path=None, chart_rejected_reason=None) -> ExecutionResult:
    if decoded.run_id != run_id:
        raise SandboxProtocolError("the result frame belongs to a different run")
    outcome = outcome_of(decoded)
    if outcome is not ExecutionOutcome.SUCCEEDED:
        chart_path, chart_rejected_reason = None, None
    return ExecutionResult(
        run_id=run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER, outcome=outcome,
        stdout=frame_mod.display_text(decoded.stdout), stderr=frame_mod.display_text(decoded.stderr),
        stdout_truncated=decoded.stdout_truncated, stderr_truncated=decoded.stderr_truncated,
        chart_path=chart_path, chart_rejected_reason=chart_rejected_reason, duration_ms=int(duration_ms))
