"""P3.1: pure execution contracts (isolation levels, limits, errors, results)."""

import dataclasses

import pytest

from config.feature_flags import FeatureFlagError, InsecureConfigurationError
from v2.execution import (
    ARTIFACT_REJECT_REASONS, DEFAULT_LIMITS, ArtifactRejectedError,
    DatasetNotAuthorizedError, ExecutionOutcome, ExecutionResult, IsolationLevel,
    Limits, SandboxPolicyError, SandboxProtocolError, SandboxUnavailableError,
    is_valid_run_id, isolation_for_backend,
)

RUN_ID = "0123456789abcdef0123456789abcdef"


# ── Isolation levels ─────────────────────────────────────────────────────────

def test_exactly_two_isolation_levels_and_no_process_tier():
    assert {level.value for level in IsolationLevel} == {"v1_unisolated", "rootless_container"}


def test_only_the_container_level_is_os_isolated():
    assert IsolationLevel.ROOTLESS_CONTAINER.os_isolated is True
    assert IsolationLevel.V1_UNISOLATED.os_isolated is False


def test_resolved_backends_map_to_levels():
    assert isolation_for_backend("legacy") is IsolationLevel.V1_UNISOLATED
    assert isolation_for_backend("container") is IsolationLevel.ROOTLESS_CONTAINER


@pytest.mark.parametrize("bad", ["subprocess", "process", "", None, "CONTAINER"])
def test_unknown_backends_are_rejected(bad):
    with pytest.raises(ValueError):
        isolation_for_backend(bad)


# ── Limits ───────────────────────────────────────────────────────────────────

def test_default_code_wall_clock_matches_v1_timeout():
    import sandbox.executor
    assert DEFAULT_LIMITS.code_wall_seconds == sandbox.executor.MAX_EXECUTION_TIME == 30


def test_limits_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_LIMITS.memory_bytes = 1


def test_frame_budget_is_derived_from_capped_sections():
    lim = DEFAULT_LIMITS
    assert lim.stderr_bytes == lim.stderr_head_bytes + lim.stderr_tail_bytes
    assert lim.max_frame_bytes == (lim.frame_header_bytes + lim.stdout_bytes
                                   + lim.stderr_bytes + lim.chart_bytes)
    assert lim.host_deadline_seconds == lim.code_wall_seconds + lim.host_grace_seconds


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(Limits)])
@pytest.mark.parametrize("bad", [0, -1, True, "1", None])
def test_every_limit_must_be_positive(field, bad):
    with pytest.raises(ValueError):
        Limits(**{field: bad})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.5, 0.0])
def test_cpus_must_be_positive_and_finite(bad):
    with pytest.raises(ValueError, match="positive finite"):
        Limits(cpus=bad)


@pytest.mark.parametrize("good", [0.25, 1, 2.0, 64])
def test_cpus_accepts_positive_finite_numbers(good):
    assert Limits(cpus=good).cpus == good


@pytest.mark.parametrize("kwargs", [
    {"chart_max_side": 100, "chart_max_pixels": 100 * 100 + 1},
    {"chart_bytes": 33 * 1024 * 1024, "out_bytes": 32 * 1024 * 1024},
    {"memory_bytes": 96 * 1024 * 1024},          # tmp 64 MiB + out 32 MiB not below memory
])
def test_inconsistent_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        Limits(**kwargs)


# ── Errors ───────────────────────────────────────────────────────────────────

def test_policy_and_infrastructure_errors_are_separate_families():
    assert issubclass(DatasetNotAuthorizedError, SandboxPolicyError)
    assert issubclass(ArtifactRejectedError, SandboxPolicyError)
    assert not issubclass(SandboxPolicyError, FeatureFlagError)
    assert issubclass(SandboxUnavailableError, InsecureConfigurationError)
    assert issubclass(SandboxProtocolError, SandboxUnavailableError)
    assert not issubclass(SandboxUnavailableError, SandboxPolicyError)


def test_artifact_reasons_are_a_fixed_set():
    err = ArtifactRejectedError("symlink")
    assert err.reason == "symlink" and "symlink" in str(err)
    with pytest.raises(ValueError):
        ArtifactRejectedError("../../etc/passwd")
    assert all(r.replace("_", "").isalpha() for r in ARTIFACT_REJECT_REASONS)


# ── Results ──────────────────────────────────────────────────────────────────

def _result(**overrides):
    base = dict(run_id=RUN_ID, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                outcome=ExecutionOutcome.SUCCEEDED, stdout="rows 293", stderr="")
    base.update(overrides)
    return ExecutionResult(**base)


def test_valid_success_result():
    res = _result(chart_path="/charts/t1-0123abcd.png", duration_ms=1200)
    assert res.succeeded
    assert res.isolation.os_isolated


@pytest.mark.parametrize("value,ok", [
    (RUN_ID, True), (RUN_ID.upper(), False), (RUN_ID[:-1], False), (RUN_ID + "0", False),
    ("../" + RUN_ID[3:], False), (123, False), (None, False),
])
def test_run_id_format(value, ok):
    assert is_valid_run_id(value) is ok


@pytest.mark.parametrize("overrides,exc", [
    ({"run_id": "not-a-run-id"}, ValueError),
    ({"isolation": "rootless_container"}, TypeError),
    ({"outcome": "succeeded"}, TypeError),
    ({"stdout": b"bytes"}, TypeError),
    ({"stderr_truncated": 1}, TypeError),
    ({"duration_ms": -1}, ValueError),
    ({"duration_ms": 1.5}, ValueError),
    ({"chart_path": ""}, TypeError),
    ({"outcome": ExecutionOutcome.CODE_ERROR, "chart_path": "/c/x.png"}, ValueError),
    ({"chart_path": "/c/x.png", "chart_rejected_reason": "symlink"}, ValueError),
    ({"chart_rejected_reason": "something else"}, ValueError),
    ({"outcome": ExecutionOutcome.BLOCKED_BY_SCANNER, "duration_ms": 5}, ValueError),
])
def test_result_invariants(overrides, exc):
    with pytest.raises(exc):
        _result(**overrides)


def test_rejected_chart_keeps_task_successful():
    res = _result(chart_rejected_reason="dimensions_exceeded")
    assert res.succeeded and res.chart_path is None


def test_results_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _result().stdout = "changed"
