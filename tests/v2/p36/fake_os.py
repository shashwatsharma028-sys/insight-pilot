"""
FakeOS: a test double for the in-image supervisor's operating-system facade (P3.6-2/3).

It NEVER executes code. The "child process" is simulated:
- `literal` behaviour reads the generated code text and interprets only print("...") and
  fail("...") calls with string literals (the P5 literal fixture form) via the AST, producing
  stdout chunks, or stderr plus exit status 1;
- scenario switches simulate a wall-clock timeout, an OOM kill, a stdout flood, surviving
  processes, an unreaped child, a chart in /out (bytes, symlink, hard link, oversize) and
  extra /out entries.
Every call is recorded, so tests can check ordering (signal plan and PR_SET_DUMPABLE before
the child starts; kill(-1) before the frame).
"""

import ast
import os
import stat
from types import SimpleNamespace

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
"""


def literal_output(code):
    """(stdout bytes, stderr bytes, exit code) for literal print/fail code; nothing is evaluated."""
    out, err = [], ""
    try:
        body = ast.parse(code).body
    except SyntaxError as exc:
        return b"", f"SyntaxError: {exc.msg}".encode(), 1
    for stmt in body:
        call = stmt.value if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) else None
        if not (call is not None and isinstance(call.func, ast.Name) and call.func.id in ("print", "fail")
                and len(call.args) == 1 and isinstance(call.args[0], ast.Constant)
                and isinstance(call.args[0].value, str)):
            return b"", b"LogicError: unsupported statement for the fake runtime", 1
        if call.func.id == "fail":
            err = "Traceback (most recent call last):\n" + call.args[0].value
            return ("\n".join(out) + ("\n" if out else "")).encode(), err.encode(), 1
        out.append(call.args[0].value)
    return ("\n".join(out) + ("\n" if out else "")).encode(), b"", 0


def _st(mode, size, nlink=1, ino=7):
    return SimpleNamespace(st_mode=mode, st_size=size, st_nlink=nlink, st_dev=1, st_ino=ino)


class FakeOS:
    def __init__(self, *, code="", stdout=None, stderr=b"", exit_code=0, hang=False, oom=False,
                 flood=0, survivors=(), unreaped=False, chart=None, chart_kind="file", out_extra=(),
                 status=GOOD_STATUS, mountinfo=GOOD_MOUNTINFO, net=("lo",), dev_writable=False,
                 memory_max="1073741824\n", pids_max="64\n", cpu_max="100000 100000\n", swap_max="0\n",
                 chunk=4096):
        if stdout is None:
            stdout, err2, rc = literal_output(code)
            stderr, exit_code = (err2 or stderr), (rc if rc else exit_code)
        self.calls, self.frames, self.diagnostics = [], [], []
        self.hang, self.oom, self.unreaped, self.survivors = hang, oom, unreaped, list(survivors)
        self._chunks = [("stdout", stdout[i:i + chunk]) for i in range(0, len(stdout), chunk)]
        self._chunks += [("stderr", stderr[i:i + chunk]) for i in range(0, len(stderr), chunk)]
        self._flood = flood
        self._eof_sent = False
        self._clock = 0.0
        self.exit_status = 9 if oom else (exit_code << 8)
        self.files = {"/proc/self/status": status, "/proc/self/mountinfo": mountinfo,
                      "/sys/fs/cgroup/memory.max": memory_max, "/sys/fs/cgroup/memory.swap.max": swap_max,
                      "/sys/fs/cgroup/pids.max": pids_max, "/sys/fs/cgroup/cpu.max": cpu_max,
                      "/sys/fs/cgroup/memory.events": f"low 0\nhigh 0\nmax 3\noom 1\noom_kill {1 if oom else 0}\n"}
        self.net, self._dev_writable = list(net), dev_writable
        self.chart, self.chart_kind, self.out_extra = chart, chart_kind, list(out_extra)
        self.child = SimpleNamespace(pid=42)
        self._reaped = False

    def _log(self, name, *args):
        self.calls.append((name,) + args)

    # facade
    def diagnostic(self, text):
        self.diagnostics.append(text)

    def install_signal_plan(self, plan):
        self._log("install_signal_plan", dict(plan))

    def set_not_dumpable(self):
        self._log("set_not_dumpable")

    def read_text(self, path):
        self._log("read_text", path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def listdir(self, path):
        if path == "/sys/class/net":
            return sorted(self.net)
        if path == "/out":
            return sorted((["chart.png"] if self.chart is not None else []) + self.out_extra)
        raise FileNotFoundError(path)

    def dev_writable(self):
        return self._dev_writable

    def spawn(self, argv, env):
        self._log("spawn", list(argv), dict(env))
        return self.child

    def read_ready(self, child, timeout):
        self._clock += 0.01
        if self._flood:
            self._flood -= 1
            return [("stdout", b"x" * 65536)]
        if self._chunks:
            return [self._chunks.pop(0)]
        if self.hang:
            self._clock += timeout
            return []
        if not self._eof_sent:
            self._eof_sent = True
            return [("stdout", b""), ("stderr", b"")]
        return []

    def poll(self, child):
        if self.hang or self._chunks or self._flood:
            return None
        if self.unreaped:
            return None
        self._reaped = True
        return self.exit_status

    def monotonic(self):
        return self._clock

    def kill_all(self):
        self._log("kill_all")

    def reap_all(self):
        self._log("reap_all")
        if self._reaped or self.unreaped:
            return {}
        self._reaped = True
        return {42: 9 if self.hang else self.exit_status}

    def other_pids(self):
        return list(self.survivors)

    def lstat(self, path):
        assert path == "/out/chart.png"
        size = len(self.chart or b"")
        if self.chart_kind == "symlink":
            return _st(stat.S_IFLNK | 0o777, size)
        if self.chart_kind == "fifo":
            return _st(stat.S_IFIFO | 0o644, 0)
        if self.chart_kind == "hardlink":
            return _st(stat.S_IFREG | 0o644, size, nlink=2)
        return _st(stat.S_IFREG | 0o644, size)

    def read_nofollow(self, path, cap):
        self._log("read_nofollow", path, cap)
        data = (self.chart or b"")[:cap]
        ino = 8 if self.chart_kind == "swapped" else 7
        return data, _st(stat.S_IFREG | 0o644, len(self.chart or b""), ino=ino)

    def write_frame(self, data):
        self._log("write_frame")
        self.frames.append(bytes(data))


def supervisor_args(run_id, *, mode="run", prelude="v1", limits=None):
    """The argv tail exactly as podman_argv.run_argv puts it after the image id."""
    from v2.execution import podman_argv
    from v2.execution.limits import DEFAULT_LIMITS
    from v2.execution.runtime import PreludeId
    argv = podman_argv.run_argv("/usr/bin/podman", "sha256:" + "c" * 64, instance_id="b" * 32, run_id=run_id,
                                inputs_dir=f"/tmp/x/instances/{'b' * 32}/runs/{run_id}/in",
                                prelude=PreludeId(prelude), mode=mode, limits=limits or DEFAULT_LIMITS)
    return argv[argv.index("sha256:" + "c" * 64) + 1:]


def png(size=(32, 24), mode="RGB", metadata=False):
    import io
    from PIL import Image, PngImagePlugin
    img = Image.new(mode, size, (200, 30, 60) if mode == "RGB" else (200, 30, 60, 128))
    buf = io.BytesIO()
    info = PngImagePlugin.PngInfo() if metadata else None
    if info is not None:
        info.add_text("Software", "matplotlib version3.11, https://matplotlib.org/")
    img.save(buf, "PNG", pnginfo=info, dpi=(120, 120) if metadata else None) if metadata else img.save(buf, "PNG")
    return buf.getvalue()


assert os.sep == "/"
