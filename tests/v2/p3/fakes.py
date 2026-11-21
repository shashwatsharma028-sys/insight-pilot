"""
Offline test doubles for the hardened execution path (P3.2b).

FakeRuntime NEVER executes the code it receives. It records the request and
returns a harmless, scripted ExecutionResult. It lives in tests/ only, and
declares is_test_double = True so that only bootstrap.install_for_tests()
accepts it; production code contains no fake runtime at all.
"""

import threading
import time

from v2.execution.isolation import IsolationLevel
from v2.execution.results import ExecutionOutcome, ExecutionResult

FAKE_MARKER = "FAKE-RUNTIME"


def fake_success(request, stdout=None):
    return ExecutionResult(
        run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
        outcome=ExecutionOutcome.SUCCEEDED,
        stdout=stdout if stdout is not None else
        f"{FAKE_MARKER} task={request.task_id} run={request.run_id}: code was not executed",
        stderr="")


class FakeRuntime:
    name = "offline-fake-runtime"
    isolation = IsolationLevel.ROOTLESS_CONTAINER
    is_test_double = True

    def __init__(self, responder=None, preflight_error=None, delay=0.0):
        self.responder = responder
        self.preflight_error = preflight_error
        self.delay = delay
        self.requests = []
        self.threads = []
        self.preflight_calls = 0
        self._lock = threading.Lock()

    def preflight(self):
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    def execute(self, request):
        with self._lock:
            self.requests.append(request)
            self.threads.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.responder is not None:
            return self.responder(request)
        return fake_success(request)
