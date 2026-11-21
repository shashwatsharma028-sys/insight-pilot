"""
Fixtures for the P3 tests.

- v2_global_state_guard (autouse, from leak_guard): every P3 test starts and
  ends with the original V1 executor, no V2 binding and an uninitialised V2
  bootstrap; any leak is repaired and fails the test.
- hardened_test_mode: HARDENED bootstrap mode with an explicitly injected
  FakeRuntime (never executes code). Torn down before the guard runs.
"""

from types import SimpleNamespace

import pytest

from tests.v2.p3.fakes import FakeRuntime
from tests.v2.p3.leak_guard import v2_global_state_guard  # noqa: F401  (autouse fixture)

HARDENED_ENV = {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"}
HARDENED_OPTIONS = {"server.fileWatcherType": "none", "server.runOnSave": False}


@pytest.fixture
def hardened_streamlit_options():
    """Set the hardened launch path's Streamlit options; restore afterwards."""
    from streamlit import config
    original = {key: config.get_option(key) for key in HARDENED_OPTIONS}
    for key, value in HARDENED_OPTIONS.items():
        config.set_option(key, value)
    yield dict(HARDENED_OPTIONS)
    for key, value in original.items():
        config.set_option(key, value)


@pytest.fixture
def hardened_env(monkeypatch):
    for key, value in HARDENED_ENV.items():
        monkeypatch.setenv(key, value)
    return dict(HARDENED_ENV)


@pytest.fixture
def hardened_test_mode(hardened_env, hardened_streamlit_options):
    from v2.execution import binding, bootstrap
    runtime = FakeRuntime()
    dispatcher = bootstrap.install_for_tests(runtime)
    yield SimpleNamespace(runtime=runtime, dispatcher=dispatcher, bootstrap=bootstrap, binding=binding)
    bootstrap._reset_for_tests()
