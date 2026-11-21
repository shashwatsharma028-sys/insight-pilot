"""P0: default-off V2 feature flags — strict parsing and no insecure fallback."""

import os
import subprocess
import sys

import pytest

from config.feature_flags import (
    FeatureFlags, FeatureFlagError, InsecureConfigurationError,
    load_flags, resolve_sandbox_backend, flag_environment_variables, validate,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SECURE = {
    "IP_DEPLOYMENT_PROFILE": "secure",
    "IP_FF_SANDBOX_HARDENED": "1",
    "IP_SANDBOX_BACKEND": "container",
    "IP_FF_GUARDRAILS": "enforce",
}


def test_defaults_are_v1():
    flags = load_flags({})
    assert flags == FeatureFlags()
    assert flags.is_v1_default
    assert flags.deployment_profile == "exam"
    assert flags.sandbox_hardened is False
    assert flags.orchestrator == "v1"
    assert flags.verifier == "off"
    assert flags.live_llm_budget_usd == 0.0


def test_blank_values_mean_default():
    assert load_flags({"IP_FF_TRACING": "  "}).is_v1_default


def test_unrelated_environment_is_ignored():
    assert load_flags({"PATH": "/bin", "LLM_MODEL": "x", "IPYTHON": "1"}).is_v1_default


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("ON", True),
                                          ("0", False), ("no", False)])
def test_bool_parsing(raw, expected):
    assert load_flags({"IP_FF_TRACING": raw}).tracing is expected


@pytest.mark.parametrize("env", [
    {"IP_FF_TRACING": "maybe"},
    {"IP_FF_VERIFIER": "strict"},
    {"IP_FF_ORCHESTRATOR": "v3"},
    {"IP_FF_MAX_PARALLEL_TASKS": "0"},
    {"IP_FF_MAX_PARALLEL_TASKS": "two"},
    {"IP_LIVE_LLM_BUDGET_USD": "-1"},
])
def test_invalid_values_raise_instead_of_defaulting(env):
    with pytest.raises(FeatureFlagError):
        load_flags(env)


def test_unknown_reserved_flag_is_rejected():
    with pytest.raises(FeatureFlagError, match="IP_FF_VERIFER"):
        load_flags({"IP_FF_VERIFER": "enforce"})


def test_container_requires_hardened():
    with pytest.raises(FeatureFlagError):
        load_flags({"IP_SANDBOX_BACKEND": "container"})


def test_dependent_flags_require_parent():
    with pytest.raises(FeatureFlagError):
        load_flags({"IP_HITL_CODE_REVIEW": "all"})
    with pytest.raises(FeatureFlagError):
        load_flags({"IP_HITL_CHECKPOINTER": "sqlite"})
    with pytest.raises(FeatureFlagError):
        load_flags({"IP_FF_MAX_PARALLEL_TASKS": "4"})


def test_secure_profile_accepts_isolated_configuration():
    flags = load_flags(SECURE)
    assert flags.deployment_profile == "secure"


@pytest.mark.parametrize("drop_or_change", [
    {"IP_FF_SANDBOX_HARDENED": "0", "IP_SANDBOX_BACKEND": "subprocess"},
    {"IP_SANDBOX_BACKEND": "subprocess"},
    {"IP_FF_GUARDRAILS": "monitor"},
    {"IP_FF_HITL_V2": "1", "IP_HITL_CHECKPOINTER": "memory"},
])
def test_secure_profile_rejects_weakened_configuration(drop_or_change):
    with pytest.raises(InsecureConfigurationError):
        load_flags({**SECURE, **drop_or_change})


def test_legacy_executor_only_when_hardening_not_requested():
    assert resolve_sandbox_backend(FeatureFlags(), lambda b: False) == "legacy"


def test_no_silent_fallback_when_hardened_backend_unavailable():
    flags = FeatureFlags(sandbox_hardened=True, sandbox_backend="subprocess")
    with pytest.raises(InsecureConfigurationError):
        resolve_sandbox_backend(flags, lambda b: False)
    secure = load_flags(SECURE)
    with pytest.raises(InsecureConfigurationError):
        resolve_sandbox_backend(secure, lambda b: False)
    assert resolve_sandbox_backend(secure, lambda b: b == "container") == "container"


def test_validate_rejects_directly_constructed_insecure_flags():
    with pytest.raises(InsecureConfigurationError):
        validate(FeatureFlags(deployment_profile="secure"))


def test_every_flag_documented():
    with open(os.path.join(ROOT, "docs", "v2", "FEATURE_FLAGS.md"), encoding="utf-8") as f:
        doc = f.read()
    for var in flag_environment_variables().values():
        assert var in doc, f"{var} missing from docs/v2/FEATURE_FLAGS.md"


def test_no_v1_module_imports_feature_flags():
    """P0 contract: flags exist but nothing in the V1 app reads them yet."""
    offenders = []
    for folder in ("agent", "sandbox", "utils", "ui"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        if "feature_flags" in f.read():
                            offenders.append(os.path.join(dirpath, name))
    assert offenders == []


def test_importing_app_modules_does_not_load_flags():
    code = ("import sys; sys.path.insert(0, %r); import agent.graph, sandbox.executor; "
            "print('config.feature_flags' in sys.modules)") % ROOT
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
