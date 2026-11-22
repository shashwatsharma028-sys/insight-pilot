"""
Offline, isolated execution of one benchmark run (no pytest needed).

Inside offline_run():
- GOOGLE_API_KEY is removed, and the real Gemini chat class cannot be
  constructed: it raises OfflineViolation;
- in-process sockets may only connect to loopback or UNIX sockets (the same
  guard as tests/v2/conftest.py; the sandbox subprocess is not covered, as
  documented there);
- charts, reports and lessons go to the run's own directory, never to the
  repository's reports/ or lessons/;
- every module that imported utils.llm.get_llm gets the scripted model,
  wrapped so that every call is recorded with token provenance;
- every sandbox execution (agent.nodes.executor.execute_code) is timed.

Everything is restored on exit, in reverse order, even after an exception.
"""

import ipaddress
import os
import socket
import sys
import time
from contextlib import contextmanager

from evaluation import provenance

_MISSING = object()


class OfflineViolation(RuntimeError):
    pass


class Patcher:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        old = getattr(obj, name)
        self._undo.append(lambda: setattr(obj, name, old))
        setattr(obj, name, value)

    def setenv(self, key, value):
        old = os.environ.get(key, _MISSING)
        self._undo.append(lambda: os.environ.__setitem__(key, old) if old is not _MISSING
                          else os.environ.pop(key, None))
        os.environ[key] = value

    def delenv(self, key):
        old = os.environ.get(key, _MISSING)
        if old is not _MISSING:
            self._undo.append(lambda: os.environ.__setitem__(key, old))
            del os.environ[key]

    def undo(self):
        while self._undo:
            self._undo.pop()()


def _is_loopback(address):
    host = address[0] if isinstance(address, tuple) else address
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ProvenanceChat:
    """Provider-level stand-in: delegates to the scripted chat and records each call."""

    def __init__(self, inner, skill, calls, call_source, clock):
        self._inner, self._skill, self._calls, self._source, self._clock = inner, skill, calls, call_source, clock

    def invoke(self, *args, **kwargs):
        t0 = self._clock()
        response = self._inner.invoke(*args, **kwargs)
        self._calls.append(provenance.call_record(self._skill, self._source, args, response, self._clock() - t0))
        return response


class Controller:
    def __init__(self, scenario, calls, clock):
        self.scenario, self.calls, self._clock = scenario, calls, clock
        self.mock_calls = []            # the scripted chat's own log (skill, chars)

    def get_llm(self, temperature: float = 0.1, skill: str = "unknown"):
        from tests.v2.mock_llm import ScriptedChat
        from utils.llm import RateLimitedLLM
        chat = ProvenanceChat(ScriptedChat(skill, self.scenario, self.mock_calls), skill, self.calls,
                              provenance.MOCK, self._clock)
        return RateLimitedLLM(chat, skill=skill)

    def calls_by_skill(self):
        out = {}
        for c in self.calls:
            out[c["skill"]] = out.get(c["skill"], 0) + 1
        return dict(sorted(out.items()))


@contextmanager
def offline_run(scenario, workdir, clock=time.perf_counter):
    """Yield (controller, executions, dirs) for one isolated run; restores everything on exit."""
    import agent.graph  # noqa: F401  (imports every node module)
    import agent.memory.conversation  # noqa: F401
    import agent.nodes.executor as executor_node
    import agent.nodes.report_generator as report_generator
    import sandbox.executor
    import utils.llm
    from agent.token_monitor import monitor

    dirs = {"root": workdir, "reports": os.path.join(workdir, "reports"),
            "charts": os.path.join(workdir, "reports", "charts"), "lessons": os.path.join(workdir, "lessons")}
    os.makedirs(dirs["charts"], exist_ok=True)
    os.makedirs(dirs["lessons"], exist_ok=True)
    p = Patcher()
    calls, executions = [], []
    controller = Controller(scenario, calls, clock)
    try:
        p.delenv("GOOGLE_API_KEY")

        class _Blocked:
            def __init__(self, *a, **k):
                raise OfflineViolation("real LLM client constructed during an offline benchmark")
        p.setattr(utils.llm, "ChatGoogleGenerativeAI", _Blocked)

        real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

        def _allowed(sock, address):
            return sock.family == getattr(socket, "AF_UNIX", None) or _is_loopback(address)

        def connect(sock, address):
            if not _allowed(sock, address):
                raise OfflineViolation(f"network connection blocked: {address!r}")
            return real_connect(sock, address)

        def connect_ex(sock, address):
            if not _allowed(sock, address):
                raise OfflineViolation(f"network connection blocked: {address!r}")
            return real_connect_ex(sock, address)
        p.setattr(socket.socket, "connect", connect)
        p.setattr(socket.socket, "connect_ex", connect_ex)

        p.setattr(sandbox.executor, "CHARTS_DIR", dirs["charts"])
        p.setattr(report_generator, "REPORTS_DIR", dirs["reports"])
        p.setenv("LESSONS_DIR", dirs["lessons"])

        original_get_llm = utils.llm.get_llm
        patched = [name for name, m in list(sys.modules.items())
                   if m is not None and getattr(m, "get_llm", None) is original_get_llm]
        if "agent.nodes.planner" not in patched:
            raise RuntimeError(f"scripted model could not be installed: {patched}")
        for name in patched:
            p.setattr(sys.modules[name], "get_llm", controller.get_llm)

        real_execute = executor_node.execute_code

        def timed_execute(*args, **kwargs):
            t0 = clock()
            result = real_execute(*args, **kwargs)
            success, output, chart_path, error_type = result
            executions.append({
                "task_id": kwargs.get("task_id", args[2] if len(args) > 2 else None),
                "seconds": clock() - t0, "success": bool(success),
                "blocked_by_safety_scan": (not success) and str(output).startswith("SANDBOX SAFETY VIOLATION"),
                "error_type": getattr(error_type, "value", error_type), "chart": bool(chart_path)})
            return result
        p.setattr(executor_node, "execute_code", timed_execute)

        monitor.reset()
        yield controller, executions, dirs
    finally:
        p.undo()
        monitor.reset()
