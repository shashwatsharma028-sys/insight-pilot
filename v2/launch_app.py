"""
InsightPilot V2 launcher.

    streamlit run v2/launch_app.py
    # hardened launch path (refused in this build: no validated backend yet):
    streamlit run v2/launch_app.py --server.fileWatcherType none --server.runOnSave false

- With every IP_* flag at its V1-compatible default (and no V2 bootstrap
  initialised in this process) it runs the unchanged ui/app.py exactly as in
  P3.2a: nothing V2 is imported, nothing is bound.
- Any other configuration is decided by the V2 bootstrap
  (v2/execution/bootstrap.py), once per process. In this build that always
  ends in a refusal BEFORE ui/app.py executes, after all hardened
  prerequisites have been checked. The V1 executor is never a fallback.
- Invalid or unknown flags (strict parsing) stop it.

It does not start processes, run sandboxes or load secrets.

`streamlit run ui/app.py` bypasses this launcher and the bootstrap completely,
ignores every IP_* variable, and is an unprotected legacy application. See
docs/v2/P3_SECURE_EXECUTION_DESIGN.md (deployment boundary).
"""

import os
import runpy
import sys

LAUNCHER_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LAUNCHER_DIR)
UI_DIR = os.path.join(ROOT, "ui")
APP_PATH = os.path.join(UI_DIR, "app.py")

SUPPORTED_STAGE = "P3.2b"
_BOOTSTRAP_MODULE = "v2.execution.bootstrap"


class LaunchRefused(Exception):
    """The launcher will not run the application for this configuration."""


def prepare_sys_path(path=None) -> None:
    """
    Make import resolution match `streamlit run ui/app.py`.

    `streamlit run X` puts X's folder first on sys.path. For the launcher that
    is v2/; for the original command it is ui/. Replace the former with the
    latter, and make the repository root importable for config.feature_flags.
    Idempotent: reruns do not grow sys.path.
    """
    path = sys.path if path is None else path
    path[:] = [p for p in path if not (p and os.path.abspath(p) == LAUNCHER_DIR)]
    if UI_DIR not in path:
        path.insert(0, UI_DIR)
    if ROOT not in path:
        path.insert(0, ROOT)


def _strict_flags(env=None):
    from config.feature_flags import FeatureFlagError, load_flags
    try:
        return load_flags(env)
    except FeatureFlagError as exc:
        raise LaunchRefused(f"Invalid V2 feature-flag configuration: {exc}") from None


def check_flags(env=None):
    """
    Production capability check: return the flags if, and only if, they are
    all V1-compatible defaults (the only configuration this build can run in
    production). Raise LaunchRefused otherwise. `env` defaults to os.environ.
    """
    from config.feature_flags import FeatureFlags, flag_environment_variables

    flags = _strict_flags(env)
    if not flags.is_v1_default:
        defaults = FeatureFlags()
        names = flag_environment_variables()
        requested = sorted(names[field] for field, value in flags.as_dict().items()
                           if value != getattr(defaults, field))
        raise LaunchRefused(
            "V2 features were requested that this build cannot provide yet: "
            + ", ".join(requested)
            + f". The {SUPPORTED_STAGE} launcher runs only the V1-compatible defaults. "
            "Hardened execution is not available, and the V1 executor is never "
            "used as a fallback. Unset these variables to run the V1-compatible app.")
    return flags


def run_app(app_path: str = APP_PATH) -> None:
    """Execute the application script the way Streamlit executes a main script."""
    runpy.run_path(app_path, run_name="__main__")


def admit() -> None:
    """
    Decide whether ui/app.py may run in this script run; raise LaunchRefused
    if not. Default flags with no initialised V2 bootstrap take the P3.2a path
    (no V2 module is imported). Otherwise the bootstrap decides, and once it
    is initialised it stays authoritative for the whole process, so the
    execution mode never switches between sessions or reruns.
    """
    boot = sys.modules.get(_BOOTSTRAP_MODULE)
    if boot is None or not boot.is_initialized():
        if _strict_flags().is_v1_default:
            return
        from v2.execution import bootstrap as boot
    try:
        boot.ensure_ready()
    except boot.BootstrapRefused as exc:
        raise LaunchRefused(str(exc)) from None


def main() -> None:
    prepare_sys_path()
    try:
        admit()
    except LaunchRefused as exc:
        import streamlit as st
        print(f"InsightPilot V2 launcher refused to start: {exc}", file=sys.stderr)
        st.error(f"InsightPilot did not start. {exc}")
        st.stop()
    run_app()


if __name__ == "__main__":
    main()
