"""
P3.6-4B hand-written functional fixtures for the real-container validation (tests/container/p364b).

These are committed test fixtures, NOT model-generated code. Each one does a single benign, bounded
thing so the runtime's fail-closed paths and the requested isolation can be OBSERVED: an error, a
sleep past the wall limit, one large allocation, one connection attempt to a reserved documentation
address (192.0.2.1, RFC 5737), writes to read-only paths, and one /out entry that must be rejected.
None of them attempts privilege escalation or any hostile technique; that is VE-2, not this stage.

SHA256 pins every fixture: container_p364b.py refuses to run a fixture whose text differs, and
tests/v2/p36/test_container_suite_static_p364b.py checks the pins offline.
"""

import hashlib

OBSERVE = r'''
import hashlib, json, os, socket
def _try_write(path):
    try:
        with open(path, "x") as f:
            f.write("x")
        os.unlink(path)
        return "writable"
    except OSError as exc:
        return type(exc).__name__ + ":" + str(exc.errno)
def _connect(addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(addr)
        return "connected"
    except OSError as exc:
        return type(exc).__name__ + ":" + str(exc.errno)
    finally:
        s.close()
def _text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError as exc:
        return None
def _routes():
    v4 = _text("/proc/net/route") or ""
    v6 = _text("/proc/net/ipv6_route") or ""
    return {"ipv4": [[c[0], c[1]] for c in (l.split() for l in v4.splitlines()[1:]) if c],
            "ipv6": [[c[0], c[-1]] for c in (l.split() for l in v6.splitlines()) if c]}
def _dns_files():
    out = {}
    for p in ("/etc/resolv.conf", "/etc/hosts", "/etc/hostname", "/etc/nsswitch.conf"):
        t = _text(p)
        out[p] = None if t is None else {"bytes": len(t.encode()), "lines": len(t.splitlines()),
                                         "sha256": hashlib.sha256(t.encode()).hexdigest()}
    return out
def _lookup(name):
    try:
        socket.getaddrinfo(name, 80)
        return "resolved"
    except OSError as exc:
        return type(exc).__name__ + ":" + str(exc.errno)
with open("/proc/self/status") as f:
    status = {k: v.strip() for k, v in (line.split(":", 1) for line in f if ":" in line)}
with open("/proc/self/mountinfo") as f:
    mounts = [line.split(" - ")[0].split()[4:6] + line.split(" - ")[1].split()[:1] for line in f]
with open("/in/dataset.csv", "rb") as f:
    dataset_sha = hashlib.sha256(f.read()).hexdigest()
print("P364B_OBSERVE " + json.dumps({
    "uid": os.getuid(), "gid": os.getgid(), "euid": os.geteuid(), "groups": sorted(os.getgroups()),
    "status": {k: status.get(k) for k in ("Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb",
                                          "NoNewPrivs", "Seccomp", "Seccomp_filters")},
    "pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(),
    "env_names": sorted(os.environ), "in_entries": sorted(os.listdir("/in")), "dataset_sha256": dataset_sha,
    "net_interfaces": sorted(os.listdir("/sys/class/net")),
    "proc_net_dev": sorted(l.split(":")[0].strip() for l in (_text("/proc/net/dev") or "").splitlines()[2:] if ":" in l),
    "routes": _routes(), "dns_files": _dns_files(), "getaddrinfo_example_com": _lookup("example.com"),
    "connect_tcp_192_0_2_1_80": _connect(("192.0.2.1", 80)),
    "connect_tcp_loopback_9": _connect(("127.0.0.1", 9)),
    "writes": {p: _try_write(p) for p in ("/p364b", "/opt/ip/p364b", "/in/p364b", "/etc/p364b", "/usr/p364b",
                                          "/dev/p364b", "/var/tmp/p364b", "/tmp/p364b", "/out/p364b")},
    "mounts": mounts,
}, sort_keys=True))
'''

CODE_ERROR = 'raise RuntimeError("p364b fixture: deliberate code error")\n'
SLEEP_PAST_WALL = 'import time\ntime.sleep(600)\n'
ALLOCATE_2_GIB = 'block = bytearray(2 * 1024 * 1024 * 1024)\nprint(len(block))\n'
CHART_SYMLINK = 'import os\nos.symlink("/etc/hostname", "/out/chart.png")\n'
CHART_NOT_PNG = 'with open("/out/chart.png", "wb") as f:\n    f.write(b"not a png")\n'

FIXTURES = {"observe": OBSERVE, "code_error": CODE_ERROR, "sleep_past_wall": SLEEP_PAST_WALL,
            "allocate_2_gib": ALLOCATE_2_GIB, "chart_symlink": CHART_SYMLINK, "chart_not_png": CHART_NOT_PNG}
SHA256 = {
    "observe": "bc3bca7cf2091d1f69ddc9eb9a4045cacdfd52560af8488f9f3770a6d24db647",
    "code_error": "04f316a138368ea0380e0750443b8950a1a152a8db6ab453b8ca9aa028970569",
    "sleep_past_wall": "7eaecb82187b5f0c525f8175cdc2484728c0d096371bd69fe329bfe905c1725a",
    "allocate_2_gib": "a705e22cdd85d9e89bbc961aa5e513b7b2ebcd0606a2714acee713c60a7ad24f",
    "chart_symlink": "9855f557d72026fbf4d20e7936eb122d9e9a2237080d3e12effcda1909d91fc2",
    "chart_not_png": "3cf4a0fe7407848466fccdd388ef933af57da0bf37d293c37c932e05f52a2caf",
}


def fixture(name):
    code = FIXTURES[name]
    if hashlib.sha256(code.encode()).hexdigest() != SHA256[name]:
        raise RuntimeError(f"fixture {name} differs from its reviewed SHA-256 pin")
    return code
