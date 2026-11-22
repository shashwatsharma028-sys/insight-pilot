"""
Demo-only dataset authorisation (P3.3).

The only bytes that may ever be handed to a hardened runtime are
byte-identical copies of the four pinned demonstration datasets. Uploads,
arbitrary /tmp paths, other files in data/, and anything outside the
approved directory are refused (DatasetNotAuthorizedError).

How a dataset is authorised (descriptor-relative, no TOCTOU window):

1. String checks on the requested path: a str, absolute, already normalised
   (no ".", "..", "//" or trailing "/"), no control characters, basename is
   exactly an allowlisted demo filename, and its directory resolves to the
   approved data directory (a symlinked *directory* is accepted, correction
   B-5).
2. The bytes are never read through the requested path. They are read from
   the TRUSTED approved directory (opened O_DIRECTORY|O_NOFOLLOW, owned by
   this user, not group- or world-writable) plus the allowlisted name, opened
   O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY relative to that directory
   descriptor. A symlinked final component fails with ELOOP; a FIFO cannot
   block.
3. fstat on the open descriptor: regular file, link count 1, owned by this
   user, 0 < size <= Limits.dataset_bytes.
4. Copy into the workspace's fixed name `dataset.csv` (exclusive create,
   relative to the workspace input-directory descriptor), at most cap + 1
   bytes, computing SHA-256 while copying.
5. fstat again: device, inode, size, mtime and ctime unchanged, and the
   number of bytes copied equals the size. The digest must equal the pin.
6. On any failure the partial copy is removed (by its fixed name, relative to
   the workspace descriptor) and DatasetNotAuthorizedError is raised.

The requested path is never interpolated into generated Python; the runtime
only ever sees the fixed workspace name.

Platform: verified on Linux (P3.3 tests). The same POSIX calls exist on
macOS, but no equivalent guarantee is claimed there until it is tested.
"""

import hashlib
import os
import re
import stat
from dataclasses import dataclass

from v2.execution.errors import DatasetNotAuthorizedError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DATA_DIR = os.path.join(ROOT, "data")

# Exact filenames and SHA-256 pins of the demonstration datasets. A test
# cross-checks them against tests/v2/golden/expected/*.json.
DEMO_DATASETS = {
    "sales_data.csv": "c5ac406600fe86e5a929e5132af87e4737c7185c167adb5d7b15d8d2d5894563",
    "attendance_data.csv": "2fe0b859f92219a49a5431ff2403a471356b00bb6f66187a6eec2874da8bd26e",
    "student_data.csv": "3259adb09dbbedeafd91802f2285dcf3a89cde407088387e495d7dea99b4fc7f",
    "ecommerce_timeseries.csv": "102eccf6775442efc96a94fda0436cfcc96901a5089cb07bf32ba9729e94b09a",
}

WORKSPACE_DATASET_NAME = "dataset.csv"
COPY_CHUNK = 1024 * 1024

_SAFE_NAME = re.compile(r"[a-z0-9_]{1,64}\.csv")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")

_OPEN_DATASET = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)
_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class AuthorizedDataset:
    name: str                  # approved demo filename
    sha256: str                # verified digest of the copied bytes
    size: int
    workspace_name: str = WORKSPACE_DATASET_NAME


def _refuse(reason: str):
    raise DatasetNotAuthorizedError(f"Dataset not authorised: {reason}")


class DatasetAuthorizer:
    def __init__(self, data_dir: str = DEFAULT_DATA_DIR, approved=None, limits: Limits = DEFAULT_LIMITS,
                 _after_chunk=None):
        approved = dict(DEMO_DATASETS if approved is None else approved)
        for name, digest in approved.items():
            if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
                raise ValueError(f"unsafe approved dataset name: {name!r}")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError(f"approved digest for {name} must be 64 lowercase hex characters")
        if not isinstance(limits, Limits):
            raise TypeError("limits must be Limits")
        self._approved = approved
        self._limits = limits
        self._data_dir = os.path.realpath(data_dir)      # trusted, resolved once
        self._after_chunk = _after_chunk                  # test seam: called after each chunk

    @property
    def approved_names(self):
        return tuple(sorted(self._approved))

    # ── 1. string checks ────────────────────────────────────────────────────
    def check_path(self, csv_path) -> str:
        """Return the approved filename for `csv_path`, or refuse."""
        if not isinstance(csv_path, str) or not csv_path:
            _refuse("the path must be a non-empty string")
        if _CONTROL.search(csv_path):
            _refuse("the path contains control characters")
        if not os.path.isabs(csv_path):
            _refuse("the path must be absolute")
        if os.path.normpath(csv_path) != csv_path:
            _refuse("the path is not normalised (contains '.', '..', '//' or a trailing '/')")
        name = os.path.basename(csv_path)
        if name not in self._approved:
            _refuse("only the four bundled demonstration datasets are allowed in this build")
        if os.path.realpath(os.path.dirname(csv_path)) != self._data_dir:
            _refuse("the file is not in the approved demonstration-data directory")
        return name

    # ── 2-6. descriptor-relative open, verified copy ────────────────────────
    def authorize_into(self, csv_path, workspace) -> AuthorizedDataset:
        """
        Copy the approved dataset into `workspace` (a JobWorkspace) as dataset.csv.
        Policy failures raise DatasetNotAuthorizedError; I/O failures (for
        example a full disk) raise SandboxUnavailableError. Either way no
        partial copy is left behind.
        """
        try:
            return self._authorize_into(csv_path, workspace)
        except OSError as exc:
            raise SandboxUnavailableError(
                f"dataset copy failed ({type(exc).__name__}: {exc.strerror or exc})") from exc

    def _authorize_into(self, csv_path, workspace) -> AuthorizedDataset:
        name = self.check_path(csv_path)
        expected = self._approved[name]
        fds = []
        dst_fd = None
        created = False
        try:
            src_fd, before = self._open_source(name, fds)

            dst_fd = workspace.create_input(WORKSPACE_DATASET_NAME)
            created = True
            digest = hashlib.sha256()
            copied = 0
            cap = self._limits.dataset_bytes
            while True:
                chunk = os.read(src_fd, min(COPY_CHUNK, cap + 1 - copied))
                if not chunk:
                    break
                copied += len(chunk)
                if copied > cap:
                    _refuse("the dataset grew beyond the size limit while it was copied")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(dst_fd, view)
                    view = view[written:]
                if self._after_chunk is not None:
                    self._after_chunk(copied)

            after = os.fstat(src_fd)
            unchanged = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == \
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if not unchanged or copied != before.st_size:
                _refuse("the dataset changed while it was being copied")
            actual = digest.hexdigest()
            if actual != expected:
                _refuse("the content does not match the approved SHA-256")
            os.fsync(dst_fd)
            result = AuthorizedDataset(name=name, sha256=actual, size=copied)
            created = False          # keep the verified copy
            return result
        finally:
            for fd in [dst_fd] + fds[::-1]:
                if fd is not None:
                    os.close(fd)
            if created:
                workspace.remove_input(WORKSPACE_DATASET_NAME)

    # ── shared descriptor-relative open (used by the copy and by identify) ──
    def _open_source(self, name, fds):
        """
        Open the approved directory and `name` relative to it, with every
        ownership, type, link-count and size check. Each descriptor is
        appended to `fds` as soon as it exists; the caller closes them.
        Returns (src_fd, fstat before reading).
        """
        try:
            fds.append(os.open(self._data_dir, _OPEN_DIR))
        except OSError as exc:
            _refuse(f"the approved data directory cannot be opened safely ({exc.strerror})")
        dir_fd = fds[-1]
        dir_st = os.fstat(dir_fd)
        if dir_st.st_uid != os.geteuid() or dir_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _refuse("the approved data directory is not owned by this user or is group/world-writable")
        try:
            fds.append(os.open(name, _OPEN_DATASET, dir_fd=dir_fd))
        except OSError as exc:
            _refuse(f"the dataset cannot be opened safely ({exc.strerror})")
        src_fd = fds[-1]
        before = os.fstat(src_fd)
        if not stat.S_ISREG(before.st_mode):
            _refuse("not a regular file")
        if before.st_nlink != 1:
            _refuse(f"unexpected link count {before.st_nlink} (hard links are not allowed)")
        if before.st_uid != os.geteuid():
            _refuse("the file is not owned by this user")
        if not 0 < before.st_size <= self._limits.dataset_bytes:
            _refuse(f"size {before.st_size} is outside 1..{self._limits.dataset_bytes} bytes")
        return src_fd, before

    # ── staging by identity (P3.6-1; used by the container runtime) ──────────
    def authorize_identity_into(self, identity, workspace) -> AuthorizedDataset:
        """
        Stage the dataset named by a DatasetIdentity (never a caller path) into `workspace`.
        The copy is made by authorize_into from <trusted data dir>/<approved name>, and is kept only
        if THREE digests agree: the approved pin, identity.sha256 (verified by the dispatcher's
        identify() and profiled by P5), and the SHA-256 of the bytes actually copied; the size
        must equal identity.size. Otherwise the copy is removed and DatasetNotAuthorizedError raised.
        """
        from v2.execution.runtime import DatasetIdentity
        if not isinstance(identity, DatasetIdentity):
            raise TypeError("identity must be a DatasetIdentity")
        if identity.name not in self._approved:
            _refuse("only the four bundled demonstration datasets are allowed in this build")
        if identity.sha256 != self._approved[identity.name]:
            _refuse("the requested identity does not match the approved SHA-256")
        staged = self.authorize_into(os.path.join(self._data_dir, identity.name), workspace)
        if (staged.name, staged.sha256, staged.size) != (identity.name, identity.sha256, identity.size):
            workspace.remove_input(WORKSPACE_DATASET_NAME)
            _refuse("the staged copy does not match the requested identity")
        return staged

    # ── read-only identity check (P5.1A; used by the hardened dispatcher) ──
    def identify(self, csv_path) -> AuthorizedDataset:
        """
        Verify, WITHOUT copying, that `csv_path` names an approved dataset
        whose current bytes match its pin, using the same checks as
        authorize_into (string checks, descriptor-relative open, fstat before
        and after, bounded read, digest). Policy failures raise
        DatasetNotAuthorizedError; I/O failures raise SandboxUnavailableError.
        The returned identity carries no path.
        """
        try:
            return self._identify(csv_path)
        except OSError as exc:
            raise SandboxUnavailableError(
                f"dataset identity check failed ({type(exc).__name__}: {exc.strerror or exc})") from exc

    def _identify(self, csv_path) -> AuthorizedDataset:
        name = self.check_path(csv_path)
        expected = self._approved[name]
        fds = []
        try:
            src_fd, before = self._open_source(name, fds)
            digest = hashlib.sha256()
            read = 0
            cap = self._limits.dataset_bytes
            while True:
                chunk = os.read(src_fd, min(COPY_CHUNK, cap + 1 - read))
                if not chunk:
                    break
                read += len(chunk)
                if read > cap:
                    _refuse("the dataset grew beyond the size limit while it was read")
                digest.update(chunk)
                if self._after_chunk is not None:
                    self._after_chunk(read)
            after = os.fstat(src_fd)
            unchanged = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == \
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            if not unchanged or read != before.st_size:
                _refuse("the dataset changed while it was being read")
            actual = digest.hexdigest()
            if actual != expected:
                _refuse("the content does not match the approved SHA-256")
            return AuthorizedDataset(name=name, sha256=actual, size=read)
        finally:
            for fd in fds[::-1]:
                os.close(fd)
