"""
Atomic, no-overwrite chart publication (P3.4, design §10 stage B step 5).

- Accepts only a TrustedChart whose CONTENT is re-validated at publication
  (strict PNG structure, bounded inflate, size/dimension caps, metadata and
  field types; P3.4 correction). A TrustedChart is constructible by any code
  in this process, so the check proves the bytes are a canonical, bounded
  PNG of the declared size, not that the host encoder produced them. There
  is no API that takes a path, and a CHART_SAVED marker in untrusted output
  is never used as one.
- Final name: `<stem>-<run_id>.png`, server-generated. stem = task_id if it
  matches ^[A-Za-z0-9_-]{1,40}$, else task-<sha256(task_id)[:12]>. run_id is
  the full 128-bit run identifier (validated), so names are per-run unique.
- The charts directory is opened once per publication with O_DIRECTORY|
  O_NOFOLLOW (a symlinked final component is refused) and must be a
  directory owned by this user and not group/world-writable.
- Publication: write `.tmp-<random>.png` with O_CREAT|O_EXCL|O_NOFOLLOW
  relative to the directory descriptor, fsync, then os.link() it to the
  final name. link() never replaces an existing name (EEXIST), unlike
  os.replace()/rename(), so a collision can never overwrite another chart.
  The temporary name is then removed and the directory fsynced.
- Any failure BEFORE publication removes the temporary file (by its own
  name, relative to the directory descriptor) and means nothing was
  published. A pre-existing final name is never touched.
- After a successful link() the chart is published and is never removed or
  replaced. If removing the temporary file (retried once) or syncing the
  directory then fails, ChartPublishedWithCleanupError is raised: it carries
  the published path, the leftover temporary name (if any) and whether the
  directory was synced, so the outcome is never mistaken for a failure
  before publication and never silently suppressed (P3.4 correction).
- There is no stale-chart fallback: only the chart published by this call
  is ever returned.

Platform: verified on Linux. Filesystems without hard-link support make
publication fail closed (SandboxUnavailableError) rather than fall back to
an overwriting rename.

Not wired into the production dispatcher yet.
"""

import hashlib
import os
import re
import secrets
import stat

from v2.execution.artifacts import TrustedChart, validate_png_structure
from v2.execution.errors import ArtifactRejectedError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import is_valid_run_id

_SAFE_STEM = re.compile(r"[A-Za-z0-9_-]{1,40}")
_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_TMP = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def safe_stem(task_id) -> str:
    if isinstance(task_id, str) and _SAFE_STEM.fullmatch(task_id):
        return task_id
    return "task-" + hashlib.sha256(repr(task_id).encode("utf-8", "backslashreplace")).hexdigest()[:12]


def chart_filename(task_id, run_id) -> str:
    if not is_valid_run_id(run_id):
        raise ValueError("run_id must be 32 lowercase hex characters")
    return f"{safe_stem(task_id)}-{run_id}.png"


class ChartStore:
    def __init__(self, charts_dir: str, limits: Limits = DEFAULT_LIMITS):
        if not isinstance(charts_dir, str) or not os.path.isabs(charts_dir) \
                or os.path.normpath(charts_dir) != charts_dir:
            raise SandboxUnavailableError("the charts directory must be an absolute, normalised path")
        if not isinstance(limits, Limits):
            raise TypeError("limits must be Limits")
        self.charts_dir = charts_dir
        self.limits = limits

    def _open_dir(self) -> int:
        try:
            fd = os.open(self.charts_dir, _OPEN_DIR)
        except OSError as exc:
            raise SandboxUnavailableError(
                f"the charts directory cannot be opened safely ({exc.strerror})") from exc
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() \
                or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            os.close(fd)
            raise SandboxUnavailableError(
                "the charts directory must be owned by this user and not group/world-writable")
        return fd

    def _check_chart(self, chart) -> None:
        """
        Validate CONTENT at the publication boundary. TrustedChart is an
        ordinary dataclass: anyone in this process can construct one, so its
        type and digest prove nothing about the bytes. The bytes are therefore
        re-validated with the same strict structural and bounded-inflate checks
        as artifacts.validate_png_structure, and the declared metadata must
        match them. This proves the chart is a canonical, bounded PNG of the
        declared size; it does NOT prove it was produced by the host encoder.
        """
        if not isinstance(chart, TrustedChart):
            raise TypeError("only a TrustedChart from artifacts.reencode_png() can be published")
        if type(chart.png) is not bytes or type(chart.width) is not int or type(chart.height) is not int \
                or type(chart.sha256) is not str:
            raise TypeError("TrustedChart fields must be png: bytes, width: int, height: int, sha256: str")
        if hashlib.sha256(chart.png).hexdigest() != chart.sha256:
            raise SandboxUnavailableError("the chart bytes do not match their recorded digest")
        info = validate_png_structure(chart.png, self.limits)      # raises ArtifactRejectedError
        if (info.width, info.height) != (chart.width, chart.height):
            raise ArtifactRejectedError("bad_ihdr")                # metadata disagrees with the image

    def publish(self, chart: TrustedChart, task_id, run_id) -> str:
        """
        Publish a validated chart; return its final path. Never overwrites.

        Outcomes:
        - returns the path: published, temporary file removed, directory synced;
        - SandboxUnavailableError (or a subclass other than the one below), or
          ArtifactRejectedError/TypeError/ValueError: NOTHING was published;
        - ChartPublishedWithCleanupError: the chart WAS published, complete and
          canonical, at exc.path, but a later step failed (the temporary file
          could not be removed: exc.leftover_temp; or the directory could not
          be synced: exc.directory_synced is False). The final chart is never
          deleted or replaced in this case.
        """
        self._check_chart(chart)
        final = chart_filename(task_id, run_id)
        tmp = f".tmp-{secrets.token_hex(16)}.png"
        dir_fd = self._open_dir()
        try:
            created = False
            try:
                fd = os.open(tmp, _CREATE_TMP, 0o644, dir_fd=dir_fd)
                created = True
                try:
                    os.fchmod(fd, 0o644)
                    view = memoryview(chart.png)
                    while view:
                        view = view[os.write(fd, view):]
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                note = _discard_note(dir_fd, tmp) if created else ""
                raise SandboxUnavailableError(
                    f"chart publication failed before publishing ({exc.strerror or exc}){note}") from exc

            try:
                os.link(tmp, final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            except FileExistsError:
                note = _discard_note(dir_fd, tmp)
                raise SandboxUnavailableError(
                    f"a chart with this name already exists; it was not overwritten{note}") from None
            except OSError as exc:
                note = _discard_note(dir_fd, tmp)
                raise SandboxUnavailableError(
                    f"atomic no-overwrite publication is not possible here ({exc.strerror}){note}") from exc

            # Published. From here on the final chart is never removed or replaced.
            path = os.path.join(self.charts_dir, final)
            problems, leftover, synced = [], None, True
            if not _discard(dir_fd, tmp, attempts=2):
                leftover = tmp
                problems.append(f"the temporary file {tmp} could not be removed")
            try:
                os.fsync(dir_fd)
            except OSError as exc:
                synced = False
                problems.append(f"the charts directory could not be synced ({exc.strerror or exc})")
            if problems:
                raise ChartPublishedWithCleanupError(path, leftover_temp=leftover, directory_synced=synced,
                                                     detail="; ".join(problems))
            return path
        finally:
            os.close(dir_fd)


class ChartPublishedWithCleanupError(SandboxUnavailableError):
    """
    The chart WAS published (complete, validated) at `.path`; a cleanup step
    after publication failed. Callers may use `.path`, should log the problem,
    and must not retry publication under the same name (it would be refused,
    never overwrite). A leftover `.tmp-*.png` file is never referenced and
    may be removed by an operator.
    """

    def __init__(self, path, *, leftover_temp, directory_synced, detail):
        self.path = path
        self.leftover_temp = leftover_temp
        self.directory_synced = directory_synced
        self.published = True
        super().__init__(f"chart published at {os.path.basename(path)}, but cleanup failed: {detail}")


def _discard(dir_fd, tmp, attempts=1) -> bool:
    """Remove this call's own temporary file; True if it is gone."""
    for _ in range(attempts):
        try:
            os.unlink(tmp, dir_fd=dir_fd)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            continue
    return False


def _discard_note(dir_fd, tmp) -> str:
    return "" if _discard(dir_fd, tmp, attempts=2) else f"; the temporary file {tmp} could not be removed"
