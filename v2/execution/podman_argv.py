"""
Pure builders for every Podman command line the container runtime uses (P3.6-1).

Nothing here starts a process, reads the environment or touches the filesystem: it only
returns argv LISTS (never a shell string), and validate_argv() re-checks every element
against an allowlist grammar before the runtime may use one.

The run command is design §8.1 (docs/v2/P3_SECURE_EXECUTION_DESIGN.md), with every
size taken from the Limits contract (R2: memory 1 GiB, provisional). These flags are
what P3.6 *requests*; that the runtime ENFORCES them is not claimed until it is tested on a
real rootless host (VE-1 functionally, VE-2 for security; P3.7 for resource enforcement).

What can never reach an argv: the task id, the dataset's host path, generated code, model
output, or any other caller-supplied text. The only caller-derived values are the run id
(32 hex), the instance id (32 hex), the image id (sha256:<64 hex>), the workspace input
directory (a validated absolute path) and the PreludeId enum.
"""

import re

from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import is_valid_run_id
from v2.execution.runtime import PreludeId

CONTAINER_USER = "10001:10001"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
INSTANCE_ID = re.compile(r"[0-9a-f]{32}")
SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]+")
SUPERVISOR_MODES = ("run", "attest-only", "selftest-oom", "selftest-pids")
EXPECTED_WRITABLE_MOUNTS = ("/out", "/tmp")
# Environment the container receives beyond the image's own ENV (no host variable is ever passed).
CONTAINER_ENV = (("LANG", "C.UTF-8"), ("TZ", "UTC"), ("OPENBLAS_NUM_THREADS", "1"), ("OMP_NUM_THREADS", "1"),
                 ("MKL_NUM_THREADS", "1"))
LABEL_PREFIX = "org.insightpilot"

# Every element of every argv must match one of these (checked again by validate_argv).
_ELEMENT = re.compile(r"[A-Za-z0-9_./:=,@+-]{1,512}")


class ArgvError(ValueError):
    """A value is not acceptable on a Podman command line; nothing may be started."""


def _require(cond, message):
    if not cond:
        raise ArgvError(message)


def _check_podman(podman):
    _require(isinstance(podman, str) and SAFE_PATH.fullmatch(podman) is not None and _normalised(podman),
             "the podman executable must be an absolute, normalised path of safe characters")


def _normalised(path):
    return not ("//" in path or "/./" in path or "/../" in path or path.endswith(("/", "/.", "/..")))


def check_ids(instance_id, run_id):
    _require(isinstance(instance_id, str) and INSTANCE_ID.fullmatch(instance_id) is not None,
             "instance id must be 32 lowercase hex characters")
    _require(is_valid_run_id(run_id), "run id must be 32 lowercase hex characters")


def container_name(instance_id, run_id):
    check_ids(instance_id, run_id)
    return f"ip-{instance_id[:8]}-{run_id[:12]}"


def _cpus(value):
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def run_argv(podman, image_id, *, instance_id, run_id, inputs_dir, prelude=PreludeId.V1, mode="run",
             limits=DEFAULT_LIMITS):
    """The complete `podman run` argv for one execution (or a trusted supervisor self-test mode)."""
    _check_podman(podman)
    _require(isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id) is not None,
             "the image must be pinned by ID (sha256:<64 hex>), never by tag")
    check_ids(instance_id, run_id)
    _require(isinstance(inputs_dir, str) and SAFE_PATH.fullmatch(inputs_dir) is not None
             and _normalised(inputs_dir) and inputs_dir.endswith(f"/runs/{run_id}/in"),
             "the input mount source must be this run's workspace in/ directory (safe absolute path)")
    _require(isinstance(prelude, PreludeId), "prelude must be a PreludeId")
    _require(mode in SUPERVISOR_MODES, f"unknown supervisor mode {mode!r}")
    _require(isinstance(limits, Limits), "limits must be Limits")
    name = container_name(instance_id, run_id)
    lim = limits
    argv = [
        podman, "run", "--rm", "--name", name,
        "--label", f"{LABEL_PREFIX}.instance={instance_id}",
        "--label", f"{LABEL_PREFIX}.run={run_id}",
        "--label", f"{LABEL_PREFIX}.schema=1",
        "--pull=never", "--image-volume=ignore",
        # filesystem: read-only root, no implicit tmpfs, two bounded tmpfs, inputs read-only
        "--read-only", "--read-only-tmpfs=false",
        # P3.6-4B D3 (approved): no nr_inodes. Podman 4.9.3 rejects it ("unknown mount option", CI run
        # 45125846903). The explicit inode-count limits (Limits.tmp_inodes/out_inodes) are therefore NOT
        # enforced by the launcher; byte sizes, mode and noexec/nosuid/nodev are unchanged. Not equivalent
        # protection: inode memory is only bounded indirectly, by the container's memory cgroup.
        "--tmpfs", f"/tmp:rw,size={lim.tmp_bytes},mode=1777,noexec,nosuid,nodev",
        "--tmpfs", f"/out:rw,size={lim.out_bytes},mode=1777,noexec,nosuid,nodev",
        "--mount", f"type=bind,source={inputs_dir},destination=/in,readonly=true",
        # network and name services: none. P3.6-4B D1 (approved): no --dns=none; Podman 4.9.3 refuses it
        # with --network=none ("conflicting options: dns and the network mode: none", CI run 45122041350),
        # and without a network interface DNS configuration has nothing to act on.
        "--network=none", "--no-hosts", "--http-proxy=false",
        # identity and privileges
        "--user", CONTAINER_USER, "--passwd=false", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        # namespaces and logging
        "--ipc=none", "--cgroupns=private", "--log-driver=passthrough", "--env-host=false",
        # resources (requested here; enforcement is proven only by P3.7 runtime tests)
        f"--memory={lim.memory_bytes}", f"--memory-swap={lim.memory_bytes}",
        f"--cpus={_cpus(lim.cpus)}", f"--pids-limit={lim.pids}",
        f"--ulimit=nofile={lim.open_files}:{lim.open_files}", "--ulimit=core=0",
        f"--timeout={lim.host_deadline_seconds}",
    ]
    for key, value in CONTAINER_ENV:
        argv += ["--env", f"{key}={value}"]
    argv += [image_id,                       # the image ENTRYPOINT is the trusted supervisor
             "--mode", mode, "--run-id", run_id, "--prelude", prelude.value,
             "--wall", str(lim.code_wall_seconds),
             "--stdout-cap", str(lim.stdout_bytes),
             "--stderr-head", str(lim.stderr_head_bytes), "--stderr-tail", str(lim.stderr_tail_bytes),
             "--chart-cap", str(lim.chart_bytes), "--max-side", str(lim.chart_max_side),
             "--max-pixels", str(lim.chart_max_pixels),
             "--memory-max", str(lim.memory_bytes), "--pids-max", str(lim.pids)]
    validate_argv(argv)
    return argv


def kill_argv(podman, name):
    _check_podman(podman)
    _check_name(name)
    return [podman, "kill", "--signal", "KILL", name]


def rm_argv(podman, name):
    _check_podman(podman)
    _check_name(name)
    return [podman, "rm", "--force", "--time", "0", name]


def exists_argv(podman, name):
    _check_podman(podman)
    _check_name(name)
    return [podman, "container", "exists", name]


def image_inspect_argv(podman, image_id):
    _check_podman(podman)
    _require(isinstance(image_id, str) and IMAGE_ID.fullmatch(image_id) is not None, "image must be pinned by ID")
    return [podman, "image", "inspect", image_id]


def version_argv(podman):
    _check_podman(podman)
    return [podman, "version", "--format", "json"]


def info_argv(podman):
    _check_podman(podman)
    return [podman, "info", "--format", "json"]


_NAME = re.compile(r"ip-[0-9a-f]{8}-[0-9a-f]{12}")


def _check_name(name):
    _require(isinstance(name, str) and _NAME.fullmatch(name) is not None, "container name has the wrong form")


# Podman 4.9.3 rejects these with --network=none before any container starts (P3.6-4B D1).
DNS_FLAGS_INCOMPATIBLE_WITH_NETWORK_NONE = ("--dns", "--dns-option", "--dns-search")


def validate_argv(argv):
    """Every element is a str of the allowlisted alphabet; no shell metacharacters, spaces or NUL."""
    _require(isinstance(argv, list) and argv, "argv must be a non-empty list")
    for element in argv:
        _require(isinstance(element, str) and _ELEMENT.fullmatch(element) is not None,
                 f"argv element not allowed: {element!r}")
    if "--network=none" in argv:
        _require(not any(e.split("=", 1)[0] in DNS_FLAGS_INCOMPATIBLE_WITH_NETWORK_NONE for e in argv),
                 "a --dns flag cannot be combined with --network=none (Podman 4.9.3 refuses the run)")
    tmpfs = [argv[i + 1] for i, e in enumerate(argv[:-1]) if e == "--tmpfs"]
    _require(not any("nr_inodes=" in t for t in tmpfs),
             "nr_inodes is not a Podman 4.9.3 tmpfs option (the run is refused); see P3.6-4B D3")
    return argv
