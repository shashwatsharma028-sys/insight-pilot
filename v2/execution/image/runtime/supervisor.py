"""
InsightPilot sandbox supervisor (P3.6-2): PID 1 inside the container. Trusted image code.

Design §8.4 (docs/v2/P3_SECURE_EXECUTION_DESIGN.md). Its argv is produced only by
v2/execution/podman_argv.run_argv and holds integers, a run id and two enums; anything else
exits 78 WITHOUT a frame, which the host treats as a protocol error (fail closed).

    1. install the signal plan (every catchable signal default except SIGCHLD handled,
       SIGPIPE ignored), then PR_SET_DUMPABLE=0;
    2. record the attestation (proc status, mountinfo, interfaces, /dev, cgroup limits);
    3. start the child (runner, or a trusted self-test) with stdin /dev/null, its own pipes,
       a new session and a minimal environment;
    4. drain both pipes into bounded collectors until the child is done or the wall deadline;
    5. kill(-1, SIGKILL), reap until ECHILD, and require that /proc lists only PID 1;
       otherwise exit 70 without a frame;
    6. stage A for /out/chart.png (lstat, O_NOFOLLOW read, fstat equality, Pillow
       canonicalisation with no metadata) for a successful child only;
    7. write ONE IPX1 frame to fd 1 and exit 0.

Every operating-system action goes through an `os_api` object (RealOS below), so the control
flow is tested offline with a fake that executes nothing. Whether the real behaviour holds in a
container (signals, kill(-1), cgroup files, mounts) REQUIRES RUNTIME VALIDATION (VE-1/VE-2).
Nothing happens on import; main() runs only as __main__.
"""

import io
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)            # in the image /opt/ip holds v2/execution/{frame,supervisor_helpers,...}

from v2.execution import supervisor_helpers as sh          # noqa: E402
from v2.execution.limits import Limits                      # noqa: E402
from v2.execution.results import is_valid_run_id           # noqa: E402

EXIT_USAGE = 78
EXIT_UNSAFE_STATE = 70
MODES = ("run", "attest-only", "selftest-oom", "selftest-pids")
PRELUDES = ("v1", "lazy_seaborn")
PYTHON = "/usr/local/bin/python3"
RUNNER = "/opt/ip/runner.py"
SELFTEST = "/opt/ip/selftest.py"
CHART = "/out/chart.png"
CHILD_ENV = {"MPLBACKEND": "Agg", "MPLCONFIGDIR": "/tmp/matplotlib", "HOME": "/nonexistent", "LANG": "C.UTF-8",
             "TZ": "UTC", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
             "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PATH": "/usr/local/bin:/usr/bin:/bin"}
LIMIT_FLAGS = {"--wall": "code_wall_seconds", "--stdout-cap": "stdout_bytes", "--stderr-head": "stderr_head_bytes",
               "--stderr-tail": "stderr_tail_bytes", "--chart-cap": "chart_bytes", "--max-side": "chart_max_side",
               "--max-pixels": "chart_max_pixels", "--memory-max": "memory_bytes", "--pids-max": "pids"}
_INT = re.compile(r"[1-9][0-9]{0,11}")
READ_CHUNK = 65536


class UsageError(ValueError):
    pass


def parse_args(argv):
    """(mode, run_id, prelude, Limits). Every flag exactly once; integers only; no free text."""
    if len(argv) % 2:
        raise UsageError("arguments must be flag/value pairs")
    pairs = {}
    for flag, value in zip(argv[0::2], argv[1::2]):
        if flag in pairs:
            raise UsageError(f"duplicate {flag}")
        pairs[flag] = value
    required = {"--mode", "--run-id", "--prelude"} | set(LIMIT_FLAGS)
    if set(pairs) != required:
        raise UsageError(f"expected exactly {sorted(required)}")
    if pairs["--mode"] not in MODES or pairs["--prelude"] not in PRELUDES or not is_valid_run_id(pairs["--run-id"]):
        raise UsageError("bad mode, prelude or run id")
    values = {}
    for flag, field in LIMIT_FLAGS.items():
        if not _INT.fullmatch(pairs[flag]):
            raise UsageError(f"{flag} must be a positive integer")
        values[field] = int(pairs[flag])
    try:
        limits = Limits(**values)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    return pairs["--mode"], pairs["--run-id"], pairs["--prelude"], limits


def child_argv(mode, prelude, limits):
    if mode == "run":
        return [PYTHON, "-I", "-B", RUNNER, "--prelude", prelude]
    if mode == "selftest-oom":
        return [PYTHON, "-I", "-B", SELFTEST, "oom", str(limits.memory_bytes)]
    if mode == "selftest-pids":
        return [PYTHON, "-I", "-B", SELFTEST, "pids", str(limits.pids)]
    raise UsageError("mode has no child")


def collect_attestation(os_api):
    cg = "/sys/fs/cgroup/"
    return sh.build_attestation(
        proc_status=os_api.read_text("/proc/self/status"), mountinfo=os_api.read_text("/proc/self/mountinfo"),
        net_interfaces=os_api.listdir("/sys/class/net"), dev_writable=os_api.dev_writable(),
        memory_max=os_api.read_text(cg + "memory.max"), memory_swap_max=os_api.read_text(cg + "memory.swap.max"),
        pids_max=os_api.read_text(cg + "pids.max"), cpu_max=os_api.read_text(cg + "cpu.max"))


def oom_kill_events(os_api):
    try:
        text = os_api.read_text("/sys/fs/cgroup/memory.events")
    except OSError:
        return 0
    m = re.search(r"^oom_kill (\d+)$", text, re.M)
    return int(m.group(1)) if m else 0


# ── stage A (in the container, after every untrusted process is dead) ─────────

def canonicalize_png(data, limits):
    """(png_bytes, None) re-encoded without metadata, or (None, reason). Pillow restricted to PNG."""
    import warnings
    from PIL import Image
    saved_max = Image.MAX_IMAGE_PIXELS            # process-global in Pillow: always restored
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            Image.MAX_IMAGE_PIXELS = limits.chart_max_pixels
            with Image.open(io.BytesIO(data), formats=("PNG",)) as img:
                img.verify()
            with Image.open(io.BytesIO(data), formats=("PNG",)) as img:
                img.load()
                w, h = img.size
                if not (1 <= w <= limits.chart_max_side and 1 <= h <= limits.chart_max_side) \
                        or w * h > limits.chart_max_pixels:
                    return None, "dimensions_exceeded"
                fresh = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            out = io.BytesIO()
            fresh.save(out, format="PNG", optimize=False, compress_level=6)   # no pnginfo, no dpi
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None, "dimensions_exceeded"
    except Exception:
        return None, "decode_failed"
    finally:
        Image.MAX_IMAGE_PIXELS = saved_max
    png = out.getvalue()
    if len(png) > limits.chart_bytes:
        return None, "too_large"
    return png, None


def stage_a(os_api, limits):
    """(chart bytes or None, reason or None, number of other /out entries)."""
    entries = os_api.listdir("/out")
    others = len([e for e in entries if e != "chart.png"])
    if "chart.png" not in entries:
        return None, None, others
    st = os_api.lstat(CHART)
    reason = sh.classify_out_entry(st.st_mode, st.st_nlink, st.st_size, limits)
    if reason:
        return None, reason, others
    data, st2 = os_api.read_nofollow(CHART, limits.chart_bytes + 1)
    if (st2.st_dev, st2.st_ino, st2.st_size) != (st.st_dev, st.st_ino, st.st_size) or len(data) != st.st_size:
        return None, "truncated", others
    png, reason = canonicalize_png(data, limits)
    return png, reason, others


# ── the supervisor ───────────────────────────────────────────────────────────

def supervise(argv, os_api):
    """Return the process exit status; writes at most one frame through os_api.write_frame."""
    try:
        mode, run_id, prelude, limits = parse_args(argv)
    except UsageError as exc:
        os_api.diagnostic(f"insightpilot-supervisor: usage: {exc}")
        return EXIT_USAGE
    os_api.install_signal_plan(sh.signal_plan())
    os_api.set_not_dumpable()
    attestation = collect_attestation(os_api)
    stdout = sh.BoundedCollector(limits.stdout_bytes)
    stderr = sh.HeadTailCollector(limits.stderr_head_bytes, limits.stderr_tail_bytes)
    if mode == "attest-only":
        os_api.write_frame(sh.assemble_frame(run_id=run_id, wait_status=0, timed_out=False, oom_kill_events=0,
                                             stdout=stdout, stderr=stderr, attestation=attestation, limits=limits))
        return 0

    child = os_api.spawn(child_argv(mode, prelude, limits), CHILD_ENV)
    deadline = os_api.monotonic() + limits.code_wall_seconds
    timed_out, child_status, open_streams = False, None, {"stdout", "stderr"}
    while open_streams or child_status is None:
        remaining = deadline - os_api.monotonic()
        if remaining <= 0:
            timed_out = child_status is None or bool(open_streams)
            break
        for name, chunk in os_api.read_ready(child, min(remaining, 0.5)):
            if chunk:
                (stdout if name == "stdout" else stderr).feed(chunk)
            else:
                open_streams.discard(name)
        if child_status is None:
            child_status = os_api.poll(child)

    os_api.kill_all()                                  # kill(-1, SIGKILL): every process but PID 1
    reaped = os_api.reap_all()                         # {pid: status}, until ECHILD
    if child_status is None:
        child_status = reaped.get(child.pid)
    if child_status is None or os_api.other_pids():
        os_api.diagnostic("insightpilot-supervisor: processes survived or the child was not reaped; no frame")
        return EXIT_UNSAFE_STATE
    if timed_out:
        child_status = 9                               # killed by SIGKILL (wait status of a SIGKILL death)
    events = oom_kill_events(os_api)
    chart = reason = None
    others = 0
    ok = not timed_out and os.WIFEXITED(child_status) and os.WEXITSTATUS(child_status) == 0
    if mode == "run" and ok:
        chart, reason, others = stage_a(os_api, limits)
    os_api.write_frame(sh.assemble_frame(
        run_id=run_id, wait_status=child_status, timed_out=timed_out, oom_kill_events=events,
        stdout=stdout, stderr=stderr, chart=chart, chart_reject_reason=reason, out_other_entries=others,
        attestation=attestation, limits=limits))
    return 0


class RealOS:  # pragma: no cover - exercised only inside the container (P3.6-4 / VE-2)
    """The only place the supervisor touches the operating system."""

    def __init__(self):
        import select
        import signal
        import subprocess
        import time
        self._select, self._signal, self._subprocess, self._time = select, signal, subprocess, time

    def diagnostic(self, text):
        os.write(2, (text + "\n").encode("utf-8", "replace"))

    def install_signal_plan(self, plan):
        sig = self._signal
        for name, action in plan.items():
            # P3.6-4B: unnamed real-time signals are planned as "SIGRT<number>" (supervisor_helpers.signal_plan);
            # the signal module has no such attribute, so resolve them by number (host smoke test, 2026-11-24).
            num = int(name[5:]) if name.startswith("SIGRT") and name[5:].isdigit() else getattr(sig, name)
            if action == "ignore":
                sig.signal(num, sig.SIG_IGN)
            elif action == "handle":
                sig.signal(num, lambda *_: None)
            else:
                sig.signal(num, sig.SIG_DFL)

    def set_not_dumpable(self):
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) != 0:      # PR_SET_DUMPABLE = 4
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE) failed")

    def read_text(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def listdir(self, path):
        return sorted(os.listdir(path))

    def dev_writable(self):
        return os.access("/dev", os.W_OK)

    def spawn(self, argv, env):
        return self._subprocess.Popen(argv, stdin=self._subprocess.DEVNULL, stdout=self._subprocess.PIPE,
                                      stderr=self._subprocess.PIPE, env=env, close_fds=True,
                                      start_new_session=True, cwd="/tmp")

    def read_ready(self, child, timeout):
        streams = {"stdout": child.stdout, "stderr": child.stderr}
        live = [s for s in streams.values() if s is not None and not s.closed]
        if not live:
            self._time.sleep(min(timeout, 0.05))
            return []
        ready, _, _ = self._select.select(live, [], [], timeout)
        out = []
        for name, s in streams.items():
            if s in ready:
                chunk = os.read(s.fileno(), READ_CHUNK)
                if not chunk:
                    s.close()
                out.append((name, chunk))
        return out

    def poll(self, child):
        try:
            pid, status = os.waitpid(child.pid, os.WNOHANG)
        except ChildProcessError:
            return None
        return status if pid == child.pid else None

    def monotonic(self):
        return self._time.monotonic()

    def kill_all(self):
        try:
            os.kill(-1, self._signal.SIGKILL)
        except ProcessLookupError:
            pass

    def reap_all(self):
        out = {}
        while True:
            try:
                pid, status = os.waitpid(-1, 0)
            except ChildProcessError:
                return out
            out[pid] = status

    def other_pids(self):
        return [int(p) for p in os.listdir("/proc") if p.isdigit() and int(p) != 1]

    def lstat(self, path):
        return os.lstat(path)

    def read_nofollow(self, path, cap):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            data = b""
            while len(data) < cap:
                chunk = os.read(fd, min(READ_CHUNK, cap - len(data)))
                if not chunk:
                    break
                data += chunk
            return data, st
        finally:
            os.close(fd)

    def write_frame(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(supervise(sys.argv[1:], RealOS()))
