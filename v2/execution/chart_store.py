"""
Atomic, no-overwrite chart publication (P3.4, design §10 stage B step 5).

- Accepts only a TrustedChart (bytes produced by the host re-encoder in
  v2/execution/artifacts.py). There is no API that takes a path, and a
  CHART_SAVED marker in untrusted output is never used as one.
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
- Any failure removes the temporary file (by its own name, relative to the
  directory descriptor). A pre-existing final name is never touched.
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

from v2.execution.artifacts import TrustedChart
from v2.execution.errors import SandboxUnavailableError
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
    def __init__(self, charts_dir: str):
        if not isinstance(charts_dir, str) or not os.path.isabs(charts_dir) \
                or os.path.normpath(charts_dir) != charts_dir:
            raise SandboxUnavailableError("the charts directory must be an absolute, normalised path")
        self.charts_dir = charts_dir

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

    def publish(self, chart: TrustedChart, task_id, run_id) -> str:
        """Publish a validated chart; return its final path. Never overwrites."""
        if not isinstance(chart, TrustedChart):
            raise TypeError("only a TrustedChart from artifacts.reencode_png() can be published")
        if hashlib.sha256(chart.png).hexdigest() != chart.sha256:
            raise SandboxUnavailableError("the chart bytes do not match their recorded digest")
        final = chart_filename(task_id, run_id)
        tmp = f".tmp-{secrets.token_hex(16)}.png"
        dir_fd = self._open_dir()
        tmp_created = False
        try:
            fd = os.open(tmp, _CREATE_TMP, 0o644, dir_fd=dir_fd)
            tmp_created = True
            try:
                os.fchmod(fd, 0o644)
                view = memoryview(chart.png)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            except FileExistsError:
                raise SandboxUnavailableError(
                    "a chart with this name already exists; it was not overwritten") from None
            except OSError as exc:
                raise SandboxUnavailableError(
                    f"atomic no-overwrite publication is not possible here ({exc.strerror})") from exc
            os.unlink(tmp, dir_fd=dir_fd)
            tmp_created = False
            try:
                os.fsync(dir_fd)          # durability only: the chart is already complete and published
            except OSError:
                pass
            return os.path.join(self.charts_dir, final)
        except OSError as exc:
            raise SandboxUnavailableError(f"chart publication failed ({exc.strerror or exc})") from exc
        finally:
            if tmp_created:
                try:
                    os.unlink(tmp, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
            os.close(dir_fd)
