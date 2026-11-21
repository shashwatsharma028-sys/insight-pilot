"""
Instance ownership and lease (P3.3).

Layout under the private V2 workspace root (default
<tempdir>/insightpilot-v2-<euid>, mode 0700, owned by this user; any other
state, including a symlink, is refused):

    <root>/instances/<instance_id>/        0700
        lease.lock   0600   flock(LOCK_EX) held for the lease's lifetime
        lease.json   0600   minimal diagnostics: schema, instance id, pid,
                            boot id, creation time. No credentials, no environment.
        runs/        0700   per-run JobWorkspaces (v2/execution/workspace.py)

Liveness is decided by the kernel lock, never by PID (PID reuse cannot fool
it): if another process can take the lock, the owner is gone. flock locks
belong to an open file description, so even within one process a second
lease is distinguishable.

Lifecycle (P3.3 correction):
- Run creation (JobWorkspace.create) and release() are serialised by a
  per-lease lock. Once release() starts, the lease is "releasing" and no
  new run can be created; a run whose creation finished first is reported
  in remaining_runs and left in place.
- release() always drops the flock and closes every descriptor it owns,
  even when a removal step fails. A failed removal is reported
  (removed=False, error=...), never as success. Removal order keeps
  lease.json until the very last file, so an incomplete removal stays
  inspectable.

inspect_instances() only REPORTS: self / live / stale / indeterminate /
orphan / unexpected. Nothing here deletes another instance's files, runs a
broad startup cleanup, or touches containers. Destructive stale-instance
reconciliation is deliberately disabled until it is separately reviewed.

Platform: flock and descriptor-relative calls are verified on Linux. macOS
provides them too, but no equivalent guarantee is claimed until tested.
"""

import contextlib
import fcntl
import json
import os
import secrets
import stat
import tempfile
import threading
import time

from v2.execution.errors import SandboxUnavailableError
from v2.execution.results import is_valid_run_id

SCHEMA = 1
INSTANCES = "instances"
RUNS = "runs"
LEASE_LOCK = "lease.lock"
LEASE_INFO = "lease.json"
MAX_LEASE_INFO_BYTES = 4096
_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_PRIVATE_BITS = stat.S_IRWXG | stat.S_IRWXO


def default_root() -> str:
    return os.path.join(tempfile.gettempdir(), f"insightpilot-v2-{os.geteuid()}")


def boot_id():
    try:
        with open(_BOOT_ID_PATH, encoding="ascii") as f:
            value = f.read(64).strip()
        return value or None
    except OSError:
        return None


def open_private_dir(parent_fd, name, create=True) -> int:
    """Open (creating if asked) a 0700 directory owned by this user, relative to parent_fd."""
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, _OPEN_DIR, dir_fd=parent_fd)
    except OSError as exc:
        raise SandboxUnavailableError(
            f"V2 workspace directory {name!r} cannot be opened safely ({exc.strerror})") from exc
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & _PRIVATE_BITS:
        os.close(fd)
        raise SandboxUnavailableError(
            f"V2 workspace directory {name!r} must be a private (0700) directory owned by this user")
    return fd


def open_private_root(root=None):
    """Return (fd, absolute path) of the private V2 workspace root."""
    root = os.path.abspath(root or default_root())
    parent, name = os.path.split(root)
    if not name:
        raise SandboxUnavailableError("the V2 workspace root cannot be a filesystem root")
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise SandboxUnavailableError(f"the V2 workspace parent cannot be opened ({exc.strerror})") from exc
    try:
        return open_private_dir(parent_fd, name), root
    finally:
        os.close(parent_fd)


class InstanceLease:
    """Ownership of one application instance's workspace. Use acquire()."""

    def __init__(self, *, instance_id, root_path, root_fd, instances_fd, instance_fd, lock_fd, runs_fd):
        self.instance_id = instance_id
        self.root_path = root_path
        self._root_fd = root_fd
        self._instances_fd = instances_fd
        self._instance_fd = instance_fd
        self._lock_fd = lock_fd
        self.runs_fd = runs_fd
        self._state_lock = threading.Lock()
        self._releasing = False

    @property
    def instance_path(self) -> str:
        return os.path.join(self.root_path, INSTANCES, self.instance_id)

    @property
    def runs_path(self) -> str:
        return os.path.join(self.instance_path, RUNS)

    @classmethod
    def acquire(cls, root=None) -> "InstanceLease":
        root_fd, root_path = open_private_root(root)
        instances_fd = instance_fd = lock_fd = runs_fd = None
        instance_id = secrets.token_hex(16)
        created_dir = False
        try:
            instances_fd = open_private_dir(root_fd, INSTANCES)
            os.mkdir(instance_id, 0o700, dir_fd=instances_fd)            # exclusive
            created_dir = True
            instance_fd = open_private_dir(instances_fd, instance_id, create=False)
            lock_fd = os.open(LEASE_LOCK, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=instance_fd)
            # Blocking: the lock file is brand new, so at most an inspector
            # holds it, for an instant.
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            info = {"schema": SCHEMA, "instance_id": instance_id, "pid": os.getpid(),
                    "boot_id": boot_id(), "created_unix": int(time.time())}
            info_fd = os.open(LEASE_INFO, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=instance_fd)
            try:
                os.write(info_fd, json.dumps(info, sort_keys=True).encode("ascii"))
            finally:
                os.close(info_fd)
            runs_fd = open_private_dir(instance_fd, RUNS)
        except BaseException as exc:
            for fd in (runs_fd, lock_fd, instance_fd):
                if fd is not None:
                    os.close(fd)
            if created_dir:
                _remove_own_instance_dir(instances_fd, instance_id)
            for fd in (instances_fd, root_fd):
                if fd is not None:
                    os.close(fd)
            if isinstance(exc, SandboxUnavailableError):
                raise
            if isinstance(exc, OSError):
                raise SandboxUnavailableError(f"instance lease could not be created ({exc.strerror})") from exc
            raise
        return cls(instance_id=instance_id, root_path=root_path, root_fd=root_fd, instances_fd=instances_fd,
                   instance_fd=instance_fd, lock_fd=lock_fd, runs_fd=runs_fd)

    def is_held(self) -> bool:
        return self._lock_fd is not None and not self._releasing

    def active_runs(self) -> list:
        if self.runs_fd is None:
            return []
        return sorted(os.listdir(self.runs_fd))

    @contextlib.contextmanager
    def hold_for_run_creation(self):
        """
        Serialise run creation with release(). Yields the runs-directory
        descriptor; refuses if the lease is released or releasing.
        """
        with self._state_lock:
            if not self.is_held():
                raise SandboxUnavailableError("the instance lease is not held (released or releasing)")
            yield self.runs_fd

    def release(self) -> dict:
        """
        Release the lease. Remove this instance's own directory only if it has
        no runs left; otherwise leave everything in place and report it.
        Always drops the flock and closes every owned descriptor.
        """
        with self._state_lock:
            if self._lock_fd is None:
                return {"released": False, "removed": False, "remaining_runs": [], "error": None}
            self._releasing = True            # no new runs from here on
            remaining, removed, error = [], False, None
            try:
                remaining = self.active_runs()
                if not remaining:
                    self._remove_own_directory()
                    removed = True
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc.strerror or exc}"
            finally:
                self._teardown()
            return {"released": True, "removed": removed, "remaining_runs": remaining, "error": error}

    def _remove_own_directory(self) -> None:
        """
        Remove only this instance's directory, in an order that keeps it
        inspectable if a step fails: runs/ first (fails if a run appeared,
        removing nothing else), then the lock file, then lease.json, then the
        directory itself.
        """
        os.rmdir(RUNS, dir_fd=self._instance_fd)
        os.unlink(LEASE_LOCK, dir_fd=self._instance_fd)
        os.unlink(LEASE_INFO, dir_fd=self._instance_fd)
        os.rmdir(self.instance_id, dir_fd=self._instances_fd)

    def _teardown(self) -> None:
        """Drop the flock and close every owned descriptor; never raises."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass                           # closing the descriptor releases it anyway
        for attr in ("runs_fd", "_lock_fd", "_instance_fd", "_instances_fd", "_root_fd"):
            fd = getattr(self, attr)
            setattr(self, attr, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _remove_own_instance_dir(instances_fd, instance_id) -> None:
    """Best-effort removal of a directory this process just created (failed acquire)."""
    try:
        fd = os.open(instance_id, _OPEN_DIR, dir_fd=instances_fd)
    except OSError:
        return
    try:
        for name in (LEASE_INFO, LEASE_LOCK):
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass
        try:
            os.rmdir(RUNS, dir_fd=fd)
        except OSError:
            pass
    finally:
        os.close(fd)
    try:
        os.rmdir(instance_id, dir_fd=instances_fd)
    except OSError:
        pass


def _read_lease_info(instance_fd):
    try:
        fd = os.open(LEASE_INFO, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=instance_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_LEASE_INFO_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_LEASE_INFO_BYTES:
        return None
    try:
        info = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    return {k: info.get(k) for k in ("schema", "instance_id", "pid", "boot_id", "created_unix")}


def inspect_instances(root=None, own_instance_id=None) -> list:
    """
    Classify every entry under <root>/instances. REPORT ONLY: never deletes,
    never blocks on a lock, never touches containers.
    """
    root_fd, _ = open_private_root(root)
    reports = []
    try:
        try:
            instances_fd = open_private_dir(root_fd, INSTANCES, create=False)
        except SandboxUnavailableError:
            return reports
        try:
            current_boot = boot_id()
            for entry in sorted(os.listdir(instances_fd)):
                reports.append(_classify(instances_fd, entry, own_instance_id, current_boot))
        finally:
            os.close(instances_fd)
    finally:
        os.close(root_fd)
    return reports


def _classify(instances_fd, entry, own_instance_id, current_boot) -> dict:
    report = {"entry": entry[:80], "state": None, "reason": "", "lease": None}
    if not is_valid_run_id(entry):
        report.update(state="unexpected", reason="not an instance identifier")
        return report
    if entry == own_instance_id:
        report.update(state="self", reason="this process holds the lease")
        return report
    try:
        instance_fd = os.open(entry, _OPEN_DIR, dir_fd=instances_fd)
    except OSError as exc:
        report.update(state="orphan", reason=f"not a usable directory ({exc.strerror})")
        return report
    try:
        report["lease"] = _read_lease_info(instance_fd)
        try:
            lock_fd = os.open(LEASE_LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                              dir_fd=instance_fd)
        except FileNotFoundError:
            report.update(state="orphan", reason="no lease lock file")
            return report
        except OSError as exc:
            report.update(state="orphan", reason=f"lease lock unusable ({exc.strerror})")
            return report
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                report.update(state="live", reason="another process holds the lease lock")
                return report
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
        lease = report["lease"]
        if lease is None:
            report.update(state="indeterminate", reason="lock is free but lease.json is missing or invalid "
                                                        "(possibly still starting)")
        elif lease.get("instance_id") != entry:
            report.update(state="orphan", reason="lease.json names a different instance")
        elif current_boot and lease.get("boot_id") and lease["boot_id"] != current_boot:
            report.update(state="stale", reason="lease is from a previous boot")
        else:
            report.update(state="stale", reason="lease lock is free: the owning process has exited")
        return report
    finally:
        os.close(instance_fd)


def reconcile_stale_instances(*args, **kwargs):
    """Disabled: destructive stale-instance reconciliation needs a separate review."""
    raise RuntimeError("destructive stale-instance reconciliation is disabled until separately reviewed; "
                       "use inspect_instances() to report")
