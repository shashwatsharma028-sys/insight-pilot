"""
P3.1: hardened execution exists only as the container backend (design D1, B1).

New tests only. The P0 tests in tests/v2/test_feature_flags.py are unchanged
and must keep passing as they are.
"""

import os

import pytest

from config.feature_flags import (
    FeatureFlagError, FeatureFlags, InsecureConfigurationError,
    load_flags, resolve_sandbox_backend, validate,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

GUARDRAILS_AND_PROFILE = {"IP_DEPLOYMENT_PROFILE": "secure", "IP_FF_GUARDRAILS": "enforce"}


@pytest.mark.parametrize("env", [
    {"IP_FF_SANDBOX_HARDENED": "1"},                                   # backend left at default
    {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "subprocess"},
    {"IP_FF_SANDBOX_HARDENED": "true", "IP_SANDBOX_BACKEND": "SUBPROCESS"},
    {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "  "},       # blank = default
])
def test_hardened_subprocess_rejected_in_exam_profile(env):
    with pytest.raises(InsecureConfigurationError, match="IP_SANDBOX_BACKEND=container"):
        load_flags(env)


def test_hardened_subprocess_rejected_in_secure_profile():
    env = {**GUARDRAILS_AND_PROFILE, "IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "subprocess"}
    with pytest.raises(InsecureConfigurationError):
        load_flags(env)


@pytest.mark.parametrize("profile", ["exam", "secure"])
def test_directly_constructed_hardened_subprocess_rejected(profile):
    flags = FeatureFlags(deployment_profile=profile, sandbox_hardened=True,
                         sandbox_backend="subprocess", guardrails="enforce")
    with pytest.raises(InsecureConfigurationError, match="no hardened subprocess mode"):
        validate(flags)


def test_rejection_is_still_a_feature_flag_error():
    # Callers that catch FeatureFlagError keep catching it.
    with pytest.raises(FeatureFlagError):
        load_flags({"IP_FF_SANDBOX_HARDENED": "1"})


def test_resolver_never_returns_subprocess_even_if_available():
    flags = FeatureFlags(sandbox_hardened=True, sandbox_backend="subprocess")
    with pytest.raises(InsecureConfigurationError):
        resolve_sandbox_backend(flags, lambda backend: True)


def test_hardened_container_in_exam_profile_is_valid():
    flags = load_flags({"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"})
    assert flags.deployment_profile == "exam"
    assert flags.sandbox_hardened is True and flags.sandbox_backend == "container"
    assert resolve_sandbox_backend(flags, lambda b: b == "container") == "container"
    with pytest.raises(InsecureConfigurationError):
        resolve_sandbox_backend(flags, lambda b: False)


@pytest.mark.parametrize("env", [{}, {"IP_SANDBOX_BACKEND": "subprocess"},
                                 {"IP_FF_SANDBOX_HARDENED": "0", "IP_SANDBOX_BACKEND": "subprocess"}])
def test_legacy_v1_unchanged_when_hardening_off(env):
    flags = load_flags(env)
    assert flags.sandbox_hardened is False
    assert flags.sandbox_backend == "subprocess"
    assert flags.is_v1_default
    # V1 is selected without consulting backend availability at all.
    assert resolve_sandbox_backend(flags, lambda b: pytest.fail("availability consulted")) == "legacy"


def test_docs_state_the_container_requirement():
    with open(os.path.join(ROOT, "docs", "v2", "FEATURE_FLAGS.md"), encoding="utf-8") as f:
        doc = f.read()
    assert "requires `IP_SANDBOX_BACKEND=container`" in doc
    assert "no hardened subprocess mode" in doc
    assert "`streamlit run ui/app.py` is pure V1" in doc
