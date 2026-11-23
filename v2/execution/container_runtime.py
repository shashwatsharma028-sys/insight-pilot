"""
ContainerRuntime (P3.6-3): the production rootless-Podman implementation of ExecutionRuntime.

One runtime per application process; ONE fresh container per execution request. It never runs
generated code itself and never falls back to any other executor: every failure raises
SandboxUnavailableError / SandboxProtocolError (infrastructure) or DatasetNotAuthorizedError
(policy), which P5 and the V1 dispatcher treat as terminating.

    preflight()   host rules (host_checks.evaluate_host) -> pinned image config -> trusted
                  self-tests in containers (attest-only frame must match expected_attestation;
                  selftest-oom must be OOM-killed; selftest-pids must be refused)
    execute(r)    semaphore -> JobWorkspace -> code.py (verbatim) -> dataset staged BY IDENTITY
                  (three-way SHA-256) -> `podman run` (podman_argv.run_argv) -> one bounded IPX1
                  frame (frame.decode_frame) -> attestation verified -> chart through stage B +
                  ChartStore -> ExecutionResult; ALWAYS: kill + rm by exact name, verify the
                  container is gone, remove the workspace, release the semaphore.

STATUS: implemented and tested only against a FAKE podman executable (tests/v2/p36). No real
container has been started by this code. Whether the requested isolation and limits are
enforced is NOT established until VE-1 (functional) and VE-2 (security) runs, and P3.7.

Production cannot reach this runtime: v2.execution.bootstrap.ensure_ready() still refuses
hardened mode; the only entry point is bootstrap.install_for_validation() (pytest only,
IP_TEST_REQUIRE_CONTAINER=1, non-root, this exact class, passing preflight).
"""

import json
import os
import platform
import pwd
import shutil
import stat
import subprocess
import threading
import time

from v2.execution import image_layout
from v2.execution import podman_argv as pa
from v2.execution.errors import DatasetNotAuthorizedError, SandboxProtocolError, SandboxUnavailableError
from v2.execution.frame import decode_frame, expected_attestation, verify_attestation
from v2.execution.host_checks import evaluate_host
from v2.execution.isolation import IsolationLevel
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.result_mapping import outcome_of, publish_chart, result_from_frame
from v2.execution.results import ExecutionOutcome
from v2.execution.runtime import ExecutionRequest, PreludeId

EXPECTED_ENTRYPOINT = ["/usr/local/bin/python3", "-I", "-B", "/opt/ip/supervisor.py"]
EXPECTED_STAGE_LABEL = "P3.6"
# P3.6-4A: the image must carry the digest of exactly the host's trusted runtime files.
EXPECTED_RUNTIME_LABEL = image_layout.RUNTIME_LABEL
CLI_STDOUT_CAP = 4 * 1024 * 1024          # podman version/info/inspect JSON
CLI_STDERR_CAP = 64 * 1024                # diagnostics only, never shown to users
CLI_TIMEOUT = 60
REMOTE_ENV = ("CONTAINER_HOST", "CONTAINER_CONNECTION", "DOCKER_HOST", "CONTAINER_SSHKEY", "DOCKER_CONTEXT")


class ContainerRuntime:
    name = "rootless-podman"
    isolation = IsolationLevel.ROOTLESS_CONTAINER
    is_test_double = False

    def __init__(self, *, image_id, podman, lease, authorizer, chart_store, limits: Limits = DEFAULT_LIMITS,
                 popen=None, clock=time.monotonic):
        pa.image_inspect_argv(podman, image_id)                       # validates both (ArgvError)
        if not isinstance(limits, Limits):
            raise TypeError("limits must be Limits")
        self._check_executable(podman)
        self.image_id, self.podman, self.limits = image_id, podman, limits
        self._lease, self._authorizer, self._store = lease, authorizer, chart_store
        self.chart_directory = os.path.realpath(chart_store.charts_dir)
        self._popen = popen or subprocess.Popen
        self._clock = clock
        self._sem = threading.BoundedSemaphore(limits.max_concurrent)
        self._preflight_ok = False
        self.diagnostics = []

    # ── trust in the CLI binary and its environment ─────────────────────────
    @staticmethod
    def _check_executable(path):
        try:
            st = os.stat(path)
        except OSError:
            raise SandboxUnavailableError("the podman executable does not exist") from None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
                or not st.st_mode & stat.S_IXUSR:
            raise SandboxUnavailableError("the podman executable must be a root-owned, non-writable executable file")

    def _env(self):
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        for key in ("HOME", "XDG_RUNTIME_DIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        return env

    def _effective_uid(self):
        return os.geteuid()

    def _cli(self, argv, timeout=CLI_TIMEOUT):
        """(returncode, stdout bytes, stderr bytes) of a short podman CLI command; bounded output."""
        pa.validate_argv(argv)
        proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=self._env(), close_fds=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise SandboxUnavailableError(f"podman {argv[1]} timed out") from None
        if len(out) > CLI_STDOUT_CAP:
            raise SandboxProtocolError(f"podman {argv[1]} produced too much output")
        self.diagnostics.append(err[:CLI_STDERR_CAP].decode("utf-8", "replace"))
        return proc.returncode, out, err

    # ── preflight ───────────────────────────────────────────────────────────
    def _host_facts(self):
        uid = self._effective_uid()
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        facts = {"uid": uid, "user": user, "machine": platform.machine(),
                 "remote_env": sorted(k for k in REMOTE_ENV if os.environ.get(k)),
                 "cgroup_fstype": _cgroup_fstype(), "delegated_controllers": _delegated(uid),
                 "subuid": _subids("/etc/subuid", user, uid), "subgid": _subids("/etc/subgid", user, uid),
                 "newuidmap": shutil.which("newuidmap"), "newgidmap": shutil.which("newgidmap"),
                 "max_user_namespaces": _sysctl("user.max_user_namespaces"),
                 "unprivileged_userns_clone": _sysctl("kernel.unprivileged_userns_clone"),
                 "apparmor_restrict_unprivileged_userns": _sysctl("kernel.apparmor_restrict_unprivileged_userns"),
                 "podman": self.podman, "podman_version": None, "podman_info": None}
        rc, out, _ = self._cli(pa.version_argv(self.podman))
        if rc == 0:
            try:
                facts["podman_version"] = json.loads(out)["Client"]["Version"]
            except (ValueError, KeyError, TypeError):
                pass
        rc, out, _ = self._cli(pa.info_argv(self.podman))
        if rc == 0:
            try:
                facts["podman_info"] = json.loads(out)
            except ValueError:
                pass
        return facts

    def _check_image(self):
        rc, out, _ = self._cli(pa.image_inspect_argv(self.podman, self.image_id))
        if rc != 0:
            raise SandboxUnavailableError("the pinned sandbox image is not present")
        try:
            (img,) = json.loads(out)
            cfg = img["Config"]
        except (ValueError, KeyError, TypeError):
            raise SandboxProtocolError("podman image inspect returned an unexpected document") from None
        problems = []
        if img.get("Id") != self.image_id.split(":", 1)[1]:
            problems.append("image id differs from the pinned id")
        if cfg.get("User") != pa.CONTAINER_USER:
            problems.append(f"image user is {cfg.get('User')!r}")
        if cfg.get("Entrypoint") != EXPECTED_ENTRYPOINT:
            problems.append("image entrypoint is not the trusted supervisor")
        labels = cfg.get("Labels") or {}
        if labels.get("io.insightpilot.stage") != EXPECTED_STAGE_LABEL:
            problems.append("image is not a P3.6 sandbox image")
        if labels.get(EXPECTED_RUNTIME_LABEL) != image_layout.runtime_tree_sha256():
            problems.append("image runtime label does not match the host's trusted runtime files")
        if cfg.get("Volumes") or cfg.get("ExposedPorts"):
            problems.append("image declares volumes or ports")
        if problems:
            raise SandboxUnavailableError("sandbox image refused: " + "; ".join(problems))

    def preflight(self):
        self._preflight_ok = False
        problems = evaluate_host(self._host_facts())
        if problems:
            raise SandboxUnavailableError("host is not suitable for rootless containers: " + "; ".join(problems))
        self._check_image()
        attested = self._self_test("attest-only")
        verify_attestation(attested.attestation, expected_attestation(self.limits))
        if not self._self_test("selftest-oom").oom_killed:
            raise SandboxUnavailableError("self-test: the memory limit was not enforced")
        pids = self._self_test("selftest-pids")
        if pids.child_exit != 4:
            raise SandboxUnavailableError("self-test: the pids limit was not enforced")
        self._preflight_ok = True

    def _self_test(self, mode):
        from v2.execution.workspace import JobWorkspace
        ws = JobWorkspace.create(self._lease, self.limits)
        try:
            decoded, _ = self._run_container(ws, PreludeId.V1, mode)
            return decoded
        finally:
            ws.cleanup()

    # ── execution ───────────────────────────────────────────────────────────
    def execute(self, request: ExecutionRequest):
        from v2.execution.workspace import JobWorkspace
        if not isinstance(request, ExecutionRequest):
            raise TypeError("request must be an ExecutionRequest")
        if not self._preflight_ok:
            raise SandboxUnavailableError("the container runtime has not passed its preflight")
        if request.dataset is None:
            raise DatasetNotAuthorizedError("Dataset not authorised: the request carries no dataset identity")
        if not self._sem.acquire(timeout=self.limits.queue_wait_seconds):
            raise SandboxUnavailableError("the container runtime is busy (queue wait exceeded)")
        try:
            ws = JobWorkspace.create(self._lease, self.limits)
            try:
                ws.write_code(request.code)
                self._authorizer.authorize_identity_into(request.dataset, ws)   # three-way SHA-256
                decoded, duration_ms = self._run_container(ws, request.prelude, "run")
                verify_attestation(decoded.attestation, expected_attestation(self.limits))
                chart_path = reason = None
                if outcome_of(decoded) is ExecutionOutcome.SUCCEEDED:
                    chart_path, reason = publish_chart(decoded, self._store, request.task_id, request.run_id,
                                                       self.limits)
                return result_from_frame(decoded, run_id=request.run_id, frame_run_id=ws.run_id,
                                         duration_ms=duration_ms, chart_path=chart_path,
                                         chart_rejected_reason=reason)
            finally:
                ws.cleanup()
        finally:
            self._sem.release()

    def _run_container(self, ws, prelude, mode):
        """(decoded frame, duration in ms). The container is always killed, removed and verified gone."""
        run_id = ws.run_id
        name = pa.container_name(self._lease.instance_id, run_id)
        argv = pa.run_argv(self.podman, self.image_id, instance_id=self._lease.instance_id, run_id=run_id,
                           inputs_dir=os.path.realpath(ws.input_dir), prelude=prelude, mode=mode, limits=self.limits)
        t0 = self._clock()
        proc = None
        stderr_tail = bytearray()
        drain = None
        deadline_hit = threading.Event()
        try:
            proc = self._popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=self._env(), close_fds=True, start_new_session=True)
            drain = threading.Thread(target=_drain, args=(proc.stderr, stderr_tail, CLI_STDERR_CAP), daemon=True)
            drain.start()

            def _deadline():
                deadline_hit.set()
                self._kill(name, proc)
            timer = threading.Timer(self.limits.host_deadline_seconds, _deadline)
            timer.daemon = True
            timer.start()
            try:
                try:
                    decoded = decode_frame(proc.stdout, run_id, self.limits)
                except SandboxProtocolError:
                    if deadline_hit.is_set():
                        raise SandboxUnavailableError("the container exceeded the host deadline") from None
                    raise
                rc = proc.wait(timeout=max(1.0, self.limits.host_grace_seconds))
            finally:
                timer.cancel()
            if deadline_hit.is_set():
                raise SandboxUnavailableError("the container exceeded the host deadline")
            if rc != 0:
                raise SandboxProtocolError(f"podman run exited with status {rc} after the frame")
            return decoded, int((self._clock() - t0) * 1000)
        except subprocess.TimeoutExpired:
            raise SandboxUnavailableError("the container did not exit after its frame") from None
        finally:
            self._kill(name, proc)
            if drain is not None:
                drain.join(timeout=5)
                self.diagnostics.append(bytes(stderr_tail).decode("utf-8", "replace"))   # never shown to users
            self._remove(name)

    def _kill(self, name, proc):
        if proc is not None and proc.poll() is None:
            try:
                self._cli(pa.kill_argv(self.podman, name), timeout=30)
            except SandboxUnavailableError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def _remove(self, name):
        try:
            self._cli(pa.rm_argv(self.podman, name), timeout=60)
        except SandboxUnavailableError:
            pass
        rc, _, _ = self._cli(pa.exists_argv(self.podman, name), timeout=30)
        if rc == 0:
            raise SandboxUnavailableError(f"container {name} is still present after removal")
        if rc != 1:
            raise SandboxUnavailableError(f"cannot verify that container {name} was removed (status {rc})")


def _drain(stream, sink, cap):
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            if len(sink) < cap:
                sink.extend(chunk[:cap - len(sink)])
    except (OSError, ValueError):
        return


# ── host fact helpers (read-only; same sources as scripts/p3/rootless.py) ──────

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


def _delegated(uid):
    try:
        with open(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers",
                  encoding="ascii") as f:
            return sorted(f.read().split())
    except OSError:
        return None
