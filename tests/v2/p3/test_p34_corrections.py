"""
P3.4 security-review corrections.

FIX 1  The frame decoder validates chart.reject_reason's TYPE before any
       membership test: untrusted arrays/objects/numbers/booleans raise
       SandboxProtocolError, never TypeError. Tested against the real binary
       decoder (hand-edited headers), not only the encoder.
FIX 2  ChartStore.publish re-validates TrustedChart CONTENT and metadata at
       the publication boundary (field types, digest, strict PNG structure,
       bounded inflate, the store's limits, declared size == IHDR).
FIX 3  Failures after os.link() succeeded are reported explicitly as
       ChartPublishedWithCleanupError; the final chart is never removed or
       replaced, and another run's chart is never touched.

All fixtures are benign and hand-built; failures are injected
deterministically with monkeypatch. Nothing untrusted is executed.
"""

import hashlib
import io
import json
import os
import stat
import struct
import zlib

import pytest

from v2.execution import chart_store, frame
from v2.execution.artifacts import PNG_SIGNATURE, TrustedChart, reencode_png
from v2.execution.chart_store import ChartPublishedWithCleanupError, ChartStore
from v2.execution.errors import (ARTIFACT_REJECT_REASONS, ArtifactRejectedError, SandboxProtocolError,
                                 SandboxUnavailableError)
from v2.execution.limits import DEFAULT_LIMITS, Limits

RID = "0123456789abcdef" * 2
RID2 = "fedcba9876543210" * 2
ATT = frame.expected_attestation()


# ── FIX 1: reject_reason type safety in the binary decoder ───────────────────

def _frame_with_reason(reason):
    """A valid frame (exit 0, no chart) whose header carries an arbitrary reject_reason."""
    data = frame.encode_frame(run_id=RID, child_exit=0, child_signal=None, timed_out=False,
                              oom_killed=False, attestation=ATT, stdout=b"ok\n")
    (n,) = struct.unpack(">I", data[4:8])
    header, payload = json.loads(data[8:8 + n]), data[8 + n:]
    header["sections"]["chart"]["reject_reason"] = reason
    raw = json.dumps(header).encode()
    return frame.MAGIC + struct.pack(">I", len(raw)) + raw + payload


UNTRUSTED_REASONS = [[], ["symlink"], {}, {"symlink": 1}, [[]], 0, 1, -1, 2 ** 70, 1.5, float("1e308"),
                     True, False, "", "SYMLINK", "symlink ", "evil reason", "symlink\x00"]


@pytest.mark.parametrize("reason", UNTRUSTED_REASONS, ids=repr)
def test_untrusted_reject_reason_is_a_protocol_error_not_a_type_error(reason):
    data = _frame_with_reason(reason)
    for decode in (lambda: frame.decode_frame_bytes(data, RID),
                   lambda: frame.decode_frame(io.BytesIO(data), RID, DEFAULT_LIMITS)):
        try:
            decode()
        except SandboxProtocolError as exc:
            # Floats never reach the field check: the JSON parser already refuses them.
            assert ("floating-point" if isinstance(reason, float) else "reject_reason") in str(exc)
            assert not isinstance(exc, TypeError)
        except Exception as exc:                       # noqa: BLE001 - the point of the test
            pytest.fail(f"{type(exc).__name__} escaped the decoder for reject_reason={reason!r}: {exc}")
        else:
            pytest.fail(f"reject_reason={reason!r} was accepted")


@pytest.mark.parametrize("reason", UNTRUSTED_REASONS, ids=repr)
def test_encoder_refuses_untrusted_reject_reasons_the_same_way(reason):
    with pytest.raises(SandboxProtocolError, match="reject_reason"):
        frame.encode_frame(run_id=RID, child_exit=0, child_signal=None, timed_out=False,
                           oom_killed=False, attestation=ATT, chart_reject_reason=reason)


@pytest.mark.parametrize("reason", sorted(ARTIFACT_REJECT_REASONS))
def test_every_known_reject_reason_and_null_still_decode(reason):
    assert frame.decode_frame_bytes(_frame_with_reason(reason), RID).chart_reject_reason == reason
    assert frame.decode_frame_bytes(_frame_with_reason(None), RID).chart_reject_reason is None


# ── PNG fixtures ─────────────────────────────────────────────────────────────

def _chunk(ctype, payload=b""):
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload))


def _png(w=12, h=10, extra=(), tail=b""):
    rows = b"".join(b"\x00" + bytes((x * 7 + y) % 256 for x in range(w * 3)) for y in range(h))
    return (PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + b"".join(extra) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND") + tail)


def _forge(data, width, height):
    """A TrustedChart with a CORRECT digest over arbitrary bytes: what any in-process caller can build."""
    return TrustedChart(png=data, width=width, height=height, sha256=hashlib.sha256(data).hexdigest())


@pytest.fixture
def charts_dir(tmp_path):
    d = tmp_path / "charts"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    return d


@pytest.fixture
def trusted():
    return reencode_png(_png())


def _entries(d):
    return sorted(os.listdir(d))


# ── FIX 2: content and metadata are validated at publication ────────────────

def test_a_normal_canonical_chart_from_reencode_png_publishes(charts_dir, trusted):
    path = ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    with open(path, "rb") as f:
        assert f.read() == trusted.png
    assert os.lstat(path).st_nlink == 1 and _entries(charts_dir) == [f"t1-{RID}.png"]


VALID = _png()
INVALID_WITH_MATCHING_DIGEST = [
    (b"not a png at all", 12, 10, "bad_signature"),
    (VALID[:-5], 12, 10, "truncated"),
    (VALID[:40] + bytes([VALID[40] ^ 1]) + VALID[41:], 12, 10, "bad_crc"),
    (_png(extra=[_chunk(b"tEXt", b"Software\x00x")]), 12, 10, "disallowed_chunk"),   # metadata chunk
    (_png(extra=[_chunk(b"eXIf", b"MM\x00*")]), 12, 10, "disallowed_chunk"),
    (VALID + b"<script>polyglot</script>", 12, 10, "trailing_data"),
    (_png(w=12, h=10)[:33] + _chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + _chunk(b"IEND"), 12, 10,
     "decompression_mismatch"),
]


@pytest.mark.parametrize("data,w,h,reason", INVALID_WITH_MATCHING_DIGEST,
                         ids=[r for *_, r in INVALID_WITH_MATCHING_DIGEST])
def test_invalid_png_bytes_with_a_matching_digest_are_not_published(charts_dir, data, w, h, reason):
    with pytest.raises(ArtifactRejectedError) as info:
        ChartStore(str(charts_dir)).publish(_forge(data, w, h), "t1", RID)
    assert info.value.reason == reason
    assert _entries(charts_dir) == []


def test_raw_matplotlib_output_with_a_matching_digest_is_not_published(charts_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(2, 2), dpi=50)
    ax.plot([1, 2, 3])
    out = io.BytesIO()
    fig.savefig(out, format="png")
    plt.close(fig)
    with pytest.raises(ArtifactRejectedError) as info:
        ChartStore(str(charts_dir)).publish(_forge(out.getvalue(), 100, 100), "t1", RID)
    assert info.value.reason == "disallowed_chunk" and _entries(charts_dir) == []


@pytest.mark.parametrize("width,height", [(13, 10), (12, 11), (10, 12), (0, 0), (-12, 10), (12, 2 ** 40)])
def test_a_valid_png_with_wrong_declared_dimensions_is_not_published(charts_dir, trusted, width, height):
    lying = TrustedChart(png=trusted.png, width=width, height=height, sha256=trusted.sha256)
    with pytest.raises(ArtifactRejectedError) as info:
        ChartStore(str(charts_dir)).publish(lying, "t1", RID)
    assert info.value.reason == "bad_ihdr" and _entries(charts_dir) == []


@pytest.mark.parametrize("field,value", [
    ("png", "a string"), ("png", None), ("png", memoryview(b"x")),
    ("width", True), ("width", 12.0), ("width", "12"), ("width", None),
    ("height", False), ("height", 10.0),
    ("sha256", None), ("sha256", b"0" * 64), ("sha256", 0),
])
def test_malformed_field_types_are_refused(charts_dir, trusted, field, value):
    fields = dict(png=trusted.png, width=trusted.width, height=trusted.height, sha256=trusted.sha256)
    fields[field] = value
    with pytest.raises(TypeError, match="TrustedChart fields"):
        ChartStore(str(charts_dir)).publish(TrustedChart(**fields), "t1", RID)
    assert _entries(charts_dir) == []


def test_a_bytearray_png_is_refused_because_it_is_mutable(charts_dir, trusted):
    chart = TrustedChart(png=bytearray(trusted.png), width=12, height=10, sha256=trusted.sha256)
    with pytest.raises(TypeError):
        ChartStore(str(charts_dir)).publish(chart, "t1", RID)
    assert _entries(charts_dir) == []


def test_bytes_subclasses_are_refused(charts_dir, trusted):
    class Sneaky(bytes):
        pass
    chart = TrustedChart(png=Sneaky(trusted.png), width=12, height=10, sha256=trusted.sha256)
    with pytest.raises(TypeError):
        ChartStore(str(charts_dir)).publish(chart, "t1", RID)


def test_the_stores_own_limits_apply_at_publication(charts_dir):
    big = reencode_png(_png(w=60, h=60))                        # valid under the default limits
    with pytest.raises(ArtifactRejectedError) as info:
        ChartStore(str(charts_dir), Limits(chart_bytes=len(big.png) - 1)).publish(big, "t1", RID)
    assert info.value.reason == "too_large"
    with pytest.raises(ArtifactRejectedError) as info:
        ChartStore(str(charts_dir), Limits(chart_max_side=32, chart_max_pixels=1024)).publish(big, "t1", RID)
    assert info.value.reason == "dimensions_exceeded"
    assert _entries(charts_dir) == []
    with pytest.raises(TypeError):
        ChartStore(str(charts_dir), limits={"chart_bytes": 1})


def test_validation_happens_before_the_directory_is_touched(charts_dir, monkeypatch):
    opened = []
    monkeypatch.setattr(ChartStore, "_open_dir", lambda self: opened.append(1) or 1 / 0)
    with pytest.raises(ArtifactRejectedError):
        ChartStore(str(charts_dir)).publish(_forge(b"junk", 1, 1), "t1", RID)
    assert opened == []


# ── FIX 3: failures after publication are explicit and never destructive ────

class Inject:
    """Deterministic failure injection for the chart store's os calls."""

    def __init__(self, monkeypatch, unlink_failures=0, dir_fsync_fails=False):
        self.unlink_failures = unlink_failures          # -1: always
        self.dir_fsync_fails = dir_fsync_fails
        self.unlinked, self.unlink_attempts = [], 0
        self._unlink, self._fsync = os.unlink, os.fsync
        monkeypatch.setattr(os, "unlink", self.unlink)
        monkeypatch.setattr(os, "fsync", self.fsync)
        for name in ("replace", "rename", "remove", "rmdir"):
            monkeypatch.setattr(os, name, self.forbidden(name))

    def unlink(self, path, *a, **k):
        self.unlinked.append(path)
        if str(path).startswith(".tmp-"):
            self.unlink_attempts += 1
            if self.unlink_failures == -1 or self.unlink_attempts <= self.unlink_failures:
                raise OSError(5, "Input/output error")
        return self._unlink(path, *a, **k)

    def fsync(self, fd):
        if self.dir_fsync_fails and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(5, "Input/output error")
        return self._fsync(fd)

    @staticmethod
    def forbidden(name):
        def call(*a, **k):
            raise AssertionError(f"os.{name} must never be used by the chart store")
        return call


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def test_persistent_temp_removal_failure_is_explicit_and_keeps_the_published_chart(charts_dir, trusted,
                                                                                    monkeypatch):
    inj = Inject(monkeypatch, unlink_failures=-1)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    exc = info.value
    final = f"t1-{RID}.png"
    assert isinstance(exc, SandboxUnavailableError) and exc.published is True
    assert exc.path == os.path.join(str(charts_dir), final) and _read(exc.path) == trusted.png
    assert exc.leftover_temp and exc.leftover_temp.startswith(".tmp-") and exc.directory_synced is True
    assert inj.unlink_attempts == 2                                   # retried once, then reported
    assert final not in inj.unlinked                                   # the final name is never removed
    assert _entries(charts_dir) == sorted([final, exc.leftover_temp])
    assert "published" in str(exc) and exc.leftover_temp in str(exc)


def test_a_second_publication_cannot_corrupt_or_replace_the_original(charts_dir, trusted, monkeypatch):
    inj = Inject(monkeypatch, unlink_failures=-1)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    leftover = info.value.leftover_temp
    monkeypatch.undo()                                                 # the filesystem recovers
    other = reencode_png(_png(w=20, h=7))
    with pytest.raises(SandboxUnavailableError, match="not overwritten") as second:
        ChartStore(str(charts_dir)).publish(other, "t1", RID)
    assert not isinstance(second.value, ChartPublishedWithCleanupError)
    final = charts_dir / f"t1-{RID}.png"
    assert final.read_bytes() == trusted.png                           # original intact
    assert _entries(charts_dir) == sorted([final.name, leftover])      # the second's temp was removed
    assert inj.unlink_attempts == 2
    # The leftover is a second hard link to the published chart; removing it is safe.
    os.unlink(charts_dir / leftover)
    assert final.read_bytes() == trusted.png and os.lstat(final).st_nlink == 1


def test_a_transient_temp_removal_failure_is_retried_and_succeeds(charts_dir, trusted, monkeypatch):
    inj = Inject(monkeypatch, unlink_failures=1)
    path = ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    assert inj.unlink_attempts == 2 and _read(path) == trusted.png
    assert _entries(charts_dir) == [f"t1-{RID}.png"] and os.lstat(path).st_nlink == 1


def test_directory_sync_failure_is_reported_not_suppressed(charts_dir, trusted, monkeypatch):
    Inject(monkeypatch, dir_fsync_fails=True)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    exc = info.value
    assert exc.directory_synced is False and exc.leftover_temp is None
    assert _read(exc.path) == trusted.png and _entries(charts_dir) == [f"t1-{RID}.png"]
    assert "synced" in str(exc)


def test_both_cleanup_failures_are_reported_together(charts_dir, trusted, monkeypatch):
    Inject(monkeypatch, unlink_failures=-1, dir_fsync_fails=True)
    with pytest.raises(ChartPublishedWithCleanupError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    exc = info.value
    assert exc.leftover_temp and exc.directory_synced is False and _read(exc.path) == trusted.png


def _fail(name, errno_=28, when=lambda *a, **k: True):
    real = getattr(os, name)

    def call(*a, **k):
        if when(*a, **k):
            raise OSError(errno_, os.strerror(errno_))
        return real(*a, **k)
    return call


PRE_LINK = {
    "write": ("write", 28, lambda *a, **k: True),
    "file_fsync": ("fsync", 5, lambda fd: not stat.S_ISDIR(os.fstat(fd).st_mode)),
    "link_unsupported": ("link", 1, lambda *a, **k: True),
}


@pytest.mark.parametrize("case", sorted(PRE_LINK))
def test_failures_before_publication_are_plain_and_publish_nothing(charts_dir, trusted, monkeypatch, case):
    name, errno_, when = PRE_LINK[case]
    monkeypatch.setattr(os, name, _fail(name, errno_, when))
    with pytest.raises(SandboxUnavailableError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    assert not isinstance(info.value, ChartPublishedWithCleanupError)
    assert not getattr(info.value, "published", False)
    assert _entries(charts_dir) == []


def test_a_pre_publication_failure_that_also_cannot_remove_its_temp_says_so(charts_dir, trusted, monkeypatch):
    monkeypatch.setattr(os, "link", _fail("link", 1))
    Inject(monkeypatch, unlink_failures=-1)
    with pytest.raises(SandboxUnavailableError) as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    assert not isinstance(info.value, ChartPublishedWithCleanupError)
    leftovers = _entries(charts_dir)
    assert len(leftovers) == 1 and leftovers[0].startswith(".tmp-") and leftovers[0] in str(info.value)
    assert not any(e.endswith(f"-{RID}.png") for e in leftovers)       # nothing published


SCENARIOS = {
    "temp_removal_fails": dict(unlink_failures=-1),
    "dir_sync_fails": dict(dir_fsync_fails=True),
    "both_fail": dict(unlink_failures=-1, dir_fsync_fails=True),
    "transient": dict(unlink_failures=1),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_another_runs_chart_is_never_touched(charts_dir, trusted, monkeypatch, scenario):
    theirs = charts_dir / f"t1-{RID2}.png"
    theirs.write_bytes(b"another run's chart")
    inj = Inject(monkeypatch, **SCENARIOS[scenario])
    try:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    except ChartPublishedWithCleanupError:
        pass
    assert theirs.read_bytes() == b"another run's chart"
    assert theirs.name not in inj.unlinked and f"t1-{RID}.png" not in inj.unlinked
    assert (charts_dir / f"t1-{RID}.png").read_bytes() == trusted.png


def test_cleanup_error_is_only_raised_after_a_successful_link(charts_dir, trusted, monkeypatch):
    """A collision is a pre-publication outcome even when temp removal and dir sync would fail."""
    existing = charts_dir / f"t1-{RID}.png"
    existing.write_bytes(b"first")
    Inject(monkeypatch, unlink_failures=1, dir_fsync_fails=True)
    with pytest.raises(SandboxUnavailableError, match="not overwritten") as info:
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    assert not isinstance(info.value, ChartPublishedWithCleanupError)
    assert existing.read_bytes() == b"first" and _entries(charts_dir) == [existing.name]


def test_publication_outcomes_are_documented():
    doc = " ".join((ChartStore.publish.__doc__ + ChartPublishedWithCleanupError.__doc__ + chart_store.__doc__
                    + ChartStore._check_chart.__doc__).split())
    for phrase in ("NOTHING was published", "WAS published", "never deleted or replaced",
                   "does NOT prove", "ChartPublishedWithCleanupError"):
        assert phrase in doc, phrase
