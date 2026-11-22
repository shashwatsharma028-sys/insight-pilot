"""
P3.4: strict host PNG validation, re-encoding and atomic chart storage.

All PNG fixtures are benign and hand-built (or rendered offline by
Matplotlib with the Agg backend). Nothing untrusted is executed.
"""

import ast
import hashlib
import inspect
import io
import os
import stat
import struct
import threading
import tracemalloc
import zlib

import pytest

from v2.execution import artifacts, chart_store
from v2.execution.artifacts import TrustedChart, reencode_png, validate_png_structure
from v2.execution.chart_store import ChartStore, chart_filename, safe_stem
from v2.execution.errors import ArtifactRejectedError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RID = "0123456789abcdef" * 2
RID2 = "fedcba9876543210" * 2
IS_ROOT = os.geteuid() == 0


# ── fixture builder ──────────────────────────────────────────────────────────

def chunk(ctype: bytes, payload: bytes = b"", crc=None) -> bytes:
    crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF if crc is None else crc
    return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)


def ihdr(w, h, depth=8, color=2, comp=0, filt=0, interlace=0):
    return chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, color, comp, filt, interlace))


def raw_rows(w, h, channels=3, filter_byte=0, seed=7):
    rows = bytearray()
    for y in range(h):
        rows.append(filter_byte)
        rows += bytes(((x * 31 + y * 17 + seed) % 256) for x in range(w * channels))
    return bytes(rows)


def png(w=8, h=6, color=2, rows=None, idat=None, before_idat=(), after_idat=(), split=1, tail=b"",
        header=None):
    channels = {2: 3, 6: 4}.get(color, 3)
    data = zlib.compress(rows if rows is not None else raw_rows(w, h, channels)) if idat is None else idat
    parts = [artifacts.PNG_SIGNATURE, header if header is not None else ihdr(w, h, color=color)]
    parts += list(before_idat)
    step = max(1, len(data) // split)
    for i in range(0, len(data), step):
        parts.append(chunk(b"IDAT", data[i:i + step]))
    parts += list(after_idat)
    parts.append(chunk(b"IEND"))
    return b"".join(parts) + tail


def rejected(data, reason, limits=DEFAULT_LIMITS):
    with pytest.raises(ArtifactRejectedError) as info:
        validate_png_structure(data, limits)
    assert info.value.reason == reason, info.value.reason


def pixels(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        return img.mode, img.size, img.tobytes()


# ── valid images ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("color,mode", [(2, "RGB"), (6, "RGBA")])
def test_canonical_png_is_accepted_and_reencoded(color, mode):
    original = png(w=17, h=9, color=color, split=3)
    info = validate_png_structure(original)
    assert (info.width, info.height, info.mode) == (17, 9, mode)
    trusted = reencode_png(original)
    assert isinstance(trusted, TrustedChart) and trusted.png != original    # never the untrusted bytes
    assert pixels(trusted.png) == pixels(original)                          # same pixels
    assert trusted.sha256 == hashlib.sha256(trusted.png).hexdigest()
    validate_png_structure(trusted.png)                                     # host output is canonical


def test_every_row_filter_type_is_accepted():
    w, h = 5, 5
    rows = bytearray()
    for y in range(h):
        rows.append(y)               # filters 0..4
        rows += bytes(w * 3)
    validate_png_structure(png(w=w, h=h, rows=bytes(rows)))


def _matplotlib_png():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(4, 3), dpi=80)
    ax.bar(["a", "b", "c"], [3, 1, 2])
    out = io.BytesIO()
    fig.savefig(out, format="png")
    plt.close(fig)
    return out.getvalue()


def test_raw_matplotlib_output_is_rejected_at_stage_b():
    # Correction B-6: Matplotlib writes metadata chunks; stage B only accepts canonical output.
    rejected(_matplotlib_png(), "disallowed_chunk")


def test_matplotlib_chart_after_stage_a_canonicalisation_is_accepted():
    from PIL import Image
    with Image.open(io.BytesIO(_matplotlib_png())) as img:
        img.load()
        canonical = io.BytesIO()
        Image.frombytes(img.mode, img.size, img.tobytes()).save(canonical, format="PNG")
    trusted = reencode_png(canonical.getvalue())
    assert (trusted.width, trusted.height) == (320, 240)


# ── size, type and signature ─────────────────────────────────────────────────

def test_size_limit_is_enforced_before_parsing():
    limits = Limits(chart_bytes=1000)
    rejected(b"not a png at all" * 100, "too_large", limits)                # not bad_signature
    rejected(png(w=40, h=40), "too_large", Limits(chart_bytes=200))


@pytest.mark.parametrize("data,reason", [
    (b"", "empty"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 100, "bad_signature"),                # JPEG
    (b"GIF89a" + b"\x00" * 100, "bad_signature"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "bad_signature"),
    (b"\x89PNG\r\n\x1a", "bad_signature"),
    (artifacts.PNG_SIGNATURE, "truncated"),
])
def test_unexpected_types_and_signatures(data, reason):
    rejected(data, reason)


def test_non_bytes_are_refused():
    with pytest.raises(TypeError):
        validate_png_structure("not bytes")


# ── chunk walking ────────────────────────────────────────────────────────────

def test_chunk_crc_corruption_is_detected_everywhere():
    good = png()
    for ctype in (b"IHDR", b"IDAT", b"IEND"):
        i = good.index(ctype)
        corrupted = bytearray(good)
        corrupted[i + 4 if ctype != b"IEND" else i + 4] ^= 0x01          # first payload byte or CRC
        rejected(bytes(corrupted), "bad_crc")


@pytest.mark.parametrize("forged_length", [0x7FFFFFFF, 0xFFFFFFFF, 10_000])
def test_forged_chunk_lengths_are_treated_as_untrusted(forged_length):
    good = png()
    i = good.index(b"IDAT") - 4
    forged = good[:i] + struct.pack(">I", forged_length) + good[i + 4:]
    rejected(forged, "truncated")


@pytest.mark.parametrize("cut", [-1, -6, -13, 33, 40])
def test_truncated_files_are_refused(cut):
    with pytest.raises(ArtifactRejectedError) as info:
        validate_png_structure(png()[:cut])
    assert info.value.reason in ("truncated", "bad_crc")


@pytest.mark.parametrize("extra", [b"tEXt", b"iTXt", b"zTXt", b"iCCP", b"pHYs", b"eXIf", b"PLTE", b"gAMA",
                                   b"acTL", b"fcTL", b"fdAT", b"abCd"])
def test_metadata_apng_and_unknown_chunks_are_refused(extra):
    rejected(png(before_idat=[chunk(extra, b"payload")]), "disallowed_chunk")


def test_chunk_order_is_canonical():
    sig, hdr = artifacts.PNG_SIGNATURE, ihdr(8, 6)
    idat, iend = chunk(b"IDAT", zlib.compress(raw_rows(8, 6))), chunk(b"IEND")
    rejected(sig + idat + hdr + iend, "disallowed_chunk")                   # IDAT before IHDR
    rejected(sig + hdr + hdr + idat + iend, "bad_ihdr")                     # IHDR twice
    rejected(sig + hdr + iend, "disallowed_chunk")                          # no IDAT
    rejected(sig + hdr + idat, "truncated")                                 # no IEND
    data = zlib.compress(raw_rows(8, 6))
    split = sig + hdr + chunk(b"IDAT", data[:10]) + chunk(b"tEXt", b"x") + chunk(b"IDAT", data[10:]) + iend
    rejected(split, "disallowed_chunk")                                     # non-contiguous IDAT
    rejected(sig + hdr + idat + chunk(b"IEND", b"x"), "disallowed_chunk")   # IEND with payload


@pytest.mark.parametrize("tail", [b"\x00", b"PK\x03\x04" + b"\x00" * 30, chunk(b"IDAT", b"")])
def test_anything_after_iend_is_refused(tail):
    rejected(png(tail=tail), "trailing_data")


def test_chunk_count_is_capped():
    data = zlib.compress(raw_rows(8, 6))
    many = [chunk(b"IDAT", b"")] * artifacts.MAX_CHUNKS
    body = artifacts.PNG_SIGNATURE + ihdr(8, 6) + b"".join(many) + chunk(b"IDAT", data) + chunk(b"IEND")
    rejected(body, "too_many_chunks")


# ── IHDR ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("header,reason", [
    (chunk(b"IHDR", b"\x00" * 12), "bad_ihdr"),
    (ihdr(8, 6, depth=16), "bad_ihdr"),
    (ihdr(8, 6, depth=1), "bad_ihdr"),
    (ihdr(8, 6, color=0), "bad_ihdr"),
    (ihdr(8, 6, color=3), "bad_ihdr"),
    (ihdr(8, 6, color=4), "bad_ihdr"),
    (ihdr(8, 6, comp=1), "bad_ihdr"),
    (ihdr(8, 6, filt=1), "bad_ihdr"),
    (ihdr(8, 6, interlace=1), "bad_ihdr"),
    (ihdr(0, 6), "dimensions_exceeded"),
    (ihdr(8, 0), "dimensions_exceeded"),
    (ihdr(8193, 1), "dimensions_exceeded"),
    (ihdr(1, 2 ** 31 - 1), "dimensions_exceeded"),
    (ihdr(8192, 4096), "dimensions_exceeded"),           # 33.5 Mpx > 16.8 Mpx
])
def test_ihdr_rules(header, reason):
    rejected(png(header=header), reason)


# ── bounded inflate ──────────────────────────────────────────────────────────

def test_decompression_bomb_is_stopped_within_bounds():
    bomb = zlib.compress(b"\x00" * (200 * 1024 * 1024), 9)                  # ~200 KiB -> 200 MiB
    assert len(bomb) < 250_000
    data = png(w=100, h=100, idat=bomb)
    tracemalloc.start()
    try:
        rejected(data, "decompression_mismatch")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024, f"peak Python allocation {peak} bytes"  # measured, not estimated


def test_maximum_declared_size_with_tiny_payload_cannot_allocate_the_image():
    # 4096 x 4096 RGBA declares 64 MiB of pixels but provides almost nothing.
    data = png(w=4096, h=4096, color=6, idat=zlib.compress(b"\x00" * 1000))
    tracemalloc.start()
    try:
        rejected(data, "decompression_mismatch")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


@pytest.mark.parametrize("make_idat", [
    lambda rows: zlib.compress(rows[:-1]),                                  # too short
    lambda rows: zlib.compress(rows + b"\x00"),                             # one byte too long
    lambda rows: zlib.compress(rows)[:-4],                                  # stream not finished
    lambda rows: zlib.compress(rows) + b"\x00\x01",                         # data after the stream
    lambda rows: b"not zlib at all",                                        # invalid stream
])
def test_inflated_size_and_stream_end_must_be_exact(make_idat):
    rejected(png(idat=make_idat(raw_rows(8, 6))), "decompression_mismatch")


@pytest.mark.parametrize("bad", [5, 7, 255])
def test_invalid_filter_bytes_are_refused(bad):
    rows = bytearray(raw_rows(8, 6))
    rows[3 * (1 + 8 * 3)] = bad                                             # filter byte of row 3
    rejected(png(rows=bytes(rows)), "decompression_mismatch")


# ── restricted Pillow decode ─────────────────────────────────────────────────

def test_pillow_failures_become_rejections(monkeypatch):
    from PIL import Image

    def broken_open(*a, **k):
        raise OSError("decoder error")
    monkeypatch.setattr(Image, "open", broken_open)
    with pytest.raises(ArtifactRejectedError) as info:
        reencode_png(png())
    assert info.value.reason == "decode_failed"


def test_truncated_image_loading_must_stay_disabled(monkeypatch):
    from PIL import ImageFile
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    with pytest.raises(ArtifactRejectedError) as info:
        reencode_png(png())
    assert info.value.reason == "decode_failed"


def test_reencoding_does_not_change_global_pillow_settings():
    from PIL import Image, ImageFile
    before = (Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES)
    reencode_png(png())
    assert (Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES) == before


def test_pillow_is_restricted_to_the_png_plugin():
    with open(os.path.join(ROOT, "v2", "execution", "artifacts.py"), encoding="utf-8") as f:
        src = f.read()
    assert 'formats=("PNG",)' in src


# ── chart store ──────────────────────────────────────────────────────────────

@pytest.fixture
def charts_dir(tmp_path):
    d = tmp_path / "charts"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    return d


@pytest.fixture
def trusted():
    return reencode_png(png(w=12, h=10))


def _entries(d):
    return sorted(os.listdir(d))


def test_publication_writes_the_trusted_bytes_under_a_server_generated_name(charts_dir, trusted):
    path = ChartStore(str(charts_dir)).publish(trusted, "sales_t1", RID)
    assert path == os.path.join(str(charts_dir), f"sales_t1-{RID}.png")
    with open(path, "rb") as f:
        assert f.read() == trusted.png
    st = os.lstat(path)
    assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and stat.S_IMODE(st.st_mode) == 0o644
    assert _entries(charts_dir) == [f"sales_t1-{RID}.png"]              # no temporary file left


@pytest.mark.parametrize("task_id", ["../escaped", "a/b", "/etc/passwd", "x" * 41, "naïve", "", None, 42,
                                     "t1\x00", "..", "CON"])
def test_unsafe_task_ids_become_deterministic_hashed_stems(task_id, charts_dir, trusted):
    stem = safe_stem(task_id)
    if isinstance(task_id, str) and task_id in ("CON",):
        assert stem == task_id                                            # simple safe name
    else:
        assert stem.startswith("task-") and len(stem) == 17 and stem == safe_stem(task_id)
    path = ChartStore(str(charts_dir)).publish(trusted, task_id, RID)
    assert os.path.dirname(path) == str(charts_dir) and "/" not in os.path.basename(path)


@pytest.mark.parametrize("run_id", ["", "short", "0" * 31, RID.upper(), "../" + RID[3:]])
def test_invalid_run_ids_are_refused(run_id, charts_dir, trusted):
    with pytest.raises(ValueError):
        chart_filename("t1", run_id)
    with pytest.raises(ValueError):
        ChartStore(str(charts_dir)).publish(trusted, "t1", run_id)


def test_only_trusted_charts_can_be_published(charts_dir, trusted):
    store = ChartStore(str(charts_dir))
    with pytest.raises(TypeError):
        store.publish(png(), "t1", RID)                                   # raw (untrusted) bytes
    forged = TrustedChart(png=b"\x89PNG forged", width=1, height=1, sha256=trusted.sha256)
    with pytest.raises(SandboxUnavailableError, match="digest"):
        store.publish(forged, "t1", RID)
    assert _entries(charts_dir) == []
    params = list(inspect.signature(ChartStore.publish).parameters)
    assert params == ["self", "chart", "task_id", "run_id"]               # no path parameter


def test_collision_never_overwrites_an_existing_chart(charts_dir, trusted):
    existing = charts_dir / f"t1-{RID}.png"
    existing.write_bytes(b"another run's chart")
    with pytest.raises(SandboxUnavailableError, match="not overwritten"):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    assert existing.read_bytes() == b"another run's chart"
    assert _entries(charts_dir) == [f"t1-{RID}.png"]                       # temporary file removed


def test_a_symlink_at_the_final_name_is_neither_followed_nor_replaced(charts_dir, tmp_path, trusted):
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    os.symlink(outside, charts_dir / f"t1-{RID}.png")
    with pytest.raises(SandboxUnavailableError, match="not overwritten"):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    assert outside.read_text() == "must survive"
    assert os.path.islink(charts_dir / f"t1-{RID}.png")


def test_concurrent_publication_of_the_same_name_has_exactly_one_winner(charts_dir):
    charts = [reencode_png(png(w=12, h=10 + i)) for i in range(8)]
    results, barrier = [], threading.Barrier(8)

    def worker(c):
        barrier.wait()
        try:
            results.append(("ok", ChartStore(str(charts_dir)).publish(c, "t1", RID), c.png))
        except SandboxUnavailableError as exc:
            results.append(("refused", str(exc), None))
    threads = [threading.Thread(target=worker, args=(c,)) for c in charts]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    winners = [r for r in results if r[0] == "ok"]
    assert len(winners) == 1 and len(results) == 8
    with open(winners[0][1], "rb") as f:
        assert f.read() == winners[0][2]                                   # the winner's bytes, intact
    assert _entries(charts_dir) == [f"t1-{RID}.png"]


def test_different_runs_get_different_files(charts_dir, trusted):
    store = ChartStore(str(charts_dir))
    a, b = store.publish(trusted, "t1", RID), store.publish(trusted, "t1", RID2)
    assert a != b and _entries(charts_dir) == sorted([os.path.basename(a), os.path.basename(b)])


def test_unsafe_chart_directories_are_refused(tmp_path, trusted, charts_dir):
    link = tmp_path / "linked_charts"
    os.symlink(charts_dir, link)
    for bad in (str(link), str(charts_dir) + "/", "relative/charts", str(charts_dir) + "/../charts"):
        with pytest.raises(SandboxUnavailableError):
            ChartStore(bad).publish(trusted, "t1", RID)
    os.chmod(charts_dir, 0o775)
    with pytest.raises(SandboxUnavailableError, match="group/world-writable"):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    afile = tmp_path / "afile"
    afile.write_text("x")
    with pytest.raises(SandboxUnavailableError):
        ChartStore(str(afile)).publish(trusted, "t1", RID)


@pytest.mark.skipif(not IS_ROOT, reason="changing ownership needs root (this Linux test environment is root)")
def test_foreign_owned_chart_directory_is_refused(charts_dir, trusted):
    os.chown(charts_dir, 12345, -1)
    with pytest.raises(SandboxUnavailableError, match="owned by this user"):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)


@pytest.mark.parametrize("failing", ["write", "fsync", "link"])
def test_interrupted_publication_leaves_no_partial_file(charts_dir, trusted, monkeypatch, failing):
    real = getattr(os, failing)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, failing, boom)
    with pytest.raises(SandboxUnavailableError):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.setattr(os, failing, real)
    assert calls["n"] >= 1
    assert _entries(charts_dir) == []


def test_link_unsupported_fails_closed_without_overwriting_rename(charts_dir, trusted, monkeypatch):
    calls = []
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(1, "Operation not permitted")))
    monkeypatch.setattr(os, "replace", lambda *a, **k: calls.append("replace"))
    monkeypatch.setattr(os, "rename", lambda *a, **k: calls.append("rename"))
    with pytest.raises(SandboxUnavailableError, match="no-overwrite publication is not possible"):
        ChartStore(str(charts_dir)).publish(trusted, "t1", RID)
    monkeypatch.undo()
    assert calls == [] and _entries(charts_dir) == []


def test_store_has_no_stale_chart_lookup_or_overwriting_rename():
    with open(os.path.join(ROOT, "v2", "execution", "chart_store.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    called = {getattr(n.func, "attr", getattr(n.func, "id", None)) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not {"replace", "rename", "listdir", "scandir", "exists", "glob", "rmtree"} & called
    assert "link" in called
