"""
Pure helpers for the future in-container supervisor (P3.4).

Everything here is a pure function or a passive data structure that can be
tested on the host without running anything:

- bounded output collectors (stdout head; stderr head + tail);
- the signal-disposition PLAN (data only; nothing is installed here);
- interpretation of an already-obtained wait status;
- parsing of /proc/self/status, mountinfo and cgroup file CONTENTS (passed in
  as text) into the attestation dictionary;
- classification of an /out entry from its stat fields;
- frame assembly (delegates to v2.execution.frame.encode_frame).

Deliberately NOT here (deferred to the validated container stage, P3.5/P3.6):
starting a child, exec/eval of generated code, installing signal handlers,
prctl, kill(-1, ...)/killpg, reaping, reading /proc or /sys, any container or
network operation. tests/v2/p3/test_supervisor_helpers_p34.py enforces that
statically.
"""

import os
import re
import signal
import stat

from v2.execution import frame
from v2.execution.limits import DEFAULT_LIMITS, Limits

# ── bounded collectors ───────────────────────────────────────────────────────


class BoundedCollector:
    """Keep the first `cap` bytes; count and flag everything beyond."""

    def __init__(self, cap: int):
        if type(cap) is not int or cap < 0:
            raise ValueError("cap must be a non-negative int")
        self.cap = cap
        self._buf = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.cap - len(self._buf)
        if room > 0:
            self._buf += chunk[:room]

    @property
    def truncated(self) -> bool:
        return self.total > self.cap

    def value(self) -> bytes:
        return bytes(self._buf)


class HeadTailCollector:
    """
    Keep the first `head` and the last `tail` bytes. If nothing was dropped,
    value() is the whole output; otherwise it is exactly head + tail bytes
    and `truncated` is True (so the exception line at the end survives).
    """

    def __init__(self, head: int, tail: int):
        for v in (head, tail):
            if type(v) is not int or v < 0:
                raise ValueError("head and tail must be non-negative ints")
        self.head, self.tail = head, tail
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk and self.tail:
            self._tail += chunk
            if len(self._tail) > self.tail:
                del self._tail[:len(self._tail) - self.tail]

    @property
    def truncated(self) -> bool:
        return self.total > self.head + self.tail

    def value(self) -> bytes:
        return bytes(self._head) + bytes(self._tail)


# ── signal plan (data only) ──────────────────────────────────────────────────

HANDLED = ("SIGCHLD",)          # reaped by the supervisor
IGNORED = ("SIGPIPE",)


def signal_plan() -> dict:
    """
    Planned disposition for every catchable signal (correction B-2): all
    default except SIGCHLD (handled) and SIGPIPE (ignored). As PID 1 of its
    namespace, the supervisor then receives no same-namespace signal it has
    no handler for. Returns {signal name: "handle" | "ignore" | "default"}.
    Installing it is deferred to the container stage.
    """
    plan = {}
    for sig in sorted(signal.valid_signals()):
        if sig in (signal.SIGKILL, signal.SIGSTOP):
            continue
        try:
            name = signal.Signals(sig).name
        except ValueError:          # real-time signals without a name
            name = f"SIGRT{sig}"
        plan[name] = "handle" if name in HANDLED else "ignore" if name in IGNORED else "default"
    return plan


# ── wait status ──────────────────────────────────────────────────────────────

def termination_from_wait_status(status: int):
    """(child_exit, child_signal) from an os.waitpid() status that was already obtained."""
    if type(status) is not int:
        raise TypeError("status must be an int")
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), None
    if os.WIFSIGNALED(status):
        return None, os.WTERMSIG(status)
    raise ValueError("the child has not terminated")


# ── attestation parsing (contents passed in as text) ─────────────────────────

def parse_proc_status(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    try:
        uid = int(fields["Uid"].split()[1])          # effective uid
        gid = int(fields["Gid"].split()[1])
        caps = {k: fields[n].lower() for k, n in (("cap_eff", "CapEff"), ("cap_prm", "CapPrm"),
                                                   ("cap_bnd", "CapBnd"))}
        nnp = int(fields["NoNewPrivs"])
        seccomp = int(fields["Seccomp"])
    except (KeyError, IndexError, ValueError):
        raise ValueError("unrecognised /proc/self/status content") from None
    return {"uid": uid, "gid": gid, **caps, "no_new_privs": nnp, "seccomp": seccomp}


def parse_writable_mounts(mountinfo: str) -> list:
    """Mount points mounted read-write, from /proc/self/mountinfo content."""
    writable = set()
    for line in mountinfo.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        mount_point, options = parts[4], parts[5].split(",")
        if "rw" in options:
            writable.add(mount_point.replace("\\040", " "))
    return sorted(writable)


_CGROUP_INT = re.compile(r"[0-9]{1,20}")


def parse_cgroup_limit(text: str):
    value = text.strip()
    if value == "max":
        return value
    if not _CGROUP_INT.fullmatch(value):
        raise ValueError("unrecognised cgroup limit")
    return int(value)


def build_attestation(*, proc_status: str, mountinfo: str, net_interfaces, dev_writable: bool,
                      memory_max: str, memory_swap_max: str, pids_max: str, cpu_max: str) -> dict:
    att = parse_proc_status(proc_status)
    att.update({
        "net_interfaces": sorted(set(net_interfaces)),
        "writable_mounts": [m for m in parse_writable_mounts(mountinfo) if m not in ("/proc", "/sys")],
        "dev_writable": bool(dev_writable),
        "memory_max": parse_cgroup_limit(memory_max),
        "memory_swap_max": parse_cgroup_limit(memory_swap_max),
        "pids_max": parse_cgroup_limit(pids_max),
        "cpu_max": cpu_max.strip(),
    })
    return frame.validate_attestation_shape(att)


# ── /out entry classification (stat fields passed in) ───────────────────────

def classify_out_entry(st_mode: int, st_nlink: int, st_size: int, limits: Limits = DEFAULT_LIMITS):
    """Reason code for an /out/chart.png lstat result, or None if acceptable."""
    if stat.S_ISLNK(st_mode):
        return "symlink"
    if not stat.S_ISREG(st_mode):
        return "not_regular_file"
    if st_nlink != 1:
        return "hardlink"
    if st_size == 0:
        return "empty"
    if st_size > limits.chart_bytes:
        return "too_large"
    return None


# ── frame assembly ───────────────────────────────────────────────────────────

def assemble_frame(*, run_id, wait_status, timed_out, oom_kill_events, stdout: BoundedCollector,
                   stderr: HeadTailCollector, chart=None, chart_reject_reason=None, out_other_entries=0,
                   attestation, limits: Limits = DEFAULT_LIMITS) -> bytes:
    child_exit, child_signal = termination_from_wait_status(wait_status)
    oom_killed = bool(oom_kill_events) and child_signal == frame.SIGKILL and not timed_out
    ok = child_exit == 0 and not timed_out and not oom_killed
    return frame.encode_frame(
        run_id=run_id, child_exit=child_exit, child_signal=child_signal,
        timed_out=bool(timed_out), oom_killed=oom_killed, attestation=attestation,
        stdout=stdout.value(), stderr=stderr.value(),
        stdout_truncated=stdout.truncated, stderr_truncated=stderr.truncated,
        chart=chart if ok else None, chart_reject_reason=chart_reject_reason if ok else None,
        out_other_entries=out_other_entries, limits=limits)
