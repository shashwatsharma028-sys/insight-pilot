"""
P3.5b Phase A: the rootless build and validation driver, tested without a container runtime.

Proved here, offline:
- platform selection;
- refusal of remote, rootful, cgroup-v1 and unmapped hosts;
- proxy and credential stripping;
- the runtime-isolation evaluation, which fails closed for every field;
- ordering: an enforcement probe runs before any build.

The real enforcement (whether podman actually produces these properties) can
only be shown on a validated rootless host (VE-2), and it was NOT shown here.
"""

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPTS = os.path.join(ROOT, "scripts", "p3")
sys.path.insert(0, SCRIPTS)
_spec = importlib.util.spec_from_file_location("p35b_rootless", os.path.join(SCRIPTS, "rootless.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)
ii = rl.image_inspect

EXPECT = rl.EXPECT
BASE = rl.BASE
BASE_ENV = sorted(e.split("=", 1)[0] for e in json.load(open(os.path.join(
    ROOT, "v2", "execution", "image", "base", "config-amd64.json")))["config"]["Env"])
IMAGE_ENV = EXPECT["env_names_allowed"]
LOCK_SHA = hashlib.sha256(open(os.path.join(ROOT, "v2", "execution", "image", "requirements.lock"), "rb").read()).hexdigest()

MAP = [{"container_id": 0, "host_id": 1001, "size": 1}, {"container_id": 1, "host_id": 100000, "size": 65536}]
GOOD_FACTS = {
    "uid": 1001, "user": "ipval", "machine": "x86_64", "remote_env": [], "cgroup_fstype": "cgroup2",
    "delegated_controllers": ["cpu", "cpuset", "io", "memory", "pids"],
    "subuid": [(100000, 65536)], "subgid": [(100000, 65536)],
    "newuidmap": "/usr/bin/newuidmap", "newgidmap": "/usr/bin/newgidmap", "max_user_namespaces": "63353",
    "unprivileged_userns_clone": None, "apparmor_restrict_unprivileged_userns": "1",
    "podman": "/usr/bin/podman", "podman_version": "4.9.3",
    "podman_info": {"host": {"arch": "amd64", "os": "linux", "cgroupVersion": "v2", "cgroupManager": "systemd",
                             "cgroupControllers": ["cpu", "io", "memory", "pids"], "serviceIsRemote": False,
                             "security": {"rootless": True, "seccompEnabled": True},
                             "idMappings": {"uidmap": MAP, "gidmap": MAP}}},
}


def facts(**changes):
    f = copy.deepcopy(GOOD_FACTS)
    for path, value in changes.items():
        target = f
        keys = path.split("__")
        for k in keys[:-1]:
            target = target[k]
        target[keys[-1]] = value
    return f


def good_runtime(env=BASE_ENV):
    rt = {
        "uid": 10001, "gid": 10001, "euid": 10001, "egid": 10001, "groups": [],
        "no_new_privs": "1", "seccomp": "2", "caps": {c: "0000000000000000" for c in ii.CAP_FIELDS},
        "uid_map": ["0", "1001", "1", "1", "100000", "65536"], "gid_map": ["0", "1001", "1", "1", "100000", "65536"],
        "env_names": sorted(set(env) | {"HOSTNAME", "container", "HOME"}),
        "net_interfaces": ["lo"], "routes": [],
        "mounts": [
            {"mountpoint": "/", "options": ["ro", "relatime"], "fstype": "overlay", "source": "overlay",
             "super_options": ["rw"]},
            {"mountpoint": "/proc", "options": ["rw", "nosuid", "nodev", "noexec"], "fstype": "proc",
             "source": "proc", "super_options": ["rw"]},
            {"mountpoint": "/dev", "options": ["rw", "nosuid"], "fstype": "tmpfs", "source": "tmpfs",
             "super_options": ["rw", "size=65536k", "mode=755"]},
            {"mountpoint": "/sys", "options": ["ro", "nosuid", "nodev", "noexec"], "fstype": "sysfs",
             "source": "sysfs", "super_options": ["ro"]},
            {"mountpoint": "/tmp", "options": ["rw", "nosuid", "nodev", "noexec", "relatime"], "fstype": "tmpfs",
             "source": "tmpfs", "super_options": ["rw", "size=65536k", "mode=1777"]},
            {"mountpoint": "/etc/hosts", "options": ["rw", "nosuid", "nodev"], "fstype": "tmpfs", "source": "tmpfs",
             "super_options": ["rw"]},
            {"mountpoint": "/proc/kcore", "options": ["ro"], "fstype": "tmpfs", "source": "tmpfs",
             "super_options": ["ro", "size=0k"]},
        ],
        "cgroup": dict(EXPECT["runtime"]["cgroup"]),
        "write_probes": {**{d: "EROFS" for d in EXPECT["runtime"]["must_not_be_writable"]}, "/tmp": "writable",
                         "/dev/shm": "ENOENT"},
        "file_write_probes": {f: "EACCES" for f in EXPECT["runtime"]["files_must_not_open_for_write"]},
    }
    return rt


# ── host readiness ──────────────────────────────────────────────────────────

def test_a_well_configured_rootless_host_passes_the_fact_checks():
    assert rl.evaluate_host(GOOD_FACTS) == []


HOST_DEFECTS = [
    ("root", dict(uid=0), "running as root"),
    ("remote_env", dict(remote_env=["CONTAINER_HOST"]), "remote container connection"),
    ("docker_host", dict(remote_env=["DOCKER_HOST"]), "remote container connection"),
    ("service_remote", dict(podman_info__host__serviceIsRemote=True), "remote service"),
    ("service_remote_unknown", dict(podman_info__host__serviceIsRemote=None), "remote service"),
    ("not_rootless", dict(podman_info__host__security__rootless=False), "not rootless"),
    ("rootless_unknown", dict(podman_info__host__security__rootless=None), "not rootless"),
    ("cgroup_v1_fs", dict(cgroup_fstype="tmpfs"), "cgroup v2 is required"),
    ("cgroup_v1_podman", dict(podman_info__host__cgroupVersion="v1"), "not v2"),
    ("cgroupfs_manager", dict(podman_info__host__cgroupManager="cgroupfs"), "need systemd"),
    ("not_delegated", dict(delegated_controllers=["cpu", "pids"]), "not delegated"),
    ("delegation_unknown", dict(delegated_controllers=None), "not delegated"),
    ("podman_missing_pids", dict(podman_info__host__cgroupControllers=["cpu", "memory"]), "needs"),
    ("no_subuid", dict(subuid=[]), "/etc/subuid"),
    ("small_subuid", dict(subuid=[(100000, 1000)]), "/etc/subuid"),
    ("no_subgid", dict(subgid=[]), "/etc/subgid"),
    ("no_newuidmap", dict(newuidmap=None), "newuidmap"),
    ("no_newgidmap", dict(newgidmap=None), "newgidmap"),
    ("single_uidmap", dict(podman_info__host__idMappings__uidmap=[MAP[0]]), "not usable"),
    ("10001_not_subordinate", dict(podman_info__host__idMappings__uidmap=[
        MAP[0], {"container_id": 1, "host_id": 1, "size": 65536}]), "not into a subordinate range"),
    ("rootful_mapping", dict(podman_info__host__idMappings__uidmap=[
        {"container_id": 0, "host_id": 0, "size": 4294967295}]), "not a rootless user namespace"),
    ("old_podman", dict(podman_version="4.3.1"), "older than"),
    ("podman_missing", dict(podman=None), "not installed"),
    ("info_failed", dict(podman_info=None), "info failed"),
    ("seccomp_off", dict(podman_info__host__security__seccompEnabled=False), "seccomp"),
    ("arch_mismatch", dict(machine="aarch64"), "does not match"),
    ("userns_off", dict(max_user_namespaces="0"), "user namespaces are disabled"),
    ("userns_clone_off", dict(unprivileged_userns_clone="0"), "unprivileged user namespaces"),
    ("riscv", dict(machine="riscv64"), "unsupported machine"),
]


@pytest.mark.parametrize("name,changes,expected", HOST_DEFECTS, ids=[d[0] for d in HOST_DEFECTS])
def test_each_host_defect_is_refused(name, changes, expected):
    problems = rl.evaluate_host(facts(**changes))
    assert any(expected in p for p in problems), problems


def test_this_environment_is_not_a_validation_host_when_it_is_root_or_cgroup_v1():
    live = rl.gather_facts()
    if live["uid"] == 0 or live["cgroup_fstype"] != "cgroup2" or not live["podman"]:
        problems = rl.evaluate_host(live)
        assert problems and any("root" in p or "cgroup" in p or "podman" in p for p in problems)


# ── platform selection: native only, and every layer must agree ─────────────

def test_native_architecture_is_selected_explicitly_everywhere():
    for machine, oci in (("x86_64", "amd64"), ("aarch64", "arm64")):
        assert rl.require_native(None, machine) == machine
        assert f"--platform=linux/{oci}" in rl.build_argv(machine, "/ctx", "t")
        assert f"--platform=linux/{oci}" in rl.pull_argv(machine)
        assert f"--platform=linux/{oci}" in rl.container_run_argv("img", machine)


@pytest.mark.parametrize("machine,requested", [("x86_64", "aarch64"), ("aarch64", "x86_64")])
def test_cross_architecture_builds_are_refused(machine, requested):
    with pytest.raises(rl.Refused) as info:
        rl.require_native(requested, machine)
    assert "cross-architecture" in info.value.problems[0]


@pytest.mark.parametrize("machine,requested", [("riscv64", None), ("x86_64", "amd64"), ("x86_64", "ppc64le")])
def test_unknown_architectures_are_refused(machine, requested):
    with pytest.raises(rl.Refused):
        rl.require_native(requested, machine)


def _inspect(arch="amd64", ident=None, cfg=None):
    return [{"Id": ident or BASE["platforms"]["amd64"]["config"][7:], "Architecture": arch, "Os": "linux",
             "Config": cfg or {}}]


def test_pulled_base_must_be_the_verified_platform_config():
    assert rl.check_pulled_base(_inspect(), "x86_64") == []
    assert any("not linux/amd64" in p for p in rl.check_pulled_base(_inspect(arch="arm64"), "x86_64"))
    arm_config = BASE["platforms"]["arm64"]["config"][7:]
    assert any("not the verified" in p for p in rl.check_pulled_base(_inspect(ident=arm_config), "x86_64"))
    assert rl.check_pulled_base([], "x86_64")


def _good_image_inspect(**over):
    cfg = {"User": "10001:10001", "Entrypoint": EXPECT["entrypoint"], "Cmd": None,
           "Env": [f"{n}=x" for n in IMAGE_ENV],
           "Labels": {"io.insightpilot.lock.sha256": LOCK_SHA, "io.insightpilot.stage": "P3.5a-inert",
                      "org.opencontainers.image.base.digest": BASE["index_digest"],
                      "org.opencontainers.image.base.name": BASE["reference"],
                      "org.opencontainers.image.title": "insightpilot-sandbox"}}
    cfg.update(over.pop("cfg", {}))
    img = {"Id": "f" * 64, "Architecture": "amd64", "Os": "linux", "Config": cfg, "Size": 1}
    img.update(over)
    return [img]


@pytest.mark.parametrize("mutate,expected", [
    (dict(Architecture="arm64"), "platform"),
    (dict(cfg={"User": "0"}), "User"),
    (dict(cfg={"User": ""}), "User"),
    (dict(cfg={"Entrypoint": ["/usr/local/bin/python3"]}), "Entrypoint"),
    (dict(cfg={"Cmd": ["python3"]}), "Cmd"),
    (dict(cfg={"ExposedPorts": {"8501/tcp": {}}}), "ExposedPorts"),
    (dict(cfg={"Volumes": {"/data": {}}}), "Volumes"),
    (dict(cfg={"Env": [f"{n}=x" for n in IMAGE_ENV] + ["HTTPS_PROXY=x"]}), "Env names"),
])
def test_built_image_config_is_checked(mutate, expected):
    assert rl.check_image_config(_good_image_inspect(), "x86_64", LOCK_SHA) == []
    problems = rl.check_image_config(_good_image_inspect(**mutate), "x86_64", LOCK_SHA)
    assert any(expected in p for p in problems), problems


def test_an_identity_label_would_be_caught_and_is_disabled_at_build():
    labels = dict(_good_image_inspect()[0]["Config"]["Labels"], **{"io.buildah.version": "1.33"})
    problems = rl.check_image_config(_good_image_inspect(cfg={"Labels": labels}), "x86_64", LOCK_SHA)
    assert any("labels" in p for p in problems)
    assert "--identity-label=false" in rl.build_argv("x86_64", "/ctx", "t")


# ── container options: isolation flags present, dangerous ones absent ───────

REQUIRED_RUN = ["--rm", "--pull=never", "--network=none", "--ipc=none", "--read-only", "--read-only-tmpfs=false",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--passwd=false", "--http-proxy=false",
                "--env-host=false",
                "--memory=256m", "--memory-swap=256m", "--pids-limit=64", "--cpus=1"]
FORBIDDEN_RUN = ["--privileged", "--network=host", "--net=host", "--userns=host", "--userns=keep-id", "--pid=host",
                 "--ipc=host", "--env-host=true", "--http-proxy=true", "-e", "--env", "--env-file", "-v", "--volume",
                 "--cap-add", "seccomp=unconfined", "label=disable", "--device", "--group-add", "--secret",
                 "--uts=host", "--cgroupns=host", "--systemd=always", "--init",
                 "--passwd", "--passwd=true", "--passwd-entry", "--group-entry"]


@pytest.mark.parametrize("kwargs", [{}, {"entrypoint": "/usr/local/bin/python3", "args": ["-I", "-B", "-"],
                                         "stdin": True},
                                    {"mounts": [("/tmp/x.csv", "/in/dataset.csv")], "out_tmpfs": True}])
def test_every_container_gets_the_isolation_options(kwargs):
    argv = rl.container_run_argv("localhost/img", "x86_64", **kwargs)
    for flag in REQUIRED_RUN:
        assert flag in argv, flag
    for flag in FORBIDDEN_RUN:
        assert flag not in argv, flag
    assert argv[argv.index("--user") + 1] == "10001:10001"
    tmp = argv[argv.index("--tmpfs") + 1]
    assert tmp.startswith("/tmp:") and {"nosuid", "nodev", "noexec"} <= set(tmp.split(":")[1].split(",")) \
        and "size=64m" in tmp
    for i, a in enumerate(argv):
        if a == "--mount":
            assert argv[i + 1].endswith(",readonly=true") and argv[i + 1].startswith("type=bind,")
    if kwargs.get("out_tmpfs"):
        out = [argv[i + 1] for i, a in enumerate(argv) if a == "--tmpfs" and argv[i + 1].startswith("/out:")]
        assert out and "size=64m" in out[0] and "noexec" in out[0]
    assert argv.index("localhost/img") > max(argv.index(f) for f in REQUIRED_RUN)


def _all_run_argvs():
    yield rl.container_run_argv("localhost/img", "x86_64")
    yield rl.container_run_argv("localhost/img", "aarch64", entrypoint="/usr/local/bin/python3",
                                args=["-I", "-B", "-"], stdin=True)
    yield rl.container_run_argv("localhost/img", "x86_64", mounts=[("/tmp/x.csv", "/in/dataset.csv")],
                                out_tmpfs=True)
    yield rl.os_packages_argv("localhost/img", "x86_64")


def test_podman_is_told_never_to_inject_passwd_or_group_files():
    for argv in _all_run_argvs():
        assert argv.count("--passwd=false") == 1
        assert argv.index("--passwd=false") < argv.index("localhost/img")      # an option, not an argument
        assert not [a for a in argv if a.startswith(("--passwd-entry", "--group-entry")) or a == "--passwd"]


RUN1_INJECTED = [  # GitHub run 45001295990 (Podman 4.9.3, ubuntu-24.04): the probe's only failures
    {"mountpoint": "/etc/passwd", "options": ["rw", "relatime"], "fstype": "ext4", "source": "/dev/mapper/cow-root",
     "super_options": ["rw"]},
    {"mountpoint": "/etc/group", "options": ["rw", "relatime"], "fstype": "ext4", "source": "/dev/mapper/cow-root",
     "super_options": ["rw"]},
]


def test_run1_regression_injected_passwd_and_group_mounts_stay_rejected():
    for point in ("/etc/passwd", "/etc/group"):
        assert point not in EXPECT["runtime"]["allowed_mountpoints"]
        assert point not in EXPECT["runtime"]["rw_mountpoints_allowed"]
        assert not point.startswith(tuple(EXPECT["runtime"]["allowed_mount_prefixes"]))
    rt = good_runtime()
    rt["mounts"] += copy.deepcopy(RUN1_INJECTED)
    problems = ii.evaluate_runtime(rt, EXPECT, BASE_ENV)
    assert any("unexpected mount /etc/passwd (ext4 from /dev/mapper/cow-root)" in p for p in problems)
    assert any("unexpected mount /etc/group (ext4 from /dev/mapper/cow-root)" in p for p in problems)
    assert any("writable mount /etc/passwd" in p for p in problems)


def test_removing_the_passwd_flag_is_detected():
    """Self-mutation: a copy of rootless.py without the flag must fail the required-option check."""
    src = open(os.path.join(SCRIPTS, "rootless.py"), encoding="utf-8").read()
    assert src.count('"--passwd=false",') == 1
    mutant_src = src.replace('"--passwd=false",', "", 1)
    mutant = types.ModuleType("p35b_rootless_mutant")
    mutant.__file__ = os.path.join(SCRIPTS, "rootless.py")
    exec(compile(mutant_src, mutant.__file__, "exec"), mutant.__dict__)
    argv = mutant.container_run_argv("localhost/img", "x86_64")
    missing = [f for f in REQUIRED_RUN if f not in argv]
    assert missing == ["--passwd=false"]
    with pytest.raises(AssertionError, match="--passwd=false"):
        _check_required(argv)
    _check_required(rl.container_run_argv("localhost/img", "x86_64"))      # the real builder passes


def _check_required(argv):
    for flag in REQUIRED_RUN:
        assert flag in argv, flag


def test_check_host_evidence_option_exists_and_writes_nothing_when_refused(tmp_path):
    out = tmp_path / "probe.json"
    if rl.evaluate_host(rl.gather_facts()):
        assert rl.main(["check-host", "--probe", "--evidence", str(out)]) == 1
        assert not out.exists()


def test_build_options_are_offline_hermetic_and_proxy_free():
    argv = rl.build_argv("x86_64", "/ctx", "localhost/t")
    for flag in ("--platform=linux/amd64", "--pull=never", "--network=none", "--no-cache", "--http-proxy=false",
                 "--identity-label=false"):
        assert flag in argv, flag
    assert argv[argv.index("--timestamp") + 1] == "0"
    assert not any(a.startswith(("--build-arg", "--volume", "-v", "--secret", "--env")) for a in argv)
    assert argv[-1] == "/ctx" and argv[argv.index("--file") + 1] == "/ctx/Containerfile"


def test_pull_reference_is_exactly_the_containerfile_from_reference():
    cf = open(os.path.join(ROOT, "v2", "execution", "image", "Containerfile")).read()
    assert f"FROM {rl.BASE_REF} AS build" in cf and f"FROM {rl.BASE_REF} AS runtime" in cf
    assert rl.pull_argv("x86_64")[-1] == rl.BASE_REF


def test_podman_environment_drops_proxies_credentials_and_remote_settings():
    environ = {"PATH": "/usr/bin", "HOME": "/home/ipval", "XDG_RUNTIME_DIR": "/run/user/1001",
               "HTTP_PROXY": "http://p", "https_proxy": "http://p", "HTTPS_PROXY": "http://p", "NO_PROXY": "x",
               "ALL_PROXY": "x", "GOOGLE_API_KEY": "k", "REGISTRY_AUTH_FILE": "/a.json",
               "CONTAINER_HOST": "ssh://x", "DOCKER_HOST": "tcp://x", "AWS_SECRET_ACCESS_KEY": "s"}
    env = rl.podman_env(environ=environ)
    assert env == {"PATH": "/usr/bin", "HOME": "/home/ipval", "XDG_RUNTIME_DIR": "/run/user/1001"}
    pull = rl.podman_env(for_pull=True, environ=environ)
    assert set(pull) == {"PATH", "HOME", "XDG_RUNTIME_DIR", "https_proxy", "HTTPS_PROXY", "NO_PROXY"}
    assert not any(ii.SECRET_ENV.search(k) for k in pull)


# ── runtime-enforced properties: verified, never just recorded ──────────────

def test_a_fully_isolated_runtime_report_passes():
    assert ii.evaluate_runtime(good_runtime(), EXPECT, BASE_ENV) == []


RUNTIME_DEFECTS = [
    ("nnp_off", dict(no_new_privs="0"), "NoNewPrivs"),
    ("nnp_missing", dict(no_new_privs=KeyError), "NoNewPrivs"),
    ("seccomp_off", dict(seccomp="0"), "seccomp"),
    ("seccomp_missing", dict(seccomp=None), "seccomp"),
    ("cap_eff", dict(caps__CapEff="00000000a80425fb"), "CapEff"),
    ("cap_prm", dict(caps__CapPrm="0000000000000400"), "CapPrm"),
    ("cap_bnd", dict(caps__CapBnd="00000000a80425fb"), "CapBnd"),
    ("cap_amb_missing", dict(caps__CapAmb=KeyError), "CapAmb"),
    ("cap_garbage", dict(caps__CapInh="zz"), "CapInh"),
    ("uid_root", dict(uid=0, euid=0), "credentials"),
    ("gid_root", dict(gid=0), "credentials"),
    ("groups_root", dict(groups=[0]), "supplementary groups"),
    ("eth0", dict(net_interfaces=["eth0", "lo"]), "network interfaces"),
    ("net_unreadable", dict(net_interfaces=None), "network interfaces"),
    ("routes", dict(routes=["eth0\t00000000\t010200C0"]), "routes"),
    ("routes_unreadable", dict(routes=None), "routes"),
    ("root_rw", dict(mounts__0__options=["rw", "relatime"]), "not read-only"),
    ("no_mounts", dict(mounts=[]), "not read-only"),
    ("tmp_exec", dict(mounts__4__options=["rw", "nosuid", "nodev"]), "/tmp is not tmpfs"),
    ("tmp_unbounded", dict(mounts__4__super_options=["rw", "mode=1777"]), "no size bound"),
    ("tmp_not_tmpfs", dict(mounts__4__fstype="ext4"), "/tmp is not tmpfs"),
    ("tmp_not_writable", dict(write_probes__1tmp="EROFS"), "/tmp must be writable"),
    ("opt_writable", dict(write_probes__opt="writable"), "/opt/ip must not be writable"),
    ("root_writable", dict(write_probes__root="writable"), "/ must not be writable"),
    ("probe_missing", dict(write_probes={}), "must not be writable"),
    ("passwd_writable", dict(file_write_probes__passwd="writable"), "/etc/passwd could be opened"),
    ("file_probes_missing", dict(file_write_probes={}), "could be opened"),
    ("memory_unlimited", dict(cgroup__memory_max="max"), "memory.max"),
    ("pids_missing", dict(cgroup__pids_max=None), "pids.max"),
    ("cpu_unlimited", dict(cgroup__cpu_max="max 100000"), "cpu.max"),
    ("swap", dict(cgroup__memory_swap_max="max"), "memory.swap.max"),
    ("proxy_env", dict(env_names=sorted(BASE_ENV + ["HOSTNAME", "https_proxy"])), "proxy variables"),
    ("proxy_env_upper", dict(env_names=sorted(BASE_ENV + ["HTTP_PROXY"])), "proxy variables"),
    ("secret_env", dict(env_names=sorted(BASE_ENV + ["GOOGLE_API_KEY"])), "secret-like"),
    ("unknown_env", dict(env_names=sorted(BASE_ENV + ["SSH_AUTH_SOCK"])), "unexpected environment"),
    ("env_missing", dict(env_names=None), "environment names missing"),
]
_ALIASES = {"1tmp": "/tmp", "opt": "/opt/ip", "root": "/", "passwd": "/etc/passwd",
            "memory_max": "memory.max", "pids_max": "pids.max", "cpu_max": "cpu.max",
            "memory_swap_max": "memory.swap.max"}


@pytest.mark.parametrize("name,changes,expected", RUNTIME_DEFECTS, ids=[d[0] for d in RUNTIME_DEFECTS])
def test_each_runtime_isolation_defect_is_reported(name, changes, expected):
    rt = good_runtime()
    for path, value in changes.items():
        keys = [_ALIASES.get(k, k) for k in path.split("__")]
        target = rt
        for k in keys[:-1]:
            target = target[int(k)] if isinstance(target, list) else target[k]
        if value is KeyError:
            del target[keys[-1]]
        else:
            target[keys[-1]] = value
    problems = ii.evaluate_runtime(rt, EXPECT, BASE_ENV)
    assert any(expected in p for p in problems), problems
    assert all(p.startswith("RUNTIME: ") for p in problems)


def test_unexpected_or_writable_mounts_are_reported():
    rt = good_runtime()
    rt["mounts"].append({"mountpoint": "/data", "options": ["rw"], "fstype": "ext4", "source": "/dev/sda1",
                         "super_options": ["rw"]})
    rt["mounts"].append({"mountpoint": "/in/dataset.csv", "options": ["rw"], "fstype": "ext4", "source": "/dev/sda1",
                         "super_options": ["rw"]})
    problems = ii.evaluate_runtime(rt, EXPECT, BASE_ENV)
    assert any("unexpected mount /data" in p for p in problems)
    assert any("writable mount /in/dataset.csv" in p for p in problems)


def test_a_later_mount_over_root_decides_the_visible_options():
    rt = good_runtime()
    rt["mounts"].append({"mountpoint": "/", "options": ["rw"], "fstype": "overlay", "source": "overlay",
                         "super_options": ["rw"]})
    assert any("not read-only" in p for p in ii.evaluate_runtime(rt, EXPECT, BASE_ENV))


def test_live_negative_control_this_unisolated_process_fails_the_runtime_evaluation(monkeypatch):
    """Real /proc data from this (unisolated) host must not pass. Write probes are simulated, not performed."""
    monkeypatch.setattr(ii, "_write_probe", lambda d: "writable")
    monkeypatch.setattr(ii, "_file_write_probe", lambda f: "writable")
    rt = ii.inspect_runtime()
    problems = ii.evaluate_runtime(rt, EXPECT, BASE_ENV)
    if rt["no_new_privs"] != "1":
        assert any("NoNewPrivs" in p for p in problems)
    assert any("must not be writable" in p for p in problems)
    assert len(problems) >= 3, problems


def test_mountinfo_parsing_handles_escapes_and_optional_fields():
    text = ("36 35 98:0 /mnt1 /mnt\\040with\\040space rw,noatime master:1 - ext3 /dev/root rw,errors=continue\n"
            "40 36 0:5 / /tmp rw,nosuid,nodev,noexec shared:2 unbindable - tmpfs tmpfs rw,size=65536k,mode=1777\n"
            "garbage line\n")
    m = ii.parse_mountinfo(text)
    assert [x["mountpoint"] for x in m] == ["/mnt with space", "/tmp"]
    assert m[1]["fstype"] == "tmpfs" and "size=65536k" in m[1]["super_options"]


def test_container_id_mapping_must_land_in_a_subordinate_range():
    rt = good_runtime()
    assert rl.check_id_mapping(rt, 1001, [(100000, 65536)]) == []
    rt["uid_map"] = ["0", "0", "4294967295"]                                  # rootful
    assert rl.check_id_mapping(rt, 1001, [(100000, 65536)])
    rt["uid_map"] = ["0", "1001", "1", "10001", "1001", "1"]                  # keep-id style: 10001 is the host user
    assert rl.check_id_mapping(rt, 1001, [(100000, 65536)])
    rt["uid_map"] = []
    assert rl.check_id_mapping(rt, 1001, [(100000, 65536)])


def test_inert_output_must_be_exact():
    ok = types.SimpleNamespace(returncode=78, stdout=b"", stderr=(EXPECT["inert_message"] + "\n").encode())
    assert rl.check_inert(ok) == []
    for bad in (dict(returncode=0), dict(stdout=b"x"), dict(stderr=b"other\n"), dict(returncode=78, stderr=b"")):
        assert rl.check_inert(types.SimpleNamespace(**{**vars(ok), **bad}))


# ── orchestration with a fake podman: order, environment, refusal paths ─────

class FakePodman:
    def __init__(self, runtime=None, base_arch="amd64", built=None):
        self.calls = []
        self.runtime = runtime or good_runtime()
        self.base_arch = base_arch
        self.built = built or _good_image_inspect()

    def __call__(self, argv, input=None, capture_output=True, env=None, timeout=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "stdin": input})
        sub = argv[1]
        out = b""
        if sub == "image" and argv[2] == "inspect":
            ref = argv[3]
            out = json.dumps(_inspect(arch=self.base_arch, cfg={"Env": [f"{n}=x" for n in BASE_ENV]})
                             if ref == rl.BASE_REF else self.built).encode()
        elif sub == "run" and "--entrypoint" in argv:
            out = json.dumps({"runtime": self.runtime}).encode()
        return types.SimpleNamespace(returncode=0, stdout=out, stderr=b"")

    def subcommands(self):
        return [c["argv"][1] for c in self.calls]


@pytest.fixture
def staged(monkeypatch):
    monkeypatch.setattr(rl.image_tool, "stage", lambda arch, cache, out: (["Containerfile"], "c" * 64))
    for k, v in {"HTTPS_PROXY": "http://proxy:3128", "http_proxy": "http://proxy:3128",
                 "GOOGLE_API_KEY": "not-a-real-key", "CONTAINER_HOST": ""}.items():
        monkeypatch.setenv(k, v)


def test_build_probes_enforcement_before_building_and_strips_the_environment(staged):
    fake = FakePodman()
    ev = rl.build("/cache", facts=GOOD_FACTS, runner=fake)
    subs = fake.subcommands()
    assert subs.index("run") < subs.index("build")                          # enforcement proven first
    assert subs[:2] == ["pull", "image"]
    for call in fake.calls:
        assert "GOOGLE_API_KEY" not in call["env"] and "http_proxy" not in call["env"]
        if call["argv"][1] != "pull":
            assert not any("PROXY" in k.upper() for k in call["env"]), call["argv"][:3]
        if call["argv"][1] == "run":
            assert "--http-proxy=false" in call["argv"] and "--env-host=false" in call["argv"]
    build_call = next(c for c in fake.calls if c["argv"][1] == "build")
    assert "--platform=linux/amd64" in build_call["argv"]
    assert ev["tag"].endswith("-x86_64") and ev["image_id"] == "f" * 64


@pytest.mark.parametrize("runtime_change,expected", [
    (dict(no_new_privs="0"), "NoNewPrivs"), (dict(caps={c: "ff" for c in ii.CAP_FIELDS}), "CapEff"),
    (dict(net_interfaces=["eth0", "lo"]), "network"), (dict(env_names=BASE_ENV + ["https_proxy"]), "proxy"),
    (dict(cgroup={"memory.max": "max"}), "memory.max"), (dict(uid_map=["0", "0", "4294967295"]), "maps to host"),
])
def test_build_is_refused_when_the_probe_shows_missing_isolation(staged, runtime_change, expected):
    rt = good_runtime()
    rt.update(runtime_change)
    fake = FakePodman(runtime=rt)
    with pytest.raises(rl.Refused) as info:
        rl.build("/cache", facts=GOOD_FACTS, runner=fake)
    assert "build" not in fake.subcommands()
    assert any(expected in p for p in info.value.problems), info.value.problems


def test_build_is_refused_on_a_wrong_base_platform_or_built_architecture(staged):
    fake = FakePodman(base_arch="arm64")
    with pytest.raises(rl.Refused):
        rl.build("/cache", facts=GOOD_FACTS, runner=fake)
    assert "build" not in fake.subcommands()
    fake = FakePodman(built=_good_image_inspect(Architecture="arm64"))
    with pytest.raises(rl.Refused) as info:
        rl.build("/cache", facts=GOOD_FACTS, runner=fake)
    assert any("platform" in p for p in info.value.problems)


@pytest.mark.parametrize("change", [dict(uid=0), dict(remote_env=["CONTAINER_HOST"]),
                                    dict(podman_info__host__serviceIsRemote=True), dict(cgroup_fstype="tmpfs")])
def test_unsuitable_hosts_are_refused_before_any_podman_call(staged, change):
    fake = FakePodman()
    with pytest.raises(rl.Refused):
        rl.build("/cache", facts=facts(**change), runner=fake)
    with pytest.raises(rl.Refused):
        rl.verify("img", facts=facts(**change), runner=fake)
    assert fake.calls == []


def test_cross_architecture_request_is_refused_before_any_podman_call(staged):
    fake = FakePodman()
    with pytest.raises(rl.Refused):
        rl.build("/cache", "aarch64", facts=GOOD_FACTS, runner=fake)
    assert fake.calls == []


def test_verify_checks_image_runtime_mapping_and_inert_output(staged):
    rt = good_runtime(env=IMAGE_ENV)
    report = {"runtime": rt, "image": {"setid_files": [], "secret_like_files": [], "dev_dirs": [], "venvs": [],
                                       "distributions": {n: e["version"] for n, e in rl.INVENTORY["packages"].items()},
                                       "opt_ip": ["inert_entrypoint.py"], "present_forbidden": [],
                                       "world_writable_files": []}}

    class VerifyFake(FakePodman):
        inert = types.SimpleNamespace(returncode=78, stdout=b"", stderr=(EXPECT["inert_message"] + "\n").encode())

        def __call__(self, argv, **kw):
            r = super().__call__(argv, **kw)
            if argv[1] == "run" and "--entrypoint" in argv:
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(report).encode(), stderr=b"")
            if argv[1] == "run":
                return self.inert
            return r
    fake = VerifyFake()
    out = rl.verify("localhost/img", facts=GOOD_FACTS, runner=fake)
    assert out["image_id"] == "f" * 64
    fake.inert = types.SimpleNamespace(returncode=0, stdout=b"executed!", stderr=b"")
    with pytest.raises(rl.Refused) as info:
        rl.verify("localhost/img", facts=GOOD_FACTS, runner=fake)
    assert any(p.startswith("INERT") for p in info.value.problems)


def test_os_package_inventory_runs_with_the_same_isolation_options(staged):
    argv = rl.os_packages_argv("localhost/img", "x86_64")
    for flag in REQUIRED_RUN:
        assert flag in argv, flag
    assert argv[argv.index("--entrypoint") + 1] == "/usr/bin/dpkg-query"

    class DpkgFake(FakePodman):
        def __call__(self, argv, **kw):
            super().__call__(argv, **kw)
            return types.SimpleNamespace(returncode=0, stdout=b"libc6\t2.36-9+deb12u13\tamd64\n", stderr=b"")
    out = rl.os_packages("localhost/img", facts=GOOD_FACTS, runner=DpkgFake())
    assert out == {"count": 1, "packages": [{"name": "libc6", "version": "2.36-9+deb12u13", "arch": "amd64"}]}
    with pytest.raises(rl.Refused):
        rl.os_packages("localhost/img", facts=facts(uid=0), runner=DpkgFake())


# ── startup statistics ──────────────────────────────────────────────────────

def test_percentiles_use_the_nearest_rank_and_report_sample_counts():
    assert rl.percentile(list(range(1, 21)), 95) == 19
    assert rl.percentile(list(range(1, 11)), 95) == 10
    assert rl.percentile([5], 95) == 5
    s = rl.summarize([0.1, 0.2, 0.3, 0.4])
    assert s["n"] == 4 and s["median_ms"] == 250.0 and s["p95_ms"] == 400.0
    with pytest.raises(ValueError):
        rl.percentile([], 50)


def test_startup_measurement_separates_cold_and_warm_runs(staged):
    ticks = iter(range(10_000))

    class InertFake(FakePodman):
        def __call__(self, argv, **kw):
            super().__call__(argv, **kw)
            return types.SimpleNamespace(returncode=78 if argv[1] == "run" else 0, stdout=b"",
                                         stderr=(EXPECT["inert_message"] + "\n").encode() if argv[1] == "run" else b"")
    fake = InertFake()
    res = rl.measure_startup("localhost/img", warm=5, cold=3, facts=GOOD_FACTS, runner=fake,
                             clock=lambda: next(ticks) * 0.01)
    assert res["warm"]["n"] == 5 and res["cold"]["n"] == 3 and "page cache not dropped" in res["cold_definition"]
    subs = fake.subcommands()
    assert subs.count("rmi") == 3 and subs.count("load") == 3 and subs.count("run") == 8


# ── golden fixtures: pinned, read-only inputs, bounded outputs ──────────────

def test_golden_fixture_manifest_pins_the_reviewed_code_and_datasets():
    scripts = rl.golden_scripts()
    assert len(scripts) == 12 and sum(1 for fx, _, _ in scripts if fx["chart"]) == 7
    for fx, data, script in scripts:
        assert 'df = pd.read_csv(r"/in/dataset.csv")' in script
        assert f'CHART_PATH = r"/out/{fx["task_id"]}.png"' in script
        assert script.endswith(rl.CHART_EPILOGUE)
        assert os.path.basename(data) == fx["dataset_file"]


def test_golden_refuses_code_that_is_not_the_reviewed_fixture(monkeypatch, tmp_path):
    manifest = json.load(open(rl.FIXTURES))
    manifest["fixtures"][0]["code_sha256"] = "0" * 64
    p = tmp_path / "fixtures.json"
    p.write_text(json.dumps(manifest))
    monkeypatch.setattr(rl, "FIXTURES", str(p))
    with pytest.raises(rl.Refused) as info:
        rl.golden_scripts()
    assert "differs from the reviewed version" in info.value.problems[0]


def test_golden_refuses_a_modified_dataset_copy(monkeypatch, tmp_path):
    manifest = json.load(open(rl.FIXTURES))
    manifest["fixtures"][0]["dataset_sha256"] = "0" * 64
    p = tmp_path / "fixtures.json"
    p.write_text(json.dumps(manifest))
    monkeypatch.setattr(rl, "FIXTURES", str(p))
    with pytest.raises(rl.Refused):
        rl.golden_scripts()


def test_golden_runs_mount_dataset_copies_read_only_and_bound_outputs(staged):
    seen = []

    class GoldenFake(FakePodman):
        def __call__(self, argv, **kw):
            super().__call__(argv, **kw)
            m = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
            src = m[0].split("source=")[1].split(",")[0]
            seen.append((argv, os.stat(src).st_mode & 0o777))
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    res = rl.golden("localhost/img", facts=GOOD_FACTS, runner=GoldenFake())
    assert len(seen) == 12 and res["ok"] is False                       # nothing printed -> answers missing
    for argv, mode in seen:
        assert mode == 0o444
        assert any(a.endswith("destination=/in/dataset.csv,readonly=true") for a in argv)
        assert any(a.startswith("/out:") and "size=64m" in a and "noexec" in a for a in argv)
        assert "--http-proxy=false" in argv and "--network=none" in argv


# ── the gate in THIS environment ────────────────────────────────────────────

@pytest.mark.parametrize("args", [["build", "--wheel-cache", "/nonexistent"], ["verify", "localhost/none"],
                                  ["measure-startup", "localhost/none"], ["golden", "localhost/none"],
                                  ["os-packages", "localhost/none"]])
def test_cli_refuses_here_when_this_host_is_unsuitable(args):
    live = rl.gather_facts()
    if not rl.evaluate_host(live):
        pytest.skip("this host passes the fact checks; the refusal path is covered with fake facts")
    out = subprocess.run([sys.executable, "-I", "-B", os.path.join(SCRIPTS, "rootless.py"), *args],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 2 and "REFUSED" in out.stderr
