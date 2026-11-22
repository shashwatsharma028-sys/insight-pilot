"""
Host suitability rules for rootless container execution (P3.6-1; moved verbatim from scripts/p3/rootless.py).

evaluate_host(facts) is PURE: it takes a facts dictionary (gathered elsewhere) and returns every
problem that makes the host unsuitable; an empty list means "suitable, still subject to the
enforcement probe / runtime preflight". The development driver scripts/p3/rootless.py and the
production ContainerRuntime.preflight() both use this one implementation, so the rules cannot drift.
Nothing here reads the environment, the filesystem or starts a process.
"""

import re

OCI_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}
MIN_PODMAN = (4, 4)                     # --identity-label, --chmod in COPY, cgroup v2 limits
SUBID_MIN = 65536
CONTAINER_UID = 10001


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
