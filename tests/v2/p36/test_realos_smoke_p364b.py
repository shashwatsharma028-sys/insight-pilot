"""
P3.6-4B: host smoke test of the supervisor's RealOS (the only code that touches the OS in the container).

Runs in a SEPARATE interpreter, so the signal plan never affects pytest. It exercises only methods that are
safe on a host: the signal plan, PR_SET_DUMPABLE, /proc reads, spawning a trivial trusted child with the
pipe/poll/reap loop, the O_NOFOLLOW read, and frame writing to a pipe. kill_all (kill(-1)) and other_pids
are NEVER called here; they are only meaningful as PID 1 of a container. Found the SIGRT<n> crash
(supervisor.RealOS.install_signal_plan) before it reached CI.
"""

import json
import os
import subprocess
import sys

from tests.v2.golden.reference import ROOT

SMOKE = r'''
import json, os, sys, tempfile
sys.path.insert(0, sys.argv[1]); sys.path.insert(1, sys.argv[2])
import supervisor as s
from v2.execution import supervisor_helpers as sh
r = s.RealOS(); out = {}
plan = sh.signal_plan()
r.install_signal_plan(plan)
import signal
out["sigpipe_ignored"] = signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN
out["sigchld_handled"] = callable(signal.getsignal(signal.SIGCHLD))
out["rt_signals"] = sorted(n for n in plan if n.startswith("SIGRT"))[:3]
r.set_not_dumpable()
out["status_keys"] = sorted(sh.parse_proc_status(r.read_text("/proc/self/status")))
out["lo_listed"] = "lo" in r.listdir("/sys/class/net")
child = r.spawn([sys.executable, "-I", "-B", "-c",
                 "import sys; sys.stdout.write('x' * 70000); sys.stderr.write('e'); sys.exit(3)"], {"PATH": "/usr/bin:/bin"})
got, status, open_streams = {"stdout": 0, "stderr": 0}, None, {"stdout", "stderr"}
deadline = r.monotonic() + 30
while (open_streams or status is None) and r.monotonic() < deadline:
    for name, chunk in r.read_ready(child, 0.5):
        if chunk:
            got[name] += len(chunk)
        else:
            open_streams.discard(name)
    if status is None:
        status = r.poll(child)
out["child"] = {"stdout": got["stdout"], "stderr": got["stderr"], "exit": os.WEXITSTATUS(status), "reaped_after": r.reap_all()}
d = tempfile.mkdtemp()
p = os.path.join(d, "chart.png")
with open(p, "wb") as f:
    f.write(b"\x89PNG" + b"0" * 100)
data, st = r.read_nofollow(p, 1000)
out["nofollow"] = [len(data), st.st_ino == r.lstat(p).st_ino]
os.symlink(p, os.path.join(d, "link.png"))
try:
    r.read_nofollow(os.path.join(d, "link.png"), 10)
    out["symlink_refused"] = False
except OSError:
    out["symlink_refused"] = True
rfd, wfd = os.pipe()
saved = os.dup(1); os.dup2(wfd, 1); os.close(wfd)
r.write_frame(b"IPX1" + b"z" * 50000)
os.dup2(saved, 1); os.close(saved)
buf = b""
while len(buf) < 50004:
    buf += os.read(rfd, 65536)
out["frame_bytes"] = len(buf)
sys.stderr.write("SMOKE " + json.dumps(out) + "\n")
'''


def test_realos_safe_methods_work_on_a_real_kernel(tmp_path):
    script = tmp_path / "smoke.py"
    script.write_text(SMOKE)
    assert "kill_all" not in SMOKE and "other_pids" not in SMOKE        # never on a host
    out = subprocess.run([sys.executable, "-I", "-B", str(script), os.path.join(ROOT, "v2", "execution", "image", "runtime"),
                          ROOT], capture_output=True, text=True, timeout=120, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr[-2000:]
    r = json.loads([ln for ln in out.stderr.splitlines() if ln.startswith("SMOKE ")][-1][6:])
    assert r["sigpipe_ignored"] is True and r["sigchld_handled"] is True
    assert {"uid", "gid", "cap_eff", "no_new_privs", "seccomp"} <= set(r["status_keys"])
    assert r["lo_listed"] is True
    assert r["child"] == {"stdout": 70000, "stderr": 1, "exit": 3, "reaped_after": {}}
    assert r["nofollow"] == [104, True] and r["symlink_refused"] is True
    assert r["frame_bytes"] == 50004
