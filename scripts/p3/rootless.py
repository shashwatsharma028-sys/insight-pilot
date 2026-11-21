#!/usr/bin/env python3
"""
InsightPilot P3.5b: rootless build and validation driver (development only).

  check-host [--probe]          Gather host facts and evaluate them. With
                                --probe, also start a real test container
                                from the digest-pinned base image (pulled
                                first) with the required user mapping,
                                security options and limits. The container
                                must report them as enforced: a printed host
                                status is not proof.
  build --wheel-cache DIR [--arch A]
                                Native-architecture build only. Refuses before
                                any container work unless every host check
                                and the enforcement probe pass.
  verify IMAGE [--evidence F]   Image config, image contents, runtime-enforced
                                properties and the inert entrypoint.
  measure-startup IMAGE [--warm N] [--cold N]
  golden IMAGE [--evidence F]   The pre-reviewed golden fixtures in
                                golden_fixtures.json, inside the image:
                                read-only dataset copies, outputs on a
                                bounded tmpfs.

It never runs as root and never uses a rootful, remote or Docker runtime.
Every podman process gets a minimal environment, with no proxy variables
and no credentials. Every container gets --http-proxy=false and
--env-host=false. No generated or user-supplied code is executed: the only
programs run in containers are the inspector, the inert entrypoint and the
hash-pinned golden fixtures.
"""

import argparse
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
IMAGE_DIR = os.path.join(ROOT, "v2", "execution", "image")
sys.path.insert(0, HERE)

import image_inspect  # noqa: E402  (same directory, standard library only)
import image_tool     # noqa: E402

OCI_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}
MIN_PODMAN = (4, 4)                     # --identity-label, --chmod in COPY, cgroup v2 limits
SUBID_MIN = 65536
REMOTE_ENV = ("CONTAINER_HOST", "CONTAINER_CONNECTION", "DOCKER_HOST", "CONTAINER_SSHKEY", "DOCKER_CONTEXT")
PODMAN_ENV_KEEP = ("PATH", "HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LANG", "USER",
                   "LOGNAME")
PULL_ENV_EXTRA = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE")
LIMITS = {"memory": "256m", "pids": "64", "cpus": "1"}


class Refused(SystemExit):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(2)


def _json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


BASE = _json(os.path.join(IMAGE_DIR, "base", "base-image.json"))
EXPECT = _json(os.path.join(IMAGE_DIR, "image-expectations.json"))
INVENTORY = _json(os.path.join(IMAGE_DIR, "dependency-inventory.json"))
BASE_REF = f"{BASE['reference']}@{BASE['index_digest']}"            # exactly the Containerfile FROM


# ── environment handed to podman ────────────────────────────────────────────

def podman_env(for_pull=False, environ=None):
    """Minimal environment for podman: never proxies (except for a registry pull) and never credentials."""
    environ = os.environ if environ is None else environ
    keep = PODMAN_ENV_KEEP + (PULL_ENV_EXTRA if for_pull else ())
    env = {k: environ[k] for k in keep if k in environ}
    for k in list(env):
        if image_inspect.SECRET_ENV.search(k):
            del env[k]
    return env


def _run(argv, *, env, stdin=None, timeout=600, runner=None):
    runner = runner or subprocess.run
    return runner(argv, input=stdin, capture_output=True, env=env, timeout=timeout)


# ── host facts and readiness ────────────────────────────────────────────────

def _subids(path, user, uid):
    ranges = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) == 3 and parts[0] in (user, str(uid)):
                    ranges.append((int(parts[1]), int(parts[2])))
    except (OSError, ValueError):
        pass
    return ranges


def _sysctl(name):
    try:
        with open(f"/proc/sys/{name.replace('.', '/')}", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def _cgroup_fstype():
    try:
        with open("/proc/self/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "/sys/fs/cgroup":
                    return parts[2]
    except OSError:
        pass
    return None


def gather_facts(runner=None):
    uid = os.getuid()
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    facts = {
        "uid": uid, "user": user, "machine": platform.machine(),
        "remote_env": sorted(k for k in REMOTE_ENV if os.environ.get(k)),
        "cgroup_fstype": _cgroup_fstype(),
        "delegated_controllers": None,
        "subuid": _subids("/etc/subuid", user, uid), "subgid": _subids("/etc/subgid", user, uid),
        "newuidmap": shutil.which("newuidmap"), "newgidmap": shutil.which("newgidmap"),
        "max_user_namespaces": _sysctl("user.max_user_namespaces"),
        "unprivileged_userns_clone": _sysctl("kernel.unprivileged_userns_clone"),
        "apparmor_restrict_unprivileged_userns": _sysctl("kernel.apparmor_restrict_unprivileged_userns"),
        "podman": shutil.which("podman"), "podman_version": None, "podman_info": None,
    }
    try:
        with open(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers",
                  encoding="ascii") as f:
            facts["delegated_controllers"] = sorted(f.read().split())
    except OSError:
        pass
    if facts["podman"]:
        env = podman_env()
        v = _run(["podman", "version", "--format", "json"], env=env, timeout=60, runner=runner)
        if v.returncode == 0:
            try:
                facts["podman_version"] = json.loads(v.stdout)["Client"]["Version"]
            except (ValueError, KeyError, TypeError):
                pass
        i = _run(["podman", "info", "--format", "json"], env=env, timeout=120, runner=runner)
        if i.returncode == 0:
            try:
                facts["podman_info"] = json.loads(i.stdout)
            except ValueError:
                pass
    return facts


def _version(text):
    m = re.match(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _maps_to(mapping, cid):
    for entry in mapping or []:
        try:
            c, h, n = int(entry["container_id"]), int(entry["host_id"]), int(entry["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if c <= cid < c + n:
            return h + (cid - c)
    return None


def evaluate_host(facts):
    """All problems that make this host unsuitable. Empty means suitable (still subject to the probe)."""
    p = []
    if facts["uid"] == 0:
        p.append("running as root (uid 0); a rootful runtime is not a substitute")
    if facts["machine"] not in OCI_ARCH:
        p.append(f"unsupported machine architecture {facts['machine']!r}")
    if facts["remote_env"]:
        p.append(f"remote container connection configured: {facts['remote_env']}")
    if facts["cgroup_fstype"] != "cgroup2":
        p.append(f"cgroup v2 is required (/sys/fs/cgroup is {facts['cgroup_fstype']!r})")
    need = {"cpu", "memory", "pids"}
    if not need <= set(facts["delegated_controllers"] or []):
        p.append(f"cgroup controllers {sorted(need)} are not delegated to user@{facts['uid']}.service: "
                 f"{facts['delegated_controllers']}")
    for kind in ("subuid", "subgid"):
        if not any(n >= SUBID_MIN for _, n in facts[kind]):
            p.append(f"/etc/{kind} has no range of at least {SUBID_MIN} for {facts['user']}: {facts[kind]}")
    for tool in ("newuidmap", "newgidmap"):
        if not facts[tool]:
            p.append(f"{tool} is not installed (package uidmap)")
    if (facts["max_user_namespaces"] or "0") in ("0", ""):
        p.append("user namespaces are disabled (user.max_user_namespaces)")
    if facts["unprivileged_userns_clone"] not in (None, "1"):
        p.append("unprivileged user namespaces are disabled (kernel.unprivileged_userns_clone)")
    if not facts["podman"]:
        p.append("podman is not installed")
        return p
    ver = _version(facts["podman_version"])
    if ver is None or ver < MIN_PODMAN:
        p.append(f"podman {facts['podman_version']} is older than {'.'.join(map(str, MIN_PODMAN))}")
    info = facts["podman_info"]
    if not isinstance(info, dict):
        p.append("podman info failed")
        return p
    host = info.get("host") or {}
    sec = host.get("security") or {}
    if sec.get("rootless") is not True:
        p.append(f"podman is not rootless (host.security.rootless={sec.get('rootless')!r})")
    if host.get("serviceIsRemote") is not False:
        p.append(f"podman talks to a remote service (host.serviceIsRemote={host.get('serviceIsRemote')!r})")
    if sec.get("seccompEnabled") is not True:
        p.append("seccomp is not enabled in podman")
    if host.get("cgroupVersion") != "v2":
        p.append(f"podman reports cgroup {host.get('cgroupVersion')!r}, not v2")
    if host.get("cgroupManager") != "systemd":
        p.append(f"podman cgroup manager is {host.get('cgroupManager')!r}; rootless limits need systemd")
    if not need <= set(host.get("cgroupControllers") or []):
        p.append(f"podman sees cgroup controllers {host.get('cgroupControllers')}, needs {sorted(need)}")
    if host.get("arch") != OCI_ARCH.get(facts["machine"]) or host.get("os") != "linux":
        p.append(f"podman host {host.get('os')}/{host.get('arch')} does not match {facts['machine']}")
    idm = host.get("idMappings") or {}
    for kind, ranges in (("uidmap", facts["subuid"]), ("gidmap", facts["subgid"])):
        mapping = idm.get(kind) or []
        if len(mapping) < 2:
            p.append(f"podman {kind} has {len(mapping)} entr{'y' if len(mapping) == 1 else 'ies'}: subordinate "
                     f"IDs are not usable (newuidmap/newgidmap or /etc/sub*id)")
        host_id = _maps_to(mapping, 10001)
        own = facts["uid"]
        if host_id is None or host_id in (0, own) or not any(s <= host_id < s + n for s, n in ranges):
            p.append(f"container id 10001 maps to host id {host_id} in {kind}, not into a subordinate range")
    if _maps_to(idm.get("uidmap"), 0) != facts["uid"]:
        p.append(f"container root maps to host uid {_maps_to(idm.get('uidmap'), 0)}, not to this user "
                 f"({facts['uid']}): not a rootless user namespace")
    return p


# ── container argv (the only place run options are defined) ─────────────────

def native_arch(machine=None):
    machine = machine or platform.machine()
    if machine not in OCI_ARCH:
        raise Refused([f"unsupported machine architecture {machine!r}"])
    return machine


def require_native(requested, machine=None):
    native = native_arch(machine)
    if requested is None:
        return native
    if requested not in OCI_ARCH:
        raise Refused([f"unknown architecture {requested!r}"])
    if requested != native:
        raise Refused([f"cross-architecture builds are not supported: requested {requested}, host is {native} "
                       "(no emulation has been verified)"])
    return requested


def container_run_argv(image, arch, *, entrypoint=None, args=(), stdin=False, mounts=(), out_tmpfs=False):
    oci = OCI_ARCH[arch]
    argv = ["podman", "run", "--rm", "--pull=never", f"--platform=linux/{oci}",
            "--network=none", "--ipc=none", "--read-only", "--read-only-tmpfs=false",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "10001:10001",
            "--http-proxy=false", "--env-host=false",
            f"--memory={LIMITS['memory']}", f"--memory-swap={LIMITS['memory']}",
            f"--pids-limit={LIMITS['pids']}", f"--cpus={LIMITS['cpus']}"]
    if out_tmpfs:
        argv += ["--tmpfs", "/out:rw,nosuid,nodev,noexec,size=64m,mode=1777"]
    for src, dst in mounts:
        argv += ["--mount", f"type=bind,source={src},destination={dst},readonly=true"]
    if stdin:
        argv.append("-i")
    if entrypoint is not None:
        argv += ["--entrypoint", entrypoint]
    return argv + [image, *args]


def build_argv(arch, context, tag):
    oci = OCI_ARCH[arch]
    return ["podman", "build", f"--platform=linux/{oci}", "--pull=never", "--network=none", "--no-cache",
            "--timestamp", "0", "--http-proxy=false", "--identity-label=false",
            "--file", os.path.join(context, "Containerfile"), "--tag", tag, context]


def pull_argv(arch):
    return ["podman", "pull", f"--platform=linux/{OCI_ARCH[arch]}", BASE_REF]


def image_tag(arch, lock_sha):
    return f"localhost/insightpilot-sandbox:p3.5a-{lock_sha[:12]}-{arch}"


# ── checks of podman output ─────────────────────────────────────────────────

def check_pulled_base(inspect_json, arch):
    p = []
    img = inspect_json[0] if isinstance(inspect_json, list) and inspect_json else {}
    plat = BASE["platforms"][OCI_ARCH[arch]]
    if img.get("Architecture") != OCI_ARCH[arch] or img.get("Os") != "linux":
        p.append(f"pulled base is {img.get('Os')}/{img.get('Architecture')}, not linux/{OCI_ARCH[arch]}")
    if "sha256:" + str(img.get("Id", "")) != plat["config"]:
        p.append(f"pulled base config {img.get('Id')} is not the verified {plat['config']}")
    return p


def check_image_config(inspect_json, arch, lock_sha):
    p = []
    img = inspect_json[0] if isinstance(inspect_json, list) and inspect_json else {}
    cfg = img.get("Config") or {}
    if img.get("Architecture") != OCI_ARCH[arch] or img.get("Os") != "linux":
        p.append(f"IMAGE: platform {img.get('Os')}/{img.get('Architecture')} is not linux/{OCI_ARCH[arch]}")
    if cfg.get("User") != EXPECT["user"]:
        p.append(f"IMAGE: User {cfg.get('User')!r}")
    if cfg.get("Entrypoint") != EXPECT["entrypoint"]:
        p.append(f"IMAGE: Entrypoint {cfg.get('Entrypoint')}")
    if (cfg.get("Cmd") or []) != EXPECT["cmd"]:
        p.append(f"IMAGE: Cmd {cfg.get('Cmd')}")
    if cfg.get("ExposedPorts"):
        p.append(f"IMAGE: ExposedPorts {cfg.get('ExposedPorts')}")
    if cfg.get("Volumes"):
        p.append(f"IMAGE: Volumes {cfg.get('Volumes')}")
    names = sorted(e.split("=", 1)[0] for e in cfg.get("Env") or [])
    if names != EXPECT["env_names_allowed"]:
        p.append(f"IMAGE: Env names {names}")
    labels = cfg.get("Labels") or {}
    if sorted(labels) != EXPECT["labels"]:
        p.append(f"IMAGE: labels {sorted(labels)} != {EXPECT['labels']}")
    if labels.get("io.insightpilot.lock.sha256") != lock_sha:
        p.append("IMAGE: lock label does not match requirements.lock")
    if labels.get("org.opencontainers.image.base.digest") != BASE["index_digest"]:
        p.append("IMAGE: base digest label does not match the verified base")
    return p


def check_id_mapping(rt, host_uid, subuid):
    """Inside the container, uid_map must place uid 10001 in a subordinate range, never at host root or this user."""
    nums = [int(x) for x in rt.get("uid_map") or [] if x.isdigit()]
    triples = [nums[i:i + 3] for i in range(0, len(nums) - len(nums) % 3, 3)]
    host = None
    for c, h, n in triples:
        if c <= 10001 < c + n:
            host = h + (10001 - c)
    if host is None or host in (0, host_uid) or not any(s <= host < s + n for s, n in subuid):
        return [f"RUNTIME: uid 10001 maps to host uid {host} (uid_map {rt.get('uid_map')})"]
    return []


def check_inert(result):
    p = []
    if result.returncode != EXPECT["inert_exit_status"]:
        p.append(f"INERT: exit status {result.returncode}, expected {EXPECT['inert_exit_status']}")
    if result.stdout:
        p.append(f"INERT: wrote to stdout: {result.stdout[:200]!r}")
    if result.stderr != (EXPECT["inert_message"] + "\n").encode():
        p.append(f"INERT: stderr {result.stderr[:300]!r}")
    return p


# ── operations ──────────────────────────────────────────────────────────────

def _image_env_names(image, runner=None):
    r = _run(["podman", "image", "inspect", image], env=podman_env(), timeout=60, runner=runner)
    if r.returncode != 0:
        raise Refused([f"cannot inspect {image}: {r.stderr[-300:]!r}"])
    data = json.loads(r.stdout)
    return data, sorted(e.split("=", 1)[0] for e in (data[0].get("Config") or {}).get("Env") or [])


def run_inspector(image, arch, *, runtime_only, runner=None):
    with open(os.path.join(HERE, "image_inspect.py"), "rb") as f:
        script = f.read()
    argv = container_run_argv(image, arch, entrypoint="/usr/local/bin/python3",
                              args=["-I", "-B", "-"] + (["--runtime-only"] if runtime_only else []), stdin=True)
    r = _run(argv, env=podman_env(), stdin=script, timeout=600, runner=runner)
    if r.returncode != 0:
        raise Refused([f"inspector container failed ({r.returncode}): {r.stderr[-500:]!r}"])
    return json.loads(r.stdout)


def ready(facts=None, *, probe=True, runner=None):
    """Host checks, then (optionally) a real enforcement probe on the pinned base. Returns evidence or raises."""
    facts = facts if facts is not None else gather_facts(runner=runner)
    problems = evaluate_host(facts)
    if problems:
        raise Refused(problems)
    evidence = {"facts": facts}
    if probe:
        arch = native_arch(facts["machine"])
        pull = _run(pull_argv(arch), env=podman_env(for_pull=True), timeout=1800, runner=runner)
        if pull.returncode != 0:
            raise Refused([f"base pull failed: {pull.stderr[-300:]!r}"])
        data, env_names = _image_env_names(BASE_REF, runner=runner)
        problems = check_pulled_base(data, arch)
        if problems:
            raise Refused(problems)
        report = run_inspector(BASE_REF, arch, runtime_only=True, runner=runner)
        problems = image_inspect.evaluate_runtime(report["runtime"], EXPECT, env_names) + \
            check_id_mapping(report["runtime"], facts["uid"], facts["subuid"])
        if problems:
            raise Refused(["the enforcement probe did not get the required isolation:"] + problems)
        evidence["probe"] = report["runtime"]
    return evidence


def build(wheel_cache, arch=None, *, facts=None, runner=None):
    facts = facts if facts is not None else gather_facts(runner=runner)
    arch = require_native(arch, facts["machine"])
    evidence = ready(facts, probe=True, runner=runner)
    lock_sha = hashlib.sha256(open(os.path.join(IMAGE_DIR, "requirements.lock"), "rb").read()).hexdigest()
    if f'io.insightpilot.lock.sha256="{lock_sha}"' not in open(os.path.join(IMAGE_DIR, "Containerfile")).read():
        raise Refused(["Containerfile lock label does not match requirements.lock"])
    with tempfile.TemporaryDirectory() as tmp:
        ctx = os.path.join(tmp, "context")
        files, ctx_digest = image_tool.stage(arch, wheel_cache, ctx)
        tag = image_tag(arch, lock_sha)
        started = time.monotonic()
        r = _run(build_argv(arch, ctx, tag), env=podman_env(), timeout=3600, runner=runner)
        seconds = time.monotonic() - started
        if r.returncode != 0:
            raise Refused([f"podman build failed: {r.stderr[-2000:]!r}"])
    data, _ = _image_env_names(tag, runner=runner)
    problems = check_image_config(data, arch, lock_sha)
    if problems:
        raise Refused(problems)
    img = data[0]
    evidence.update(tag=tag, image_id=img.get("Id"), digest=img.get("Digest"), size=img.get("Size"),
                    context_sha256=ctx_digest, context_files=len(files), build_seconds=round(seconds, 1))
    return evidence


def verify(image, *, facts=None, runner=None):
    facts = facts if facts is not None else gather_facts(runner=runner)
    problems = evaluate_host(facts)
    if problems:
        raise Refused(problems)
    arch = native_arch(facts["machine"])
    lock_sha = hashlib.sha256(open(os.path.join(IMAGE_DIR, "requirements.lock"), "rb").read()).hexdigest()
    data, env_names = _image_env_names(image, runner=runner)
    problems = check_image_config(data, arch, lock_sha)
    report = run_inspector(image, arch, runtime_only=False, runner=runner)
    lock_versions = {n: e["version"] for n, e in INVENTORY["packages"].items()}
    problems += image_inspect.evaluate(report, EXPECT, lock_versions, env_names)
    problems += check_id_mapping(report["runtime"], facts["uid"], facts["subuid"])
    inert = _run(container_run_argv(image, arch), env=podman_env(), timeout=120, runner=runner)
    problems += check_inert(inert)
    if problems:
        raise Refused(problems)
    return {"image": image, "image_id": data[0].get("Id"), "size": data[0].get("Size"), "report": report}


def percentile(samples, q):
    """Nearest-rank percentile (no interpolation), as reported."""
    if not samples:
        raise ValueError("no samples")
    s = sorted(samples)
    k = max(1, -(-q * len(s) // 100))          # ceil(q*n/100)
    return s[int(k) - 1]


def summarize(samples):
    return {"n": len(samples), "median_ms": round(statistics.median(samples) * 1000, 1),
            "p95_ms": round(percentile(samples, 95) * 1000, 1), "min_ms": round(min(samples) * 1000, 1),
            "max_ms": round(max(samples) * 1000, 1)}


def measure_startup(image, warm=30, cold=10, *, facts=None, runner=None, clock=time.perf_counter):
    """
    warm: consecutive runs of the inert container.
    cold: each sample follows `podman rmi` + `podman load` of the same image archive, so container
          storage and layers are freshly set up. The kernel page cache is NOT dropped (impossible
          rootless); that is stated with the result.
    """
    facts = facts if facts is not None else gather_facts(runner=runner)
    problems = evaluate_host(facts)
    if problems:
        raise Refused(problems)
    arch = native_arch(facts["machine"])
    argv = container_run_argv(image, arch)

    def timed():
        t0 = clock()
        r = _run(argv, env=podman_env(), timeout=120, runner=runner)
        dt = clock() - t0
        problems = check_inert(r)
        if problems:
            raise Refused(problems)
        return dt
    warm_s = [timed() for _ in range(warm)]
    cold_s = []
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, "image.tar")
        if cold:
            r = _run(["podman", "image", "save", "--format", "docker-archive", "-o", archive, image],
                     env=podman_env(), timeout=600, runner=runner)
            if r.returncode != 0:
                raise Refused([f"image save failed: {r.stderr[-300:]!r}"])
        for _ in range(cold):
            for cmd in (["podman", "rmi", "--force", image], ["podman", "load", "-i", archive]):
                r = _run(cmd, env=podman_env(), timeout=600, runner=runner)
                if r.returncode != 0:
                    raise Refused([f"{cmd[1]} failed: {r.stderr[-300:]!r}"])
            cold_s.append(timed())
    return {"warm": summarize(warm_s), "cold": summarize(cold_s) if cold_s else None,
            "cold_definition": "first run after podman rmi + podman load; page cache not dropped"}


# ── golden compatibility inside the image ───────────────────────────────────

FIXTURES = os.path.join(HERE, "golden_fixtures.json")
CHART_EPILOGUE = '''
# ── trusted P3.5b epilogue: report the saved chart (not fixture code) ──
import hashlib as _ip_h, os as _ip_o
from PIL import Image as _ip_I
if _ip_o.path.exists(CHART_PATH):
    with _ip_I.open(CHART_PATH) as _ip_im:
        _ip_im.load()
        print("CHARTINFO " + str(_ip_im.size[0]) + "x" + str(_ip_im.size[1]) + " " + _ip_im.mode + " "
              + _ip_h.sha256(_ip_im.tobytes()).hexdigest())
'''


def golden_scripts():
    """Wrap each pre-reviewed fixture exactly as V1 would, for /in/dataset.csv and /out. Refuses unknown code."""
    sys.path.insert(0, ROOT)
    import sandbox.executor as ex
    from agent.rules_engine import scan_code_safety
    from tests.v2.golden import reference as ref
    from tests.v2.golden import scenarios as sc
    manifest = _json(FIXTURES)
    out = []
    saved = ex.CHARTS_DIR
    try:
        ex.CHARTS_DIR = "/out"
        for fx in manifest["fixtures"]:
            scen = sc.SCENARIOS[fx["dataset"]]
            task = next(t for t in scen["tasks"] if t["id"] == fx["task_id"])
            code = scen["code"][task["title"]][-1]
            if hashlib.sha256(code.encode()).hexdigest() != fx["code_sha256"]:
                raise Refused([f"fixture {fx['task_id']} differs from the reviewed version"])
            if scan_code_safety(code):
                raise Refused([f"fixture {fx['task_id']} fails the V1 safety scan"])
            data = os.path.join(ref.DATA_DIR, scen["file"])
            if ref.sha256_file(data) != fx["dataset_sha256"]:
                raise Refused([f"dataset {scen['file']} differs from the approved copy"])
            script = ex.wrap_code_safely(code, "/in/dataset.csv", fx["task_id"]) + CHART_EPILOGUE
            out.append((fx, data, script))
    finally:
        ex.CHARTS_DIR = saved
    return out


def golden(image, *, facts=None, runner=None):
    facts = facts if facts is not None else gather_facts(runner=runner)
    problems = evaluate_host(facts)
    if problems:
        raise Refused(problems)
    arch = native_arch(facts["machine"])
    sys.path.insert(0, ROOT)
    from tests.v2.golden import reference as ref
    from tests.v2.golden.compare import mismatches, parse_golden
    results, printed = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o755)
        for fx, data, script in golden_scripts():
            copy = os.path.join(tmp, f"{fx['dataset']}.csv")
            if not os.path.exists(copy):
                shutil.copyfile(data, copy)
                os.chmod(copy, 0o444)
            argv = container_run_argv(image, arch, entrypoint="/usr/local/bin/python3", args=["-I", "-B", "-"],
                                      stdin=True, mounts=[(copy, "/in/dataset.csv")], out_tmpfs=True)
            r = _run(argv, env=podman_env(), stdin=script.encode(), timeout=300, runner=runner)
            stdout = r.stdout.decode("utf-8", "replace")
            stderr = r.stderr.decode("utf-8", "replace")
            chart = re.findall(r"^CHARTINFO (\d+)x(\d+) (\w+) ([0-9a-f]{64})$", stdout, re.M)
            entry = {"exit": r.returncode, "stderr_tail": stderr[-400:],
                     "matplotlib_cache_warning": "Matplotlib created a temporary" in stderr,
                     "chart": None}
            if chart:
                w, h, mode, sha = chart[0]
                entry["chart"] = {"size": [int(w), int(h)], "mode": mode, "pixels_sha256": sha}
            entry["chart_matches_reference"] = entry["chart"] == fx["chart"]
            printed.setdefault(fx["dataset"], {}).update(parse_golden(stdout))
            results[fx["task_id"]] = entry
    answers = {}
    for ds, got in printed.items():
        exp = ref.load_expected(ds)["answers"]
        answers[ds] = {"checked": len(exp), "mismatches": mismatches(got, exp), "missing": sorted(set(exp) - set(got))}
    ok = all(e["exit"] == 0 and e["chart_matches_reference"] and not e["matplotlib_cache_warning"]
             for e in results.values()) and all(not a["mismatches"] and not a["missing"] for a in answers.values())
    return {"ok": ok, "tasks": results, "answers": answers}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-host")
    c.add_argument("--probe", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("--wheel-cache", required=True)
    b.add_argument("--arch")
    for name in ("verify", "measure-startup", "golden"):
        s = sub.add_parser(name)
        s.add_argument("image")
        s.add_argument("--evidence")
        if name == "measure-startup":
            s.add_argument("--warm", type=int, default=30)
            s.add_argument("--cold", type=int, default=10)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check-host":
            facts = gather_facts()
            problems = evaluate_host(facts)
            print(json.dumps({k: v for k, v in facts.items() if k != "podman_info"}, indent=1, default=str))
            if problems:
                print("host suitable for P3.5b build and isolation tests: NO")
                print("\n".join("  - " + x for x in problems))
                return 1
            if a.probe:
                ready(facts, probe=True)
                print("enforcement probe: PASSED (container reported the required isolation)")
            print("host suitable for P3.5b build and isolation tests: YES" + ("" if a.probe else
                  " (facts only; run with --probe for proof of enforcement)"))
            return 0
        if a.cmd == "build":
            result = build(a.wheel_cache, a.arch)
        elif a.cmd == "verify":
            result = verify(a.image)
        elif a.cmd == "measure-startup":
            result = measure_startup(a.image, a.warm, a.cold)
        else:
            result = golden(a.image)
            if not result["ok"]:
                print(json.dumps(result, indent=1))
                return 1
    except Refused as exc:
        print(f"{a.cmd}: REFUSED", file=sys.stderr)
        for line in exc.problems:
            print("  - " + line, file=sys.stderr)
        return 2
    text = json.dumps(result, indent=1, sort_keys=True, default=str)
    if getattr(a, "evidence", None):
        with open(a.evidence, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
