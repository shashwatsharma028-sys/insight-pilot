"""
InsightPilot V2 feature flags.

Contract (P0):
- Every flag defaults to OFF / V1 behaviour. With no IP_* variables set the
  application behaves exactly like the examination build.
- No V1 module imports this file in P0. Later phases read flags through
  load_flags() and must not invent their own environment variables.
- Parsing is strict: an invalid value or an unknown IP_FF_* / IP_SANDBOX_* /
  IP_HITL_* variable raises FeatureFlagError instead of silently falling back
  to a default. A typo must never quietly disable a security control.
- The "secure" deployment profile refuses any configuration that would run
  untrusted code without OS/container isolation (validate()), and
  resolve_sandbox_backend() never falls back to the legacy executor once
  hardened execution has been requested.
"""

from dataclasses import dataclass, asdict, fields
from typing import Callable, Mapping, Optional
import os


class FeatureFlagError(ValueError):
    """A flag has an invalid value or an unknown flag name was set."""


class InsecureConfigurationError(FeatureFlagError):
    """The requested configuration would weaken a required security control."""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# Prefixes reserved for V2 flags. Any variable with one of these prefixes that
# is not registered below is rejected (catches typos such as IP_FF_VERIFER).
RESERVED_PREFIXES = ("IP_FF_", "IP_SANDBOX_", "IP_HITL_")

# field name -> (environment variable, kind, allowed values or (min, max))
_SPEC = {
    "deployment_profile": ("IP_DEPLOYMENT_PROFILE", "choice", ("exam", "secure")),
    "sandbox_hardened":   ("IP_FF_SANDBOX_HARDENED", "bool", None),
    "sandbox_backend":    ("IP_SANDBOX_BACKEND", "choice", ("subprocess", "container")),
    "guardrails":         ("IP_FF_GUARDRAILS", "choice", ("off", "monitor", "enforce")),
    "tracing":            ("IP_FF_TRACING", "bool", None),
    "verifier":           ("IP_FF_VERIFIER", "choice", ("off", "shadow", "annotate", "enforce")),
    "hitl_v2":            ("IP_FF_HITL_V2", "bool", None),
    "hitl_code_review":   ("IP_HITL_CODE_REVIEW", "choice", ("off", "high_risk", "all")),
    "hitl_checkpointer":  ("IP_HITL_CHECKPOINTER", "choice", ("none", "memory", "sqlite")),
    "orchestrator":       ("IP_FF_ORCHESTRATOR", "choice", ("v1", "v2")),
    "max_parallel_tasks": ("IP_FF_MAX_PARALLEL_TASKS", "int", (1, 8)),
    "live_llm_budget_usd": ("IP_LIVE_LLM_BUDGET_USD", "float", (0.0, 1000.0)),
}


@dataclass(frozen=True)
class FeatureFlags:
    deployment_profile: str = "exam"      # exam = V1-compatible; secure = V2 hardened deployment
    sandbox_hardened: bool = False        # P3
    sandbox_backend: str = "subprocess"   # P3: "container" = OS-isolated backend
    guardrails: str = "off"               # P2
    tracing: bool = False                 # P5
    verifier: str = "off"                 # P6
    hitl_v2: bool = False                 # P8
    hitl_code_review: str = "off"         # P8
    hitl_checkpointer: str = "none"       # P8: "memory" is for tests only
    orchestrator: str = "v1"              # P7
    max_parallel_tasks: int = 1           # P7
    live_llm_budget_usd: float = 0.0      # Evals: 0 = no paid calls permitted

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def is_v1_default(self) -> bool:
        return self == FeatureFlags()


def _parse(name: str, var: str, kind: str, allowed, raw: str):
    value = raw.strip()
    if kind == "bool":
        low = value.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise FeatureFlagError(f"{var}={raw!r}: expected one of {sorted(_TRUE | _FALSE)}")
    if kind == "choice":
        low = value.lower()
        if low not in allowed:
            raise FeatureFlagError(f"{var}={raw!r}: expected one of {list(allowed)}")
        return low
    if kind in ("int", "float"):
        caster = int if kind == "int" else float
        try:
            number = caster(value)
        except ValueError:
            raise FeatureFlagError(f"{var}={raw!r}: expected {kind}") from None
        lo, hi = allowed
        if not (lo <= number <= hi):
            raise FeatureFlagError(f"{var}={raw!r}: must be between {lo} and {hi}")
        return number
    raise AssertionError(f"unknown flag kind {kind} for {name}")  # pragma: no cover


def load_flags(env: Optional[Mapping[str, str]] = None) -> FeatureFlags:
    """Read flags from `env` (default: os.environ) and validate them."""
    env = os.environ if env is None else env
    known_vars = {spec[0] for spec in _SPEC.values()}

    unknown = sorted(
        k for k in env
        if k.startswith(RESERVED_PREFIXES) and k not in known_vars
    )
    if unknown:
        raise FeatureFlagError(f"Unknown V2 flag(s): {', '.join(unknown)}")

    values = {}
    for name, (var, kind, allowed) in _SPEC.items():
        raw = env.get(var)
        if raw is not None and raw.strip() != "":
            values[name] = _parse(name, var, kind, allowed, raw)

    flags = FeatureFlags(**values)
    validate(flags)
    return flags


def validate(flags: FeatureFlags) -> None:
    """Reject inconsistent or insecure combinations. Raises, never adjusts."""
    if flags.sandbox_backend == "container" and not flags.sandbox_hardened:
        raise FeatureFlagError(
            "IP_SANDBOX_BACKEND=container requires IP_FF_SANDBOX_HARDENED=1")
    if flags.hitl_code_review != "off" and not flags.hitl_v2:
        raise FeatureFlagError("IP_HITL_CODE_REVIEW requires IP_FF_HITL_V2=1")
    if flags.hitl_checkpointer != "none" and not flags.hitl_v2:
        raise FeatureFlagError("IP_HITL_CHECKPOINTER requires IP_FF_HITL_V2=1")
    if flags.max_parallel_tasks > 1 and flags.orchestrator != "v2":
        raise FeatureFlagError("IP_FF_MAX_PARALLEL_TASKS > 1 requires IP_FF_ORCHESTRATOR=v2")

    if flags.deployment_profile == "secure":
        problems = []
        if not flags.sandbox_hardened:
            problems.append("IP_FF_SANDBOX_HARDENED must be 1")
        if flags.sandbox_backend != "container":
            problems.append("IP_SANDBOX_BACKEND must be 'container' (OS-level isolation)")
        if flags.guardrails != "enforce":
            problems.append("IP_FF_GUARDRAILS must be 'enforce'")
        if flags.hitl_checkpointer == "memory":
            problems.append("IP_HITL_CHECKPOINTER=memory is for tests only")
        if problems:
            raise InsecureConfigurationError(
                "Secure deployment profile rejected: " + "; ".join(problems))


def resolve_sandbox_backend(flags: FeatureFlags,
                            is_available: Callable[[str], bool]) -> str:
    """
    Decide which executor runs generated code.

    Returns "legacy" only when hardened execution was NOT requested (V1).
    Once hardened execution is requested, an unavailable backend raises
    InsecureConfigurationError: there is no silent fallback to the
    unisolated legacy executor, in any profile.
    """
    validate(flags)
    if not flags.sandbox_hardened:
        return "legacy"
    backend = flags.sandbox_backend
    if not is_available(backend):
        raise InsecureConfigurationError(
            f"Hardened sandbox backend '{backend}' is unavailable; refusing to "
            "fall back to the legacy executor.")
    return backend


def flag_environment_variables() -> dict:
    """field name -> environment variable (used by docs and tests)."""
    return {name: spec[0] for name, spec in _SPEC.items()}


assert {f.name for f in fields(FeatureFlags)} == set(_SPEC), "flag spec out of sync"
