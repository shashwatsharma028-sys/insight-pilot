"""
P3.5a Task A: closing the charts-directory descriptor cannot hide the outcome.

The failure is injected deterministically: the wrapper really closes the
descriptor (as Linux does even when close() reports an error) and then
raises OSError(EIO), only for directory descriptors.
"""

import os
import stat
import struct
import zlib

import pytest

from v2.execution import chart_store
from v2.execution.artifacts import PNG_SIGNATURE, reencode_png
from v2.execution.chart_store import ChartPublishedWithCleanupError, ChartStore
from v2.execution.errors import ArtifactRejectedError, SandboxUnavailableError

RID = "0123456789abcdef" * 2
RID2 = "fedcba9876543210" * 2


def _chunk(ctype, payload=b""):
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


def _png(w=12, h=10, seed=0):
    rows = b"".join(b"\x00" + bytes((x * 7 + y + seed) % 256 for x in range(w * 3)) for y in range(h))
    return (PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND"))


@pytest.fixture
def charts_dir(tmp_path):
    d = tmp_path / "charts"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    return d


@pytest.fixture
def trusted():
    return reencode_png(_png())


class DirCloseFails:
    """Make close() of a directory descriptor report EIO after really closing it."""

    def __init__(self, monkeypatch, unlink_tmp_fails=False, link_fails=None):
        self.dir_closes, self.unlinked = [], []
        self._close, self._unlink, self._link = os.close, os.unlink, os.link
        self.unlink_tmp_fails = unlink_tmp_fails
        monkeypatch.setattr(os, "close", self.close)
        monkeypatch.setattr(os, "unlink", self.unlink)
        if link_fails is not None:
            monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(link_fails))
        for name in ("replace", "rename", "remove"):
            monkeypatch.setattr(os, name, self._forbidden(name))

    def close(self, fd):
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        self._close(fd)
        if is_dir:
            self.dir_closes.append(fd)
            raise OSError(5, "Input/output error")

    def unlink(self, path, *a, **k):
        self.unlinked.append(path)
        if self.unlink_tmp_fails and str(path).startswith(".tmp-"):
            raise OSError(5, "Input/output error")
        return self._unlink(path, *a, **k)

    @staticmethod
    def _forbidden(name):
        def call(*a, **k):
            raise AssertionError(f"os.{name} must never be used by the chart store")
        return call


def _open_fds():
    return set(os.listdir("/proc/self/fd"))


def test_close_failure_after_publication_is_reported_as_published(charts_dir, trusted, monkeypatch):
    before = _open_fds()
    inj = DirCloseFails(monkeypatch)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    exc = info.value
    assert exc.published is True and exc.directory_closed is False
    assert exc.leftover_temp is None and exc.directory_synced is True
    assert "descriptor could not be closed" in str(exc)
    with open(exc.path, "rb") as f:
        assert f.read() == trusted.png
    assert sorted(os.listdir(charts_dir)) == [f"t1-{RID}.png"]
    assert len(inj.dir_closes) == 1                                     # closed once, never retried
    assert f"t1-{RID}.png" not in inj.unlinked
    assert _open_fds() <= before                                        # no descriptor leaked


def test_a_retry_after_a_close_failure_cannot_overwrite_the_published_chart(charts_dir, trusted, monkeypatch):
    DirCloseFails(monkeypatch)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    other = reencode_png(_png(w=20, h=7, seed=3))
    with pytest.raises(SandboxUnavailableError, match="not overwritten") as second:
        ChartStore(str(charts_dir)).publish(other, "t1", RID)
    assert not isinstance(second.value, ChartPublishedWithCleanupError)
    with open(info.value.path, "rb") as f:
        assert f.read() == trusted.png
    assert sorted(os.listdir(charts_dir)) == [f"t1-{RID}.png"]


def test_close_and_temp_removal_failures_are_reported_together(charts_dir, trusted, monkeypatch):
    DirCloseFails(monkeypatch, unlink_tmp_fails=True)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    exc = info.value
    assert exc.directory_closed is False and exc.leftover_temp and exc.leftover_temp in str(exc)
    assert "descriptor could not be closed" in str(exc)
    assert sorted(os.listdir(charts_dir)) == sorted([f"t1-{RID}.png", exc.leftover_temp])


@pytest.mark.parametrize("case", ["collision", "link_unsupported", "write_fails"])
def test_close_failure_after_a_pre_publication_error_says_nothing_was_published(charts_dir, trusted,
                                                                                monkeypatch, case):
    theirs = charts_dir / f"t1-{RID2}.png"
    theirs.write_bytes(b"another run's chart")
    if case == "collision":
        (charts_dir / f"t1-{RID}.png").write_bytes(b"first")
        inj = DirCloseFails(monkeypatch)
    elif case == "link_unsupported":
        inj = DirCloseFails(monkeypatch, link_fails=OSError(1, "Operation not permitted"))
    else:
        inj = DirCloseFails(monkeypatch)
        monkeypatch.setattr(os, "write", lambda *a: (_ for _ in ()).throw(OSError(28, "No space left")))
    with pytest.raises(SandboxUnavailableError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    exc = info.value
    assert type(exc) is SandboxUnavailableError                         # never a bare OSError
    assert not isinstance(exc, ChartPublishedWithCleanupError) and not getattr(exc, "published", False)
    assert "nothing was published" in str(exc) and "descriptor could not be closed" in str(exc)
    assert isinstance(exc.__cause__, SandboxUnavailableError)          # the original outcome is kept
    assert len(inj.dir_closes) == 1
    assert theirs.read_bytes() == b"another run's chart"
    if case == "collision":
        assert "not overwritten" in str(exc)
        assert (charts_dir / f"t1-{RID}.png").read_bytes() == b"first"
        assert sorted(os.listdir(charts_dir)) == sorted([f"t1-{RID}.png", theirs.name])
    else:
        assert sorted(os.listdir(charts_dir)) == [theirs.name]         # nothing published, temp removed


def test_close_failure_is_not_reported_when_close_succeeds(charts_dir, trusted):
    before = _open_fds()
    path = ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    assert os.path.basename(path) == f"t1-{RID}.png" and _open_fds() <= before


def test_rejected_charts_never_open_or_close_the_directory(charts_dir, monkeypatch):
    inj = DirCloseFails(monkeypatch)
    bad = reencode_png(_png())
    forged = type(bad)(png=bad.png, width=bad.width + 1, height=bad.height, sha256=bad.sha256)
    with pytest.raises(ArtifactRejectedError):
        ChartStore(str(charts_dir)).publish(forged, "t1", RID)
    monkeypatch.undo()
    assert inj.dir_closes == [] and os.listdir(charts_dir) == []


def test_an_interrupt_still_closes_the_directory_and_propagates(charts_dir, trusted, monkeypatch):
    closes = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closes.append(stat.S_ISDIR(os.fstat(fd).st_mode)),
                                                 real_close(fd))[1])
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    before = _open_fds()
    with pytest.raises(KeyboardInterrupt):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    assert True in closes and _open_fds() <= before


def test_close_is_never_retried():
    import inspect
    src = inspect.getsource(chart_store._close_once)
    assert src.count("os.close(") == 1 and "for " not in src and "while " not in src
