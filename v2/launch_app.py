"""
InsightPilot V2 launcher — P3.2a: flags-off compatibility boundary only.

    streamlit run v2/launch_app.py

- With every IP_* feature flag at its V1-compatible default, it runs the
  unchanged ui/app.py and behaves like `streamlit run ui/app.py`.
- Any other configuration (hardened execution, the secure profile, or any
  V2 feature that is not integrated yet) stops BEFORE ui/app.py executes.
  The V1 executor is never used as a fallback for a V2 request.
- Invalid or unknown flags (strict parsing) also stop it.

It does not rebind or replace functions, start processes, run sandboxes or
load secrets.

`streamlit run ui/app.py` bypasses this launcher completely and ignores every
IP_* variable. See docs/v2/P3_SECURE_EXECUTION_DESIGN.md.
"""

import os
import runpy
import sys

LAUNCHER_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(LAUNCHER_DIR)
UI_DIR = os.path.join(ROOT, "ui")
APP_PATH = os.path.join(UI_DIR, "app.py")

SUPPORTED_STAGE = "P3.2a"


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


def check_flags(env=None):
    """
    Return the flags if, and only if, they are all V1-compatible defaults.
    Raise LaunchRefused otherwise. `env` defaults to os.environ.
    """
    from config.feature_flags import (
        FeatureFlagError, FeatureFlags, flag_environment_variables, load_flags,
    )

    try:
        flags = load_flags(env)
    except FeatureFlagError as exc:
        raise LaunchRefused(f"Invalid V2 feature-flag configuration: {exc}") from None

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


def main() -> None:
    prepare_sys_path()
    try:
        check_flags()
    except LaunchRefused as exc:
        import streamlit as st
        print(f"InsightPilot V2 launcher refused to start: {exc}", file=sys.stderr)
        st.error(f"InsightPilot did not start. {exc}")
        st.stop()
    run_app()


if __name__ == "__main__":
    main()
