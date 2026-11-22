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
  compare IMAGE_A IMAGE_B [--evidence F]
                                Reproducibility: image IDs, digests, RootFS layers; if layers
                                differ, a member-level diff of each differing layer pair.
  measure-startup IMAGE [--warm N] [--cold N]
  os-packages IMAGE [--evidence F]
                                Debian package inventory of the image (dpkg-query in
                                an isolated container), for the vulnerability review.
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
            # Without this, when the image has no entry for --user, Podman writes generated copies of
            # /etc/passwd and /etc/group and bind-mounts them into the container. GitHub run
            # 45001295990 (Podman 4.9.3) showed exactly these two host-side bind mounts, which the
            # strict mount allowlist correctly rejected. The flag keeps the allowlist strict.
            "--passwd=false",
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
                    created=img.get("Created"), rootfs_layers=(img.get("RootFS") or {}).get("Layers"),
                    lock_sha256=lock_sha, context_sha256=ctx_digest, context_files=len(files),
                    build_seconds=round(seconds, 1))
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


def _oci_layers(oci_dir):
    """Layer blob paths of an OCI image directory written by `podman image save --format oci-dir`."""
    def blob(digest):
        algo, hexd = digest.split(":", 1)
        return os.path.join(oci_dir, "blobs", algo, hexd)
    index = _json(os.path.join(oci_dir, "index.json"))
    manifest = _json(blob(index["manifests"][0]["digest"]))
    return [(layer["digest"], blob(layer["digest"])) for layer in manifest["layers"]]


def layer_members(path):
    """{name: (type, mode, uid, gid, size, mtime, linkname, sha256)} for one layer tar (any compression)."""
    import tarfile
    out = {}
    with tarfile.open(path, "r:*") as tar:
        for m in tar:
            digest = None
            if m.isfile():
                h = hashlib.sha256()
                f = tar.extractfile(m)
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
                digest = h.hexdigest()
            out[m.name] = (m.type.decode() if isinstance(m.type, bytes) else str(m.type), m.mode, m.uid, m.gid,
                           m.size, int(m.mtime), m.linkname, digest)
    return out


FIELDS = ("type", "mode", "uid", "gid", "size", "mtime", "linkname", "content")


def diff_members(a, b, examples=15):
    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    changed, by_field = [], {f: 0 for f in FIELDS}
    for name in sorted(set(a) & set(b)):
        diffs = [f for f, x, y in zip(FIELDS, a[name], b[name]) if x != y]
        if diffs:
            changed.append((name, diffs))
            for f in diffs:
                by_field[f] += 1
    return {"only_in_a": len(only_a), "only_in_b": len(only_b), "changed": len(changed),
            "changed_by_field": {k: v for k, v in by_field.items() if v},
            "examples_only_in_a": only_a[:examples], "examples_only_in_b": only_b[:examples],
            "examples_changed": [{"name": n, "fields": f, "a": list(a[n]), "b": list(b[n])}
                                 for n, f in changed[:examples]]}


def compare_images(image_a, image_b, *, facts=None, runner=None):
    facts = facts if facts is not None else gather_facts(runner=runner)
    problems = evaluate_host(facts)
    if problems:
        raise Refused(problems)
    info = {}
    for key, image in (("a", image_a), ("b", image_b)):
        data, _ = _image_env_names(image, runner=runner)
        img = data[0]
        info[key] = {"image": image, "id": img.get("Id"), "digest": img.get("Digest"), "size": img.get("Size"),
                     "created": img.get("Created"), "layers": (img.get("RootFS") or {}).get("Layers") or []}
    result = {"a": info["a"], "b": info["b"], "same_id": info["a"]["id"] == info["b"]["id"],
              "same_layers": info["a"]["layers"] == info["b"]["layers"], "layer_diffs": []}
    if not result["same_layers"]:
        with tempfile.TemporaryDirectory() as tmp:
            dirs = {}
            for key in ("a", "b"):
                dirs[key] = os.path.join(tmp, key)
                r = _run(["podman", "image", "save", "--format", "oci-dir", "-o", dirs[key], info[key]["image"]],
                         env=podman_env(), timeout=900, runner=runner)
                if r.returncode != 0:
                    raise Refused([f"image save failed: {r.stderr[-300:]!r}"])
            la, lb = _oci_layers(dirs["a"]), _oci_layers(dirs["b"])
            result["layer_count"] = [len(la), len(lb)]
            for i, ((da, pa), (db, pb)) in enumerate(zip(la, lb)):
                entry = {"index": i, "a": da, "b": db, "same": da == db}
                if da != db:
                    entry["diff"] = diff_members(layer_members(pa), layer_members(pb))
                result["layer_diffs"].append(entry)
    return result


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


def os_packages_argv(image, arch):
    return container_run_argv(image, arch, entrypoint="/usr/bin/dpkg-query",
                              args=["-W", "-f=${Package}\t${Version}\t${Architecture}\n"])


def os_packages(image, *, facts=None, runner=None):
    facts = facts if facts is not None else gather_facts(runner=runner)
    problems = evaluate_host(facts)
    if problems:
        raise Refused(problems)
    arch = native_arch(facts["machine"])
    r = _run(os_packages_argv(image, arch), env=podman_env(), timeout=120, runner=runner)
    if r.returncode != 0:
        raise Refused([f"dpkg-query failed: {r.stderr[-300:]!r}"])
    rows = [ln.split("\t") for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    return {"count": len(rows), "packages": [{"name": n, "version": v, "arch": a} for n, v, a in rows]}


# ── golden compatibility inside the image ───────────────────────────────────

FIXTURES = os.path.join(HERE, "golden_fixtures.json")
CHART_EPILOGUE = '''
# ── trusted P3.5b epilogue: report the saved chart and the render environment (not fixture code) ──
import hashlib as _ip_h, json as _ip_j, os as _ip_o
import matplotlib as _ip_m, matplotlib.font_manager as _ip_fm, PIL as _ip_pil
from matplotlib import ft2font as _ip_ft
from PIL import Image as _ip_I
if _ip_o.path.exists(CHART_PATH):
    with _ip_I.open(CHART_PATH) as _ip_im:
        _ip_im.load()
        print("CHARTINFO " + str(_ip_im.size[0]) + "x" + str(_ip_im.size[1]) + " " + _ip_im.mode + " "
              + _ip_h.sha256(_ip_im.tobytes()).hexdigest())
def _ip_rel(_p):
    _d = _ip_m.get_data_path()
    return "mpl-data:" + _ip_o.path.relpath(_p, _d) if _ip_o.path.abspath(_p).startswith(_d + _ip_o.sep) else _p
_ip_font = _ip_fm.findfont(_ip_fm.FontProperties(family=_ip_m.rcParams["font.family"]))
with open(_ip_font, "rb") as _ip_ff:
    _ip_fsha = _ip_h.sha256(_ip_ff.read()).hexdigest()
_ip_rc = _ip_m.rcParams
print("RENDERENV " + _ip_j.dumps({
    "matplotlib": _ip_m.__version__, "pillow": _ip_pil.__version__, "freetype": _ip_ft.__freetype_version__,
    "freetype_build": _ip_ft.__freetype_build_type__, "backend": _ip_m.get_backend(),
    "matplotlibrc": _ip_rel(_ip_m.matplotlib_fname()),
    "rc_env": sorted(k for k in _ip_o.environ if k.startswith(("MATPLOTLIB", "MPL"))),
    "system_fonts": len(_ip_fm.findSystemFonts()), "font_file": _ip_rel(_ip_font), "font_sha256": _ip_fsha,
    "font.family": list(_ip_rc["font.family"]), "font.sans-serif": list(_ip_rc["font.sans-serif"]),
    "text.hinting": str(_ip_rc["text.hinting"]), "text.hinting_factor": _ip_rc["text.hinting_factor"],
    "text.antialiased": bool(_ip_rc["text.antialiased"]), "figure.dpi": float(_ip_rc["figure.dpi"]),
    "savefig.dpi": str(_ip_rc["savefig.dpi"]), "figure.figsize": [float(x) for x in _ip_rc["figure.figsize"]],
}, sort_keys=True))
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


def parse_render_output(stdout):
    """(chart dict or None, render environment dict or None) from a golden run's stdout."""
    chart = re.findall(r"^CHARTINFO (\d+)x(\d+) (\w+) ([0-9a-f]{64})$", stdout, re.M)
    renv = re.findall(r"^RENDERENV (\{.*\})$", stdout, re.M)
    c = None
    if chart:
        w, h, mode, sha = chart[0]
        c = {"size": [int(w), int(h)], "mode": mode, "pixels_sha256": sha}
    try:
        r = json.loads(renv[-1]) if renv else None
    except ValueError:
        r = None
    return c, r


def check_render_env(renv, expected):
    """Exact comparison with the committed hermetic render environment; every difference is named."""
    if not isinstance(renv, dict):
        return ["render environment not reported"]
    return [f"render environment {k}: {renv.get(k)!r} != {expected.get(k)!r}"
            for k in sorted(set(expected) | set(renv)) if renv.get(k) != expected.get(k)]


def evaluate_golden_task(fx, entry, expected_env):
    """Acceptance for one fixture: exit 0, hermetic render environment, exact chart_hermetic match."""
    entry["render_env_problems"] = check_render_env(entry.get("render_env"), expected_env)
    entry["chart_matches_reference"] = entry["chart"] == fx["chart_hermetic"]
    entry["chart_matches_p35a_host_reference"] = entry["chart"] == fx["chart"]    # informational only
    return entry["exit"] == 0 and not entry["render_env_problems"] and entry["chart_matches_reference"] \
        and not entry["matplotlib_cache_warning"]


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
    expected_env = _json(FIXTURES)["render_environment"]
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
            chart, renv = parse_render_output(stdout)
            entry = {"exit": r.returncode, "stderr_tail": stderr[-400:],
                     "matplotlib_cache_warning": "Matplotlib created a temporary" in stderr,
                     "chart": chart, "render_env": renv}
            entry["ok"] = evaluate_golden_task(fx, entry, expected_env)
            printed.setdefault(fx["dataset"], {}).update(parse_golden(stdout))
            results[fx["task_id"]] = entry
    answers = {}
    for ds, got in printed.items():
        exp = ref.load_expected(ds)["answers"]
        answers[ds] = {"checked": len(exp), "mismatches": mismatches(got, exp), "missing": sorted(set(exp) - set(got))}
    charts_ok = all(e["ok"] for e in results.values())
    numbers_ok = all(not a["mismatches"] and not a["missing"] for a in answers.values())
    return {"ok": charts_ok and numbers_ok and len(results) == len(_json(FIXTURES)["fixtures"]), "numerical_ok": numbers_ok,
            "charts_and_render_env_ok": charts_ok, "tasks": results, "answers": answers,
            "expected_render_environment": expected_env}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-host")
    c.add_argument("--probe", action="store_true")
    c.add_argument("--evidence", help="with --probe: write the probe's runtime report (JSON) here")
    b = sub.add_parser("build")
    b.add_argument("--wheel-cache", required=True)
    b.add_argument("--arch")
    b.add_argument("--evidence")
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("image_a")
    cmp_.add_argument("image_b")
    cmp_.add_argument("--evidence")
    for name in ("verify", "measure-startup", "golden", "os-packages"):
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
                evidence = ready(facts, probe=True)
                if a.evidence:
                    with open(a.evidence, "w", encoding="utf-8") as f:
                        json.dump(evidence["probe"], f, indent=1, sort_keys=True, default=str)
                print("enforcement probe: PASSED (container reported the required isolation)")
            print("host suitable for P3.5b build and isolation tests: YES" + ("" if a.probe else
                  " (facts only; run with --probe for proof of enforcement)"))
            return 0
        if a.cmd == "build":
            result = build(a.wheel_cache, a.arch)
        elif a.cmd == "verify":
            result = verify(a.image)
        elif a.cmd == "compare":
            result = compare_images(a.image_a, a.image_b)
        elif a.cmd == "measure-startup":
            result = measure_startup(a.image, a.warm, a.cold)
        elif a.cmd == "os-packages":
            result = os_packages(a.image)
        else:
            result = golden(a.image)
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
    if a.cmd == "golden" and not result["ok"]:
        return 1                                  # evidence is written either way
    return 0


if __name__ == "__main__":
    sys.exit(main())
