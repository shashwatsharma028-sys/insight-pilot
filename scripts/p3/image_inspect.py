#!/usr/bin/env python3
"""
InsightPilot image inspector (P3.5a, extended in P3.5b). Standard library only.

Inside a container it is run by scripts/p3/rootless.py as
    python3 -I -B - [--runtime-only] < image_inspect.py
on stdin, so the inspector is never part of the image. It prints one JSON
report with two separate sections:

  "image"    IMAGE PROPERTIES: what the image filesystem contains (setid
             files, secret-like and repository files, virtual environments,
             forbidden tools, the installed distributions, /opt/ip). These
             hold however the container is started.
  "runtime"  RUNTIME-ENFORCED PROPERTIES: what the launcher's options
             actually produced for this process: credentials, NoNewPrivs,
             capability sets, seccomp mode, mounts, network interfaces and
             routes, cgroup limits, environment names, and write probes.
             These are NOT image properties, and they hold only for a
             container started with the same options.

On the host, `evaluate` checks a report against
v2/execution/image/image-expectations.json. Every runtime field is verified,
not just recorded: a missing or unparsable field is a problem.

It reads names, modes, /proc and /sys only. It never imports or executes
anything it finds. The only files it creates are in the write probes: a
probe file is created exclusively and removed at once.
"""

import json
import os
import re
import stat
import sys

SKIP = ("/proc", "/sys", "/dev")
SITE = "usr/local/lib/python3.11/site-packages"
SECRET_NAMES = re.compile(r"^(\.env(\..*)?|\.netrc|\.pypirc|\.git-credentials|credentials\.json|"
                          r"id_(rsa|dsa|ecdsa|ed25519)|.*\.(key|p12|pfx|keystore))$", re.I)
DEV_DIR_NAMES = {".git", ".svn", ".hg", "__pytest_cache__", ".pytest_cache", ".mypy_cache", "node_modules"}
SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
PROXY_ENV = re.compile(r"(^|_)(PROXY)$|^NO_PROXY$|PROXY", re.I)
PUBLIC_ENV = {"GPG_KEY"}          # the base image's public release-signing key fingerprint
CAP_FIELDS = ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
CGROUP_FILES = ("memory.max", "memory.swap.max", "pids.max", "cpu.max")
WRITE_PROBES = ("/", "/etc", "/usr", "/usr/local/lib/python3.11/site-packages", "/opt/ip", "/var/tmp", "/run",
                "/dev", "/dev/shm", "/home", "/root", "/tmp")
FILE_WRITE_PROBES = ("/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/etc/passwd", "/usr/local/bin/python3.11",
                     "/opt/ip/inert_entrypoint.py",                      # P3.5a reference image
                     "/opt/ip/supervisor.py", "/opt/ip/runner.py", "/opt/ip/selftest.py",  # P3.6-4A image
                     "/opt/ip/preludes.json", "/opt/ip/v2/execution/frame.py")

FORBIDDEN_PATHS = [
    "/usr/local/lib/python3.11/site-packages/pip", "/usr/local/lib/python3.11/site-packages/setuptools",
    "/usr/local/lib/python3.11/site-packages/wheel", "/usr/local/lib/python3.11/site-packages/pkg_resources",
    "/usr/local/lib/python3.11/site-packages/_distutils_hack", "/usr/local/lib/python3.11/ensurepip",
    "/usr/local/bin/pip", "/usr/local/bin/pip3", "/usr/local/bin/pip3.11", "/usr/local/bin/wheel",
    "/usr/bin/gcc", "/usr/bin/cc", "/usr/bin/c++", "/usr/bin/g++", "/usr/bin/clang", "/usr/bin/make",
    "/usr/bin/ld", "/usr/bin/as", "/usr/bin/curl", "/usr/bin/wget",
    "/usr/local/lib/python3.11/site-packages/bin", "/usr/local/lib/python3.11/site-packages/share",
]


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


# ── image properties ─────────────────────────────────────────────────────────

def inspect_image(root="/"):
    root = os.path.abspath(root)
    rep = {"setid_files": [], "secret_like_files": [], "dev_dirs": [], "venvs": [], "distributions": {},
           "opt_ip": [], "present_forbidden": [], "world_writable_files": []}
    for dirpath, dirnames, files in os.walk(root, followlinks=False):
        rel = "/" + os.path.relpath(dirpath, root).replace(os.sep, "/").lstrip(".")
        rel = "/" if rel in ("/", "/.") else rel.rstrip("/")
        if any(rel == s or rel.startswith(s + "/") for s in SKIP):
            dirnames[:] = []
            continue
        for d in list(dirnames):
            if d in DEV_DIR_NAMES:
                rep["dev_dirs"].append(f"{rel.rstrip('/')}/{d}")
        for name in files:
            path = os.path.join(dirpath, name)
            vpath = f"{rel.rstrip('/')}/{name}"
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                    rep["setid_files"].append(vpath)
                if st.st_mode & stat.S_IWOTH and not vpath.startswith(("/tmp/", "/var/tmp/")):
                    rep["world_writable_files"].append(vpath)
            if SECRET_NAMES.match(name):
                rep["secret_like_files"].append(vpath)
            if name == "pyvenv.cfg":
                rep["venvs"].append(rel)
    site = os.path.join(root, SITE)
    if os.path.isdir(site):
        for entry in sorted(os.listdir(site)):
            if entry.endswith(".dist-info"):
                name = version = None
                try:
                    with open(os.path.join(site, entry, "METADATA"), encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if not line.strip():
                                break
                            if line.startswith("Name:"):
                                name = line.split(":", 1)[1].strip()
                            elif line.startswith("Version:"):
                                version = line.split(":", 1)[1].strip()
                except OSError:
                    pass
                rep["distributions"][_norm(name or entry)] = version
    opt = os.path.join(root, "opt/ip")
    if os.path.isdir(opt):
        rep["opt_ip"] = sorted(os.path.relpath(os.path.join(d, f), opt).replace(os.sep, "/")
                               for d, _, fs in os.walk(opt) for f in fs)
    for p in FORBIDDEN_PATHS:
        if os.path.lexists(os.path.join(root, p.lstrip("/"))):
            rep["present_forbidden"].append(p)
    return rep


# ── runtime-enforced properties ──────────────────────────────────────────────

def _read(path):
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _status(text):
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mountinfo(text):
    mounts = []
    for line in (text or "").splitlines():
        left, sep, right = line.partition(" - ")
        f = left.split()
        r = right.split()
        if not sep or len(f) < 6 or len(r) < 2:
            continue
        mounts.append({"mountpoint": _unescape(f[4]), "options": f[5].split(","), "fstype": r[0],
                       "source": _unescape(r[1]), "super_options": r[2].split(",") if len(r) > 2 else []})
    return mounts


def _write_probe(directory):
    path = os.path.join(directory, f".ip-write-probe-{os.getpid()}")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        return {2: "ENOENT", 13: "EACCES", 30: "EROFS", 1: "EPERM"}.get(exc.errno, f"errno{exc.errno}")
    os.close(fd)
    os.unlink(path)
    return "writable"


def _file_write_probe(path):
    """Open an EXISTING file for writing without truncating or writing; report whether that was allowed."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        return {2: "ENOENT", 13: "EACCES", 30: "EROFS", 1: "EPERM", 26: "ETXTBSY"}.get(exc.errno,
                                                                                    f"errno{exc.errno}")
    os.close(fd)
    return "writable"


def inspect_runtime():
    st = _status(_read("/proc/self/status"))
    dev = _read("/proc/self/net/dev")                    # per network namespace (sysfs may not be)
    nets = None if dev is None else sorted(ln.split(":", 1)[0].strip() for ln in dev.splitlines()[2:] if ":" in ln)
    routes = _read("/proc/self/net/route")
    cg = {name: (_read(f"/sys/fs/cgroup/{name}") or "").strip() or None for name in CGROUP_FILES}
    return {
        "uid": os.getuid(), "gid": os.getgid(), "euid": os.geteuid(), "egid": os.getegid(),
        "groups": sorted(os.getgroups()),
        "no_new_privs": st.get("NoNewPrivs"),
        "seccomp": st.get("Seccomp"),
        "caps": {k: st.get(k) for k in CAP_FIELDS},
        "uid_map": (_read("/proc/self/uid_map") or "").split(),
        "gid_map": (_read("/proc/self/gid_map") or "").split(),
        "env_names": sorted(os.environ),
        "net_interfaces": nets,
        "routes": None if routes is None else [ln for ln in routes.splitlines()[1:] if ln.strip()],
        "mounts": parse_mountinfo(_read("/proc/self/mountinfo")),
        "cgroup": cg,
        "write_probes": {d: _write_probe(d) for d in WRITE_PROBES},
        "file_write_probes": {f: _file_write_probe(f) for f in FILE_WRITE_PROBES},
    }


# ── evaluation (host side) ───────────────────────────────────────────────────

def evaluate_image(img, expectations, lock_versions):
    problems = []
    for key, label in (("setid_files", "setuid/setgid files"), ("world_writable_files", "world-writable files"),
                       ("secret_like_files", "secret-like files"),
                       ("dev_dirs", "repository/development directories"), ("venvs", "virtual environments"),
                       ("present_forbidden", "forbidden tools or packages present")):
        if img[key]:
            problems.append(f"IMAGE: {label}: {img[key]}")
    if img["distributions"] != lock_versions:
        extra = sorted(set(img["distributions"]) - set(lock_versions))
        missing = sorted(set(lock_versions) - set(img["distributions"]))
        wrong = sorted(n for n in set(lock_versions) & set(img["distributions"])
                       if img["distributions"][n] != lock_versions[n])
        problems.append(f"IMAGE: distributions differ from the lock: extra={extra} missing={missing} wrong={wrong}")
    if img["opt_ip"] != expectations["opt_ip_files"]:
        problems.append(f"IMAGE: /opt/ip files {img['opt_ip']} != {expectations['opt_ip_files']}")
    return problems


def _is_zero_hex(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]+", value) is not None and int(value, 16) == 0


def evaluate_runtime(rt, expectations, image_env_names):
    """Every field is verified; an absent or unparsable value is a problem, never a pass."""
    exp = expectations["runtime"]
    p = []
    if (rt.get("uid"), rt.get("euid"), rt.get("gid"), rt.get("egid")) != (exp["uid"], exp["uid"], exp["gid"],
                                                                         exp["gid"]):
        p.append(f"RUNTIME: credentials uid/euid/gid/egid = {rt.get('uid')}/{rt.get('euid')}/"
                 f"{rt.get('gid')}/{rt.get('egid')}")
    if not set(rt.get("groups", [None])) <= {exp["gid"]}:
        p.append(f"RUNTIME: supplementary groups {rt.get('groups')}")
    if rt.get("no_new_privs") != "1":
        p.append(f"RUNTIME: NoNewPrivs is {rt.get('no_new_privs')!r}, not '1'")
    if rt.get("seccomp") != exp["seccomp_mode"]:
        p.append(f"RUNTIME: seccomp mode {rt.get('seccomp')!r}, expected {exp['seccomp_mode']!r} (filter)")
    caps = rt.get("caps") or {}
    for field in CAP_FIELDS:
        if not _is_zero_hex(caps.get(field)):
            p.append(f"RUNTIME: {field} is {caps.get(field)!r}, not empty")
    if rt.get("net_interfaces") != exp["net_interfaces"]:
        p.append(f"RUNTIME: network interfaces {rt.get('net_interfaces')} != {exp['net_interfaces']}")
    if rt.get("routes") != []:
        p.append(f"RUNTIME: IPv4 routes present: {rt.get('routes')}")
    mounts = rt.get("mounts") or []
    by_point = {}
    for m in mounts:
        by_point[m["mountpoint"]] = m                        # the last mount on a point is the visible one
    root = by_point.get("/")
    if root is None or "ro" not in root["options"]:
        p.append(f"RUNTIME: root filesystem is not read-only: {root and root['options']}")
    for point, spec in exp["tmpfs"].items():
        m = by_point.get(point)
        if m is None or m["fstype"] != "tmpfs" or not set(spec["options"]) <= set(m["options"]):
            p.append(f"RUNTIME: {point} is not tmpfs with {spec['options']}: {m}")
        elif not any(o.startswith("size=") for o in m["super_options"]):
            p.append(f"RUNTIME: {point} tmpfs has no size bound")
    allowed = set(exp["allowed_mountpoints"]) | set(exp["tmpfs"])
    prefixes = tuple(exp["allowed_mount_prefixes"])
    for point, m in by_point.items():
        if point not in allowed and not point.startswith(prefixes):
            p.append(f"RUNTIME: unexpected mount {point} ({m['fstype']} from {m['source']})")
        if "rw" in m["options"] and point not in exp["rw_mountpoints_allowed"] and point not in exp["tmpfs"] \
                and not point.startswith(prefixes):
            p.append(f"RUNTIME: writable mount {point}")
    probes = rt.get("write_probes") or {}
    for d in exp["must_be_writable"]:
        if probes.get(d) != "writable":
            p.append(f"RUNTIME: {d} must be writable: {probes.get(d)}")
    for d in exp["must_not_be_writable"]:
        if probes.get(d) not in ("EROFS", "EACCES", "EPERM", "ENOENT"):
            p.append(f"RUNTIME: {d} must not be writable: {probes.get(d)}")
    fprobes = rt.get("file_write_probes") or {}
    for f in exp["files_must_not_open_for_write"]:
        if fprobes.get(f) is None or fprobes.get(f) == "writable":
            p.append(f"RUNTIME: {f} could be opened for writing (or was not probed): {fprobes.get(f)}")
    for name, value in exp["cgroup"].items():
        if (rt.get("cgroup") or {}).get(name) != value:
            p.append(f"RUNTIME: cgroup {name} = {(rt.get('cgroup') or {}).get(name)!r}, expected {value!r}")
    env = rt.get("env_names")
    if not isinstance(env, list):
        p.append("RUNTIME: environment names missing")
        env = []
    unexpected = sorted(set(env) - set(image_env_names) - set(exp["env_added_by_runtime"]))
    if unexpected:
        p.append(f"RUNTIME: unexpected environment variables: {unexpected}")
    proxies = [n for n in env if PROXY_ENV.search(n)]
    if proxies:
        p.append(f"RUNTIME: proxy variables reached the container: {proxies}")
    secretish = [n for n in env if SECRET_ENV.search(n) and n not in PUBLIC_ENV]
    if secretish:
        p.append(f"RUNTIME: secret-like environment variable names: {secretish}")
    return p


def evaluate(report, expectations, lock_versions, image_env_names):
    problems = []
    if "image" in report:
        problems += evaluate_image(report["image"], expectations, lock_versions)
    if "runtime" not in report:
        problems.append("RUNTIME: section missing")
    else:
        problems += evaluate_runtime(report["runtime"], expectations, image_env_names)
    return problems


def main(argv):
    if argv[:1] == ["evaluate"]:
        report = json.load(open(argv[1], encoding="utf-8"))
        expectations = json.load(open(argv[2], encoding="utf-8"))
        lock = json.load(open(argv[3], encoding="utf-8"))
        image_env = json.load(open(argv[4], encoding="utf-8"))
        lock_versions = {n: e["version"] for n, e in lock["packages"].items()}
        problems = evaluate(report, expectations, lock_versions, image_env)
        print("\n".join(problems) if problems else "OK: no problems")
        return 1 if problems else 0
    if "--root" in argv:                                 # host tests: image section of a fake root only
        json.dump({"image": inspect_image(argv[argv.index("--root") + 1])}, sys.stdout, indent=1, sort_keys=True)
        return 0
    report = {"runtime": inspect_runtime()}
    if "--runtime-only" not in argv:
        report["image"] = inspect_image("/")
    json.dump(report, sys.stdout, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
