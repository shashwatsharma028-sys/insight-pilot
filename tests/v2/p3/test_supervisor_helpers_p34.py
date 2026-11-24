"""
P3.4: pure supervisor/runner helpers, and the no-execution guarantees of
every P3.4 module.

Nothing here starts a process, installs a signal handler, sends a signal or
executes generated code; the helpers are fed literal data.
"""

import ast
import os
import random
import signal
import stat
import subprocess
import sys

import pytest

from v2.execution import frame, supervisor_helpers as sh
from v2.execution.errors import SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RID = "0123456789abcdef" * 2
P34_MODULES = ["v2/execution/frame.py", "v2/execution/artifacts.py", "v2/execution/chart_store.py",
               "v2/execution/supervisor_helpers.py"]

GOOD_STATUS = """Name:\tpython
Uid:\t10001\t10001\t10001\t10001
Gid:\t10001\t10001\t10001\t10001
CapInh:\t0000000000000000
CapPrm:\t0000000000000000
CapEff:\t0000000000000000
CapBnd:\t0000000000000000
NoNewPrivs:\t1
Seccomp:\t2
"""
GOOD_MOUNTINFO = """22 1 0:21 / / ro,relatime - overlay overlay ro
23 22 0:22 / /proc rw,nosuid - proc proc rw
24 22 0:23 / /tmp rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=65536k
25 22 0:24 / /out rw,nosuid,nodev,noexec - tmpfs tmpfs rw,size=32768k
26 22 0:25 / /in ro,relatime - ext4 /dev/sda1 ro
27 22 0:26 / /sys ro,nosuid - sysfs sysfs ro
28 22 0:27 / /dev ro,nosuid - tmpfs tmpfs rw,size=65536k,mode=755
29 28 0:5 /null /dev/null rw,nosuid,noexec,relatime - devtmpfs udev rw
30 28 0:5 /zero /dev/zero rw,nosuid,noexec,relatime - devtmpfs udev rw
31 28 0:5 /full /dev/full rw,nosuid,noexec,relatime - devtmpfs udev rw
32 28 0:5 /random /dev/random rw,nosuid,noexec,relatime - devtmpfs udev rw
33 28 0:5 /urandom /dev/urandom rw,nosuid,noexec,relatime - devtmpfs udev rw
34 28 0:5 /tty /dev/tty rw,nosuid,noexec,relatime - devtmpfs udev rw
35 28 0:28 / /dev/pts rw,nosuid,noexec,relatime - devpts devpts rw,gid=5,mode=620,ptmxmode=666
36 28 0:29 / /dev/mqueue rw,nosuid,nodev,noexec,relatime - mqueue mqueue rw
"""                    # /dev lines as rootless Podman 4.9.3 mounts them (CI run 4 and run 45341414519)


def _good_attestation(**over):
    args = dict(proc_status=GOOD_STATUS, mountinfo=GOOD_MOUNTINFO, net_interfaces=["lo"], dev_writable=False,
                memory_max="1073741824\n", memory_swap_max="0\n", pids_max="64\n", cpu_max="100000 100000\n")
    args.update(over)
    return sh.build_attestation(**args)


# ── collectors ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("size,truncated", [(0, False), (9, False), (10, False), (11, True), (1000, True)])
def test_bounded_collector_boundaries(size, truncated):
    c = sh.BoundedCollector(10)
    data = bytes(range(256))[:size] if size <= 256 else b"x" * size
    for i in range(0, len(data), 3):
        c.feed(data[i:i + 3])
    assert c.truncated is truncated and c.total == size
    assert c.value() == data[:10]


@pytest.mark.parametrize("size", [0, 5, 13, 14, 15, 16, 100, 5000])
def test_head_tail_collector_keeps_both_ends(size):
    head, tail = 8, 6
    data = bytes((i * 7) % 251 for i in range(size))
    rng = random.Random(size)
    c, i = sh.HeadTailCollector(head, tail), 0
    while i < len(data):
        n = rng.randint(1, 9)
        c.feed(data[i:i + n])
        i += n
    if size <= head + tail:
        assert not c.truncated and c.value() == data
    else:
        assert c.truncated and c.value() == data[:head] + data[-tail:]
        assert len(c.value()) == head + tail


def test_collectors_at_the_real_caps_produce_valid_frames():
    out = sh.BoundedCollector(DEFAULT_LIMITS.stdout_bytes)
    err = sh.HeadTailCollector(DEFAULT_LIMITS.stderr_head_bytes, DEFAULT_LIMITS.stderr_tail_bytes)
    out.feed(b"o" * (DEFAULT_LIMITS.stdout_bytes + 1))
    err.feed(b"e" * (DEFAULT_LIMITS.stderr_bytes + 5) + b"\nKeyError: 'x'\n")
    data = sh.assemble_frame(run_id=RID, wait_status=1 << 8, timed_out=False, oom_kill_events=0,
                             stdout=out, stderr=err, attestation=frame.expected_attestation())
    f = frame.decode_frame_bytes(data, RID, expected_attestation=frame.expected_attestation())
    assert f.stdout_truncated and len(f.stdout) == DEFAULT_LIMITS.stdout_bytes
    assert f.stderr_truncated and f.stderr.endswith(b"KeyError: 'x'\n")   # the exception line survives
    assert f.child_exit == 1


# ── signal plan (data only) ──────────────────────────────────────────────────

def test_signal_plan_defaults_everything_except_sigchld_and_sigpipe():
    before = signal.getsignal(signal.SIGINT)
    plan = sh.signal_plan()
    assert signal.getsignal(signal.SIGINT) is before                       # nothing installed
    assert plan["SIGCHLD"] == "handle" and plan["SIGPIPE"] == "ignore"
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGQUIT", "SIGALRM"):
        assert plan[name] == "default", name
    assert "SIGKILL" not in plan and "SIGSTOP" not in plan
    assert set(plan.values()) == {"handle", "ignore", "default"}


# ── wait status ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,expected", [(0, (0, None)), (3 << 8, (3, None)), (255 << 8, (255, None)),
                                             (9, (None, 9)), (15, (None, 15)), (9 | 0x80, (None, 9))])
def test_termination_from_wait_status(status, expected):
    assert sh.termination_from_wait_status(status) == expected


def test_a_stopped_child_is_not_a_termination():
    with pytest.raises(ValueError):
        sh.termination_from_wait_status((19 << 8) | 0x7F)                   # WIFSTOPPED


# ── attestation parsing ──────────────────────────────────────────────────────

def test_good_attestation_matches_the_expected_isolation():
    att = _good_attestation()
    frame.verify_attestation(att, frame.expected_attestation())
    assert att["writable_mounts"] == ["/dev/full", "/dev/mqueue", "/dev/null", "/dev/pts", "/dev/random", "/dev/tty",
                                      "/dev/urandom", "/dev/zero", "/out", "/tmp"]        # P3.6-4B D2: exact set


@pytest.mark.parametrize("over,key", [
    (dict(proc_status=GOOD_STATUS.replace("CapEff:\t0000000000000000", "CapEff:\t000001ffffffffff")), "cap_eff"),
    (dict(proc_status=GOOD_STATUS.replace("Uid:\t10001\t10001", "Uid:\t0\t0")), "uid"),
    (dict(proc_status=GOOD_STATUS.replace("Seccomp:\t2", "Seccomp:\t0")), "seccomp"),
    (dict(mountinfo=GOOD_MOUNTINFO.replace("/ / ro,relatime", "/ / rw,relatime")), "writable_mounts"),
    (dict(net_interfaces=["lo", "eth0"]), "net_interfaces"),
    (dict(dev_writable=True), "dev_writable"),
    (dict(memory_max="max"), "memory_max"),
    (dict(pids_max="max"), "pids_max"),
    (dict(cpu_max="max 100000"), "cpu_max"),
])
def test_weakened_isolation_is_detected(over, key):
    with pytest.raises(SandboxUnavailableError, match=key):
        frame.verify_attestation(_good_attestation(**over), frame.expected_attestation())


@pytest.mark.parametrize("bad", ["", "Uid: x\n", GOOD_STATUS.replace("NoNewPrivs:\t1\n", "")])
def test_unrecognised_proc_status_is_refused(bad):
    with pytest.raises(ValueError):
        sh.parse_proc_status(bad)


def test_cgroup_limits_are_parsed_strictly():
    assert sh.parse_cgroup_limit("max\n") == "max" and sh.parse_cgroup_limit("64") == 64
    for bad in ("-1", "1.5", "64k", ""):
        with pytest.raises(ValueError):
            sh.parse_cgroup_limit(bad)


# ── /out entries ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode,nlink,size,reason", [
    (stat.S_IFREG | 0o644, 1, 100, None),
    (stat.S_IFLNK | 0o777, 1, 10, "symlink"),
    (stat.S_IFDIR | 0o755, 2, 0, "not_regular_file"),
    (stat.S_IFIFO | 0o644, 1, 0, "not_regular_file"),
    (stat.S_IFSOCK | 0o644, 1, 0, "not_regular_file"),
    (stat.S_IFCHR | 0o644, 1, 0, "not_regular_file"),
    (stat.S_IFREG | 0o644, 2, 100, "hardlink"),
    (stat.S_IFREG | 0o644, 1, 0, "empty"),
    (stat.S_IFREG | 0o644, 1, DEFAULT_LIMITS.chart_bytes + 1, "too_large"),
])
def test_out_entry_classification(mode, nlink, size, reason):
    assert sh.classify_out_entry(mode, nlink, size) == reason


# ── frame assembly ───────────────────────────────────────────────────────────

def _assemble(**kw):
    args = dict(run_id=RID, wait_status=0, timed_out=False, oom_kill_events=0, stdout=sh.BoundedCollector(100),
                stderr=sh.HeadTailCollector(10, 10), chart=b"chart", attestation=frame.expected_attestation())
    args.update(kw)
    return frame.decode_frame_bytes(sh.assemble_frame(**args), RID)


def test_successful_run_keeps_its_chart():
    f = _assemble()
    assert f.succeeded and f.chart == b"chart"


@pytest.mark.parametrize("kw,expect", [
    (dict(wait_status=1 << 8), dict(child_exit=1, timed_out=False, oom_killed=False)),
    (dict(wait_status=9, timed_out=True), dict(child_signal=9, timed_out=True, oom_killed=False)),
    (dict(wait_status=9, oom_kill_events=1), dict(child_signal=9, timed_out=False, oom_killed=True)),
    (dict(wait_status=9, timed_out=True, oom_kill_events=1), dict(child_signal=9, timed_out=True, oom_killed=False)),
    (dict(wait_status=15, oom_kill_events=1), dict(child_signal=15, oom_killed=False)),
])
def test_failed_runs_never_carry_a_chart(kw, expect):
    f = _assemble(chart_reject_reason=None, **kw)
    for key, value in expect.items():
        assert getattr(f, key) == value, key
    assert f.chart is None and f.chart_reject_reason is None


# ── no execution, processes, signals, network or containers ─────────────────

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_ATTRS = {"kill", "killpg", "fork", "forkpty", "execv", "execve", "execvp", "execl", "execlp",
                   "spawnv", "spawnl", "posix_spawn", "system", "popen", "Popen", "run", "check_output",
                   "call", "setsid", "waitpid", "wait", "prctl", "setuid", "setgid", "chroot",
                   "connect", "bind", "listen", "urlopen"}
FORBIDDEN_IMPORTS = {"subprocess", "socket", "ctypes", "multiprocessing", "pickle", "marshal", "shelve",
                     "importlib", "runpy", "http", "urllib", "pty", "select", "asyncio", "docker", "podman"}


@pytest.mark.parametrize("rel", P34_MODULES)
def test_p34_modules_cannot_execute_spawn_signal_or_connect(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                problems.append(node.func.id)
            if isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_ATTRS:
                problems.append(node.func.attr)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "signal" \
                    and getattr(node.func.value, "id", None) == "signal":
                problems.append("signal.signal")
        if isinstance(node, ast.Import):
            problems += [a.name for a in node.names if a.name.split(".")[0] in FORBIDDEN_IMPORTS]
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
            problems.append(node.module)
    assert problems == [], f"{rel}: {problems}"


def test_importing_p34_modules_loads_no_process_network_or_v1_modules():
    code = ("import sys; sys.path.insert(0, %r); before = set(sys.modules)\n"
            "import v2.execution.frame, v2.execution.artifacts, v2.execution.chart_store, "
            "v2.execution.supervisor_helpers\n"
            "print(','.join(sorted({m.split('.')[0] for m in set(sys.modules) - before})))") % ROOT
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    loaded = set(out.stdout.strip().split(","))
    assert not loaded & {"subprocess", "socket", "ssl", "http", "urllib", "multiprocessing", "ctypes",
                         "agent", "sandbox", "utils", "ui", "streamlit", "langchain_core", "PIL"}, loaded


def test_p34_modules_are_not_wired_into_production():
    for rel in ["v2/launch_app.py", "v2/execution/bootstrap.py", "v2/execution/binding.py",
                "v2/execution/dispatch.py", "v2/execution/runtime.py", "v2/execution/__init__.py"]:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        # P3.6-1 (approved, narrow): the dispatcher may import ONLY the pure naming function
        # chart_store.chart_filename, to check that a chart path is the store-published name.
        allowed = {("v2/execution/dispatch.py", "v2.execution.chart_store"): {"chart_filename"}}
        modules = {n.module for n in imports if (rel, n.module) not in allowed}
        names = {a.name for n in imports for a in n.names}
        assert not modules & {"v2.execution.frame", "v2.execution.artifacts", "v2.execution.chart_store",
                              "v2.execution.supervisor_helpers"}, rel
        assert not names & {"frame", "artifacts", "chart_store", "supervisor_helpers", "ChartStore",
                            "reencode_png", "decode_frame"}, rel
        for n in imports:
            if (rel, n.module) in allowed:
                assert {a.name for a in n.names} <= allowed[(rel, n.module)], rel


def test_production_hardened_execution_still_fails_closed():
    from v2.execution import bootstrap
    with pytest.raises(bootstrap.BootstrapRefused, match="no validated container backend"):
        bootstrap.ensure_ready(env={"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"},
                               options={"server.fileWatcherType": "none", "server.runOnSave": False})


def test_execution_contracts_are_unchanged():
    import dataclasses
    from v2.execution.results import ExecutionResult
    from v2.execution.runtime import ExecutionRequest
    # P5.1A (approved) adds the path-free dataset identity and the trusted-prelude selector.
    # The pin stays exact: any further change must update this list deliberately.
    assert [f.name for f in dataclasses.fields(ExecutionRequest)] == [
        "run_id", "task_id", "code", "limits", "dataset", "prelude"]
    assert [f.name for f in dataclasses.fields(ExecutionResult)] == [
        "run_id", "isolation", "outcome", "stdout", "stderr", "stdout_truncated", "stderr_truncated",
        "chart_path", "chart_rejected_reason", "duration_ms"]
