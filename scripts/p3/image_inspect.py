#!/usr/bin/env python3
"""
InsightPilot P3.5a image inspector. Standard library only.

Two modes:
  inspect   Walk a root filesystem and print a JSON report. Inside a built
            image it is run by verify_image.sh as
                python3 -I -B - < image_inspect.py
            (stdin, so the inspector is never part of the image). On the host,
            tests run it against fake roots with --root.
  evaluate  Compare a report with v2/execution/image/image-expectations.json
            and the lock; print the problems; exit 1 if there are any.

It reads file names, modes and dist-info METADATA only. It never imports or
executes anything it finds.
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
PUBLIC_ENV = {"GPG_KEY"}          # the base image's public release-signing key fingerprint


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def inspect(root="/"):
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
                meta = os.path.join(site, entry, "METADATA")
                name = version = None
                try:
                    with open(meta, encoding="utf-8", errors="replace") as f:
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
    return rep


def add_forbidden(rep, root, forbidden_paths):
    for p in forbidden_paths:
        if os.path.lexists(os.path.join(root, p.lstrip("/"))):
            rep["present_forbidden"].append(p)
    return rep


def add_process(rep):
    rep["process"] = {"uid": os.getuid(), "gid": os.getgid(), "euid": os.geteuid(),
                      "groups": sorted(os.getgroups()),
                      "env_names": sorted(os.environ),
                      "no_new_privs": _status_field("NoNewPrivs"),
                      "cap_eff": _status_field("CapEff")}
    return rep


def _status_field(field):
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def evaluate(rep, expectations, lock_versions):
    problems = []
    if rep["setid_files"]:
        problems.append(f"setuid/setgid files: {rep['setid_files']}")
    if rep["world_writable_files"]:
        problems.append(f"world-writable files: {rep['world_writable_files']}")
    if rep["secret_like_files"]:
        problems.append(f"secret-like files: {rep['secret_like_files']}")
    if rep["dev_dirs"]:
        problems.append(f"repository/development directories: {rep['dev_dirs']}")
    if rep["venvs"]:
        problems.append(f"virtual environments: {rep['venvs']}")
    if rep["present_forbidden"]:
        problems.append(f"forbidden tools or packages present: {rep['present_forbidden']}")
    if rep["distributions"] != lock_versions:
        extra = sorted(set(rep["distributions"]) - set(lock_versions))
        missing = sorted(set(lock_versions) - set(rep["distributions"]))
        wrong = sorted(n for n in set(lock_versions) & set(rep["distributions"])
                       if rep["distributions"][n] != lock_versions[n])
        problems.append(f"distributions differ from the lock: extra={extra} missing={missing} wrong={wrong}")
    if rep["opt_ip"] != expectations["opt_ip_files"]:
        problems.append(f"/opt/ip files {rep['opt_ip']} != {expectations['opt_ip_files']}")
    proc = rep.get("process")
    if proc is not None:
        if (proc["uid"], proc["gid"], proc["euid"]) != (expectations["uid"], expectations["gid"], expectations["uid"]):
            problems.append(f"not running as {expectations['uid']}:{expectations['gid']}: {proc}")
        secretish = [n for n in proc["env_names"] if SECRET_ENV.search(n) and n not in PUBLIC_ENV]
        if secretish:
            problems.append(f"secret-like environment variable names: {secretish}")
    return problems


def main(argv):
    if argv[:1] == ["evaluate"]:
        report = json.load(open(argv[1], encoding="utf-8"))
        expectations = json.load(open(argv[2], encoding="utf-8"))
        lock = json.load(open(argv[3], encoding="utf-8"))
        lock_versions = {n: e["version"] for n, e in lock["packages"].items()}
        problems = evaluate(report, expectations, lock_versions)
        print("\n".join(problems) if problems else "OK: no problems")
        return 1 if problems else 0
    root = argv[argv.index("--root") + 1] if "--root" in argv else "/"
    expectations = {"forbidden_paths": FORBIDDEN_PATHS}
    rep = add_forbidden(inspect(root), root, expectations["forbidden_paths"])
    if root == "/":
        add_process(rep)
    json.dump(rep, sys.stdout, indent=1, sort_keys=True)
    return 0


FORBIDDEN_PATHS = [
    "/usr/local/lib/python3.11/site-packages/pip", "/usr/local/lib/python3.11/site-packages/setuptools",
    "/usr/local/lib/python3.11/site-packages/wheel", "/usr/local/lib/python3.11/site-packages/pkg_resources",
    "/usr/local/lib/python3.11/site-packages/_distutils_hack", "/usr/local/lib/python3.11/ensurepip",
    "/usr/local/bin/pip", "/usr/local/bin/pip3", "/usr/local/bin/pip3.11", "/usr/local/bin/wheel",
    "/usr/bin/gcc", "/usr/bin/cc", "/usr/bin/c++", "/usr/bin/g++", "/usr/bin/clang", "/usr/bin/make",
    "/usr/bin/ld", "/usr/bin/as", "/usr/bin/curl", "/usr/bin/wget",
    "/usr/local/lib/python3.11/site-packages/bin", "/usr/local/lib/python3.11/site-packages/share",
]

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
