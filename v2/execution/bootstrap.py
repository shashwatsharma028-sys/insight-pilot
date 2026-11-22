"""
V2 bootstrap (P3.2b): decides, once per process, whether and how the V2
launcher may run ui/app.py.

Modes (process-wide, thread-safe, never switched once decided):

- V1_COMPATIBLE: every flag at its default. Nothing is bound; ui/app.py runs
  exactly as with `streamlit run ui/app.py`.
- HARDENED: hardened container execution with the executor binding
  installed. **Reachable only through install_for_tests() with an explicitly
  injected test double.** In this build no validated container backend
  exists, so production never reaches this mode.
- FAILED: a refusal. Sticky for the life of the process; a restart is needed.

Production (ensure_ready) has no runtime parameter and no switch, environment
variable or setting that can supply one. A hardened request therefore always
fails closed here, after every prerequisite has been checked and reported.
It never runs generated code through the V1 subprocess executor.

The secure deployment profile stays refused until the P2 guardrails exist:
a hardened backend alone does not make the application secure.

`streamlit run ui/app.py` never imports this module and is not protected by
it (see docs/v2/P3_SECURE_EXECUTION_DESIGN.md, deployment boundary).

P5.1A:
- IP_FF_ORCHESTRATOR=v2 (the P5 multi-agent orchestrator) is accepted only
  TOGETHER with hardened container execution; on its own it is still refused.
  It never relaxes any other prerequisite.
- hardened_dispatcher() gives the P5 ContainerExecutionPort the one process-
  wide dispatcher, or raises SandboxUnavailableError (fail closed) unless the
  mode is HARDENED and the binding is intact.
"""

import logging
import sys
import threading
from enum import Enum

from config.feature_flags import FeatureFlagError, FeatureFlags, flag_environment_variables, load_flags
from v2.execution.isolation import IsolationLevel

STAGE = "P3.2b"
P2_GUARDRAILS_IMPLEMENTED = False

# Streamlit settings the hardened launch path requires: no module reloading
# behind the bootstrap's back. Not checked for the V1-compatible path.
REQUIRED_STREAMLIT_OPTIONS = (("server.fileWatcherType", "none"), ("server.runOnSave", False))

_HARDENING_FIELDS = {"sandbox_hardened", "sandbox_backend", "deployment_profile"}
# Accepted only together with sandbox_hardened (P5.1A): the P5 orchestrator.
_REQUIRES_HARDENING_FIELDS = {"orchestrator"}
_log = logging.getLogger("insightpilot.v2.bootstrap")


class BootstrapRefused(RuntimeError):
    """The application must not run in this process."""


class Mode(str, Enum):
    UNINITIALIZED = "uninitialized"
    V1_COMPATIBLE = "v1_compatible"
    HARDENED = "hardened"
    FAILED = "failed"


class _State:
    def __init__(self):
        self.mode = Mode.UNINITIALIZED
        self.flags = None
        self.message = None
        self.runtime = None
        self.dispatcher = None
        self.init_count = 0
        self.repair_count = 0


_LOCK = threading.RLock()
_state = _State()


# ── Public status ────────────────────────────────────────────────────────────

def mode() -> Mode:
    with _LOCK:
        return _state.mode


def is_initialized() -> bool:
    return mode() is not Mode.UNINITIALIZED


def status() -> dict:
    with _LOCK:
        return {
            "mode": _state.mode.value,
            "init_count": _state.init_count,
            "repair_count": _state.repair_count,
            "runtime": getattr(_state.runtime, "name", None),
            "runtime_is_test_double": bool(getattr(_state.runtime, "is_test_double", False)),
            "message": _state.message,
        }


# ── Production entry point ───────────────────────────────────────────────────

def ensure_ready(env=None, options=None) -> Mode:
    """
    Called by the launcher on every script run. Initialises once, then checks
    on every run that nothing changed. Raises BootstrapRefused to stop the app.
    `env` defaults to os.environ; `options` defaults to Streamlit's config.
    """
    with _LOCK:
        if _state.mode is Mode.UNINITIALIZED:
            _state.init_count += 1
            _initialize_production(env, options)
        return _check_every_run(env, options)


def _initialize_production(env, options) -> None:
    flags = _load(env)
    _state.flags = flags
    if flags.is_v1_default:
        _state.mode = Mode.V1_COMPATIBLE
        return
    problems = _prerequisite_problems(flags, options, runtime=None)
    if not problems:   # defensive: there is no production hardened path in this build
        problems = ["no production execution path for V2 features exists in this build"]
    _fail(_refusal_text(flags, problems))


def _check_every_run(env, options) -> Mode:
    if _state.mode is Mode.FAILED:
        raise BootstrapRefused(
            f"{_state.message} The execution mode is fixed for this process; restart the application.")
    try:
        current = load_flags(env)
    except FeatureFlagError as exc:
        _fail(f"Invalid V2 feature-flag configuration (changed while running): {exc}")
    if current != _state.flags:
        now = ", ".join(_requested_names(current)) or "the V1 defaults"
        _fail("V2 feature flags changed while the process was running (now requesting "
              f"{now}). The execution mode never switches inside a process.")
    if _state.mode is Mode.V1_COMPATIBLE:
        return _state.mode

    # HARDENED: settings still hold, and the binding is still in place.
    option_problems = _streamlit_option_problems(options)
    if option_problems:
        _fail("Hardened execution prerequisites no longer hold: " + "; ".join(option_problems) + ".")
    from v2.execution import binding
    drift = binding.verify()
    if drift:
        binding.repair()
        _state.repair_count += 1
        _log.warning("V2 executor binding drifted and was restored: %s", "; ".join(drift))
        remaining = binding.verify()
        if remaining:
            _fail("The hardened executor binding could not be restored: " + "; ".join(remaining) + ".")
    return _state.mode


def hardened_dispatcher():
    """The process-wide hardened dispatcher; SandboxUnavailableError unless HARDENED and intact."""
    from v2.execution import binding
    from v2.execution.errors import SandboxUnavailableError
    with _LOCK:
        if _state.mode is not Mode.HARDENED or _state.dispatcher is None:
            raise SandboxUnavailableError(
                f"hardened execution is not active in this process (mode {_state.mode.value}); "
                "there is no unisolated fallback")
        drift = binding.verify()
        if drift:
            raise SandboxUnavailableError("the hardened executor binding is not intact: " + "; ".join(drift))
        return _state.dispatcher


# ── Test-only entry points ───────────────────────────────────────────────────

def _require_pytest() -> None:
    # Defence in depth only: these functions are reachable just from Python
    # code already running in this process; no setting can call them.
    if "pytest" not in sys.modules:
        raise RuntimeError("this bootstrap function is for the offline test suite only")


def install_for_tests(runtime, env=None, options=None):
    """
    Enter HARDENED mode with an explicitly injected test double. Runs the same
    prerequisite checks as production, plus the runtime's own preflight.
    Returns the dispatcher. Undo with _reset_for_tests().
    """
    _require_pytest()
    if getattr(runtime, "is_test_double", False) is not True:
        raise ValueError("install_for_tests accepts only runtimes that declare is_test_double = True")
    with _LOCK:
        if _state.mode is not Mode.UNINITIALIZED:
            raise RuntimeError("the bootstrap is already initialised; reset it first")
        _state.init_count += 1
        flags = _load(env)
        _state.flags = flags
        if not flags.sandbox_hardened:
            _fail("install_for_tests requires IP_FF_SANDBOX_HARDENED=1 and IP_SANDBOX_BACKEND=container")
        problems = _prerequisite_problems(flags, options, runtime=runtime)
        if problems:
            _fail(_refusal_text(flags, problems))

        from v2.execution import binding
        from v2.execution.dispatch import HardenedDispatcher
        dispatcher = HardenedDispatcher(runtime)
        binding.install(dispatcher)
        _state.mode = Mode.HARDENED
        _state.runtime = runtime
        _state.dispatcher = dispatcher
        return dispatcher


def _reset_for_tests() -> None:
    """Return to UNINITIALIZED and remove any binding. Tests only."""
    _require_pytest()
    global _state
    with _LOCK:
        from v2.execution import binding
        binding.uninstall()
        _state = _State()


# ── Internals ────────────────────────────────────────────────────────────────

def _load(env) -> FeatureFlags:
    try:
        return load_flags(env)
    except FeatureFlagError as exc:
        _fail(f"Invalid V2 feature-flag configuration: {exc}")


def _fail(message: str):
    _state.mode = Mode.FAILED
    _state.message = message
    _log.error("InsightPilot V2 bootstrap refused: %s", message)
    raise BootstrapRefused(message)


def _requested_names(flags: FeatureFlags) -> list:
    defaults = FeatureFlags()
    names = flag_environment_variables()
    return sorted(names[field] for field, value in flags.as_dict().items()
                  if value != getattr(defaults, field))


def _option(options, key):
    if options is not None:
        return options.get(key)
    try:
        from streamlit import config
        return config.get_option(key)
    except Exception as exc:   # cannot verify: treated as not satisfied
        return f"<unavailable: {type(exc).__name__}>"


def _streamlit_option_problems(options) -> list:
    problems = []
    for key, required in REQUIRED_STREAMLIT_OPTIONS:
        actual = _option(options, key)
        if actual != required:
            problems.append(f"Streamlit {key} must be {required!r} for hardened execution (is {actual!r})")
    return problems


def _prerequisite_problems(flags: FeatureFlags, options, runtime) -> list:
    problems = []
    defaults = FeatureFlags()
    names = flag_environment_variables()
    if flags.deployment_profile == "secure" and not P2_GUARDRAILS_IMPLEMENTED:
        problems.append("IP_DEPLOYMENT_PROFILE=secure needs the P2 guardrails, which are not "
                        "implemented; a hardened backend alone does not make the application secure")
    accepted = _HARDENING_FIELDS | (_REQUIRES_HARDENING_FIELDS if flags.sandbox_hardened else set())
    other = sorted(names[f] for f, v in flags.as_dict().items()
                   if f not in accepted and v != getattr(defaults, f))
    if other:
        problems.append("not integrated yet: " + ", ".join(other))
    for f in sorted(_REQUIRES_HARDENING_FIELDS):
        if getattr(flags, f) != getattr(defaults, f) and not flags.sandbox_hardened:
            problems.append(f"{names[f]} requires hardened container execution (IP_FF_SANDBOX_HARDENED=1)")
    if flags.sandbox_hardened:            # validate() guarantees backend == "container"
        problems.extend(_streamlit_option_problems(options))
        if runtime is None:
            problems.append(f"no validated container backend exists in this build ({STAGE}), "
                            "so hardened execution cannot start")
        else:
            if getattr(runtime, "isolation", None) is not IsolationLevel.ROOTLESS_CONTAINER:
                problems.append("the runtime does not declare rootless container isolation")
            try:
                runtime.preflight()
            except Exception as exc:
                problems.append(f"runtime preflight failed ({type(exc).__name__}: {exc})")
    return problems


def _refusal_text(flags: FeatureFlags, problems: list) -> str:
    requested = ", ".join(_requested_names(flags)) or "none"
    return ("V2 features were requested that this build cannot provide yet: "
            f"{requested}. Prerequisites not met: " + "; ".join(problems) + ". "
            "Hardened execution is not available, and the V1 executor is never used as a "
            "fallback. Unset these variables to run the V1-compatible app.")
