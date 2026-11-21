"""
Per-execution workspace (P3.3).

    <root>/instances/<instance_id>/runs/<run_id>/     0700 (private)
        in/                                           0755 inside 0700 parents
            code.py       0644   generated code, written verbatim
            dataset.csv   0644   verified demo copy (DatasetAuthorizer)

- run_id: 128 random bits (secrets.token_hex(16)), validated before it is
  used as a path component, so it can never traverse.
- `in/` holds TRUSTED HOST INPUT only, written by this process with exclusive
  creation relative to a directory descriptor (O_CREAT|O_EXCL|O_NOFOLLOW).
  The planned rootless container will mount it read-only. The 0755/0644
  modes let the container's mapped user read it; the 0700 parents keep every
  other host account out (design §8.2, option R1).
- There is deliberately NO output directory: future container output stays
  in an in-container tmpfs and leaves only as a bounded frame (design §8.5),
  so untrusted output never lands in this workspace.
- Only two input names exist: code.py and dataset.csv. Anything else is
  refused before any filesystem call.
- cleanup() removes exactly this run's directory, relative to the lease's
  runs-directory descriptor, with shutil.rmtree's symlink-attack-resistant
  descriptor implementation (symlinks inside are unlinked, never followed).
  If that implementation is unavailable on a platform, only the two known
  files and the two directories are removed, and anything unexpected is
  left in place and reported. It never touches another run or instance.
  Used as a context manager, cleanup runs on success, refusal and exceptions.

Lifecycle (P3.3 correction):
- create() removes a run directory on failure ONLY if this very call created
  it; a colliding run id fails without touching the existing run. Creation
  holds the lease's run-creation lock, so it cannot interleave with release.
- Each workspace owns a duplicate of the runs-directory descriptor, so its
  cleanup never depends on the lease still being held, and never falls back
  to a path relative to the current directory.
- cleanup() first closes the workspace to new input (permanently), then
  deletes. `cleaned` becomes True only after the deletion is verified; a
  failed deletion is raised and can be retried by calling cleanup() again.

Platform: verified on Linux (P3.3 tests). No equivalent guarantee is
claimed on macOS until tested there.
"""

import os
import secrets
import shutil
import stat

from v2.execution.errors import SandboxPolicyError, SandboxUnavailableError
from v2.execution.lease import open_private_dir
from v2.execution.limits import DEFAULT_LIMITS, MIB, Limits
from v2.execution.results import is_valid_run_id

INPUT_DIR = "in"
CODE_NAME = "code.py"
DATASET_NAME = "dataset.csv"
INPUT_NAMES = frozenset({CODE_NAME, DATASET_NAME})

_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_INPUT = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _new_run_id() -> str:
    return secrets.token_hex(16)


class JobWorkspace:
    def __init__(self, *, lease, run_id, runs_fd, run_fd, input_fd, limits):
        self._lease = lease
        self.run_id = run_id
        self._runs_fd = runs_fd          # owned duplicate of the lease's runs/ descriptor
        self._run_fd = run_fd
        self._input_fd = input_fd
        self.limits = limits
        self.closing = False             # set when cleanup starts; never cleared
        self.cleaned = False             # set only after deletion is verified

    # ── creation ────────────────────────────────────────────────────────────
    @classmethod
    def create(cls, lease, limits: Limits = DEFAULT_LIMITS) -> "JobWorkspace":
        if not isinstance(limits, Limits):
            raise TypeError("limits must be Limits")
        run_id = _new_run_id()
        if not is_valid_run_id(run_id):                        # defensive
            raise SandboxUnavailableError("invalid run identifier")
        with lease.hold_for_run_creation() as lease_runs_fd:   # refuses if released/releasing
            runs_fd = os.dup(lease_runs_fd)
            run_fd = input_fd = None
            created = False                                    # did THIS call create runs/<run_id>?
            try:
                try:
                    os.mkdir(run_id, 0o700, dir_fd=runs_fd)    # exclusive
                except FileExistsError:
                    raise SandboxUnavailableError(
                        "run identifier collision; the existing run was left untouched") from None
                created = True
                run_fd = open_private_dir(runs_fd, run_id, create=False)
                os.mkdir(INPUT_DIR, 0o700, dir_fd=run_fd)
                input_fd = os.open(INPUT_DIR, _OPEN_DIR, dir_fd=run_fd)
                st = os.fstat(input_fd)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                    raise SandboxUnavailableError("the workspace input directory is not ours")
                os.fchmod(input_fd, 0o755)
            except BaseException as exc:
                for fd in (input_fd, run_fd):
                    if fd is not None:
                        os.close(fd)
                try:
                    if created:                                # only what this call created
                        _remove_run_dir(runs_fd, run_id)
                finally:
                    os.close(runs_fd)
                if isinstance(exc, OSError):
                    raise SandboxUnavailableError(
                        f"workspace could not be created ({type(exc).__name__}: {exc.strerror or exc})") from exc
                raise
        return cls(lease=lease, run_id=run_id, runs_fd=runs_fd, run_fd=run_fd, input_fd=input_fd, limits=limits)

    # ── paths (for a future read-only mount; never for generated source) ───
    @property
    def path(self) -> str:
        return os.path.join(self._lease.runs_path, self.run_id)

    @property
    def input_dir(self) -> str:
        return os.path.join(self.path, INPUT_DIR)

    # ── trusted host inputs ─────────────────────────────────────────────────
    def _require_open(self):
        if self.closing or self.cleaned or self._input_fd is None:
            raise SandboxUnavailableError("the workspace is closed (cleanup has started)")

    @staticmethod
    def _check_name(name):
        if name not in INPUT_NAMES:
            raise ValueError(f"unknown workspace input name {name!r}; allowed: {sorted(INPUT_NAMES)}")

    def create_input(self, name) -> int:
        """Exclusively create input `name`; return a write descriptor the caller closes."""
        self._check_name(name)
        self._require_open()
        fd = os.open(name, _CREATE_INPUT, 0o644, dir_fd=self._input_fd)
        os.fchmod(fd, 0o644)
        return fd

    def remove_input(self, name) -> None:
        self._check_name(name)
        if self._input_fd is None:           # closing or cleaned: the whole run is being removed
            return
        try:
            os.unlink(name, dir_fd=self._input_fd)
        except FileNotFoundError:
            pass

    def write_code(self, code) -> int:
        """Write generated code verbatim to code.py after checking its size. Returns bytes written."""
        if not isinstance(code, str):
            raise TypeError("generated code must be str")
        data = code.encode("utf-8")
        if len(data) > self.limits.code_bytes:
            raise SandboxPolicyError(
                f"generated code is {len(data)} bytes; the limit is {self.limits.code_bytes // MIB} MiB")
        fd = self.create_input(CODE_NAME)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            self.remove_input(CODE_NAME)
            raise
        os.close(fd)
        return len(data)

    def inputs(self) -> list:
        self._require_open()
        return sorted(os.listdir(self._input_fd))

    # ── cleanup ─────────────────────────────────────────────────────────────
    def cleanup(self) -> None:
        """
        Remove this run. Raises if the deletion fails; the workspace then
        stays closed to new input and cleanup() can be called again.
        """
        if self.cleaned:
            return
        self.closing = True                   # from here on no input can be written
        for attr in ("_input_fd", "_run_fd"):
            fd = getattr(self, attr)
            setattr(self, attr, None)
            if fd is not None:
                os.close(fd)
        try:
            _remove_run_dir(self._runs_fd, self.run_id, strict=True)
        except OSError as exc:                # not cleaned; the runs/ descriptor is kept for a retry
            raise SandboxUnavailableError(
                f"workspace cleanup failed ({type(exc).__name__}: {exc.strerror or exc}); "
                "call cleanup() again") from exc
        os.close(self._runs_fd)
        self._runs_fd = None
        self.cleaned = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False


def _remove_run_dir(runs_fd, run_id, strict=False) -> None:
    """Remove runs/<run_id> relative to runs_fd; never follows symlinks."""
    if not is_valid_run_id(run_id):
        raise SandboxUnavailableError("refusing to remove a non-run-id path")
    if not isinstance(runs_fd, int) or isinstance(runs_fd, bool):
        # dir_fd=None would resolve relative to the current directory
        raise SandboxUnavailableError("refusing to remove a run without its runs-directory descriptor")
    try:
        st = os.stat(run_id, dir_fd=runs_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(run_id, dir_fd=runs_fd)            # a planted symlink/file: remove the entry only
        if strict:
            raise SandboxUnavailableError("the run directory had been replaced; removed the entry only")
        return
    if shutil.rmtree.avoids_symlink_attacks:
        shutil.rmtree(run_id, dir_fd=runs_fd)
    else:                                            # conservative fallback
        fd = os.open(run_id, _OPEN_DIR, dir_fd=runs_fd)
        try:
            try:
                in_fd = os.open(INPUT_DIR, _OPEN_DIR, dir_fd=fd)
            except FileNotFoundError:
                in_fd = None
            if in_fd is not None:
                try:
                    for name in INPUT_NAMES:
                        try:
                            os.unlink(name, dir_fd=in_fd)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(in_fd)
                os.rmdir(INPUT_DIR, dir_fd=fd)
        finally:
            os.close(fd)
        os.rmdir(run_id, dir_fd=runs_fd)             # fails, leaving it in place, if anything unexpected remains
    try:
        os.stat(run_id, dir_fd=runs_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise SandboxUnavailableError("the run directory could not be removed")
