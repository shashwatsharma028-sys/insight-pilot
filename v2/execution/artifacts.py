"""
Host-side chart validation and re-encoding (P3.4, design §10 stage B).

Input bytes are UNTRUSTED (they come from a frame a compromised container
could forge). Every length, chunk count, dimension and decompressed size is
treated as attacker-controlled:

1. Size cap BEFORE any parsing (Limits.chart_bytes).
2. Standard-library chunk walk: PNG signature; for every chunk the length is
   checked against the remaining bytes (and the PNG 2^31-1 limit) before it
   is used; CRC-32 verified; at most MAX_CHUNKS chunks. Only the canonical
   structure is allowed: IHDR (first, once, 13 bytes), then one or more
   contiguous IDAT, then IEND (empty, last) and nothing after it. Every other
   chunk (tEXt, iTXt, zTXt, iCCP, pHYs, eXIf, acTL/fcTL/fdAT for APNG,
   PLTE, unknown) is rejected, which also rejects appended polyglot data.
3. IHDR: 1..chart_max_side per side, width*height <= chart_max_pixels,
   bit depth 8, colour type 2 (RGB) or 6 (RGBA), compression, filter and
   interlace all 0.
4. Bounded inflate: the exact expected size h*(1 + w*channels) is computed
   from IHDR; decompression proceeds in bounded steps (max_length), never
   produces more than expected+1 bytes in total, keeps no pixel data (only
   the filter byte of each row is checked, 0..4), and must end exactly at
   the expected size with the zlib stream complete and no unused data.
5. Only then Pillow decodes it, restricted to the PNG plugin, with
   DecompressionBombWarning escalated to an error; format, size and mode
   must match IHDR. The pixels are re-encoded by the host encoder, without
   metadata, and the re-encoded bytes are validated again.

The original untrusted bytes are never returned or stored: only
TrustedChart.png, produced by the host encoder, is.

RESIDUAL RISK (do not overstate): validation shrinks, but does not remove,
the attack surface of the C libraries involved. zlib still inflates attacker
data (within bounds), and Pillow/libpng-equivalent decoding code still runs
on attacker-chosen pixel content (after the structure is proven canonical).
A memory-safety bug in zlib or Pillow could still be reachable. Running the
equivalent stage-A canonicalisation inside the container first (P3.5) and
keeping Pillow and zlib patched remain necessary.
"""

import hashlib
import io
import struct
import warnings
import zlib
from dataclasses import dataclass

from v2.execution.errors import ArtifactRejectedError
from v2.execution.limits import DEFAULT_LIMITS, Limits

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CHUNKS = 4096
MAX_CHUNK_LENGTH = 2 ** 31 - 1
INFLATE_STEP = 64 * 1024
CHANNELS = {2: 3, 6: 4}
MODES = {2: "RGB", 6: "RGBA"}


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    color_type: int

    @property
    def channels(self) -> int:
        return CHANNELS[self.color_type]

    @property
    def mode(self) -> str:
        return MODES[self.color_type]


@dataclass(frozen=True)
class TrustedChart:
    """PNG bytes produced by the HOST encoder from validated pixels."""
    png: bytes
    width: int
    height: int
    sha256: str


def _reject(reason: str):
    raise ArtifactRejectedError(reason)


def _chunks(data: bytes):
    """Yield (type, payload, end offset) for every chunk, with strict length and CRC checks."""
    pos, count, size = len(PNG_SIGNATURE), 0, len(data)
    while pos < size:
        count += 1
        if count > MAX_CHUNKS:
            _reject("too_many_chunks")
        if size - pos < 12:
            _reject("truncated")
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        if length > MAX_CHUNK_LENGTH or length > size - pos - 12:
            _reject("truncated")
        payload_end = pos + 8 + length
        payload = data[pos + 8:payload_end]
        (crc,) = struct.unpack(">I", data[payload_end:payload_end + 4])
        if zlib.crc32(ctype + payload) & 0xFFFFFFFF != crc:
            _reject("bad_crc")
        pos = payload_end + 4
        yield ctype, payload, pos


def validate_png_structure(data, limits: Limits = DEFAULT_LIMITS) -> PngInfo:
    """Strict structural validation; raises ArtifactRejectedError(reason)."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("chart data must be bytes")
    if len(data) == 0:
        _reject("empty")
    if len(data) > limits.chart_bytes:
        _reject("too_large")
    data = bytes(data)
    if not data.startswith(PNG_SIGNATURE):
        _reject("bad_signature")

    info = None
    state = "start"               # start -> ihdr -> idat -> end
    inflater = None
    expected = produced = 0
    row_len = 0
    for ctype, payload, end in _chunks(data):
        if ctype == b"IHDR":
            if state != "start" or len(payload) != 13:
                _reject("bad_ihdr")
            width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", payload)
            if depth != 8 or color not in CHANNELS or comp != 0 or filt != 0 or interlace != 0:
                _reject("bad_ihdr")
            if not (1 <= width <= limits.chart_max_side and 1 <= height <= limits.chart_max_side) \
                    or width * height > limits.chart_max_pixels:
                _reject("dimensions_exceeded")
            info = PngInfo(width=width, height=height, color_type=color)
            row_len = 1 + width * CHANNELS[color]
            expected = height * row_len
            inflater = zlib.decompressobj()
            state = "ihdr"
        elif ctype == b"IDAT":
            if state not in ("ihdr", "idat"):
                _reject("disallowed_chunk")
            state = "idat"
            produced = _inflate_step(inflater, payload, produced, expected, row_len)
        elif ctype == b"IEND":
            if state != "idat" or payload:
                _reject("disallowed_chunk")
            state = "end"
            if end != len(data):
                _reject("trailing_data")          # anything after IEND: extra chunks or polyglot bytes
            break
        else:
            _reject("disallowed_chunk")
    if state == "start":
        _reject("truncated")
    if state != "end":
        _reject("truncated")
    produced = _inflate_step(inflater, b"", produced, expected, row_len)
    if produced != expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        _reject("decompression_mismatch")
    return info


def _inflate_step(inflater, data, produced, expected, row_len):
    """Inflate `data` in bounded steps; check filter bytes; never keep pixels."""
    buf = data
    while True:
        if inflater.eof:
            if buf or inflater.unused_data:
                _reject("decompression_mismatch")      # data after the end of the zlib stream
            return produced
        budget = expected + 1 - produced                # never more than one byte past expected
        try:
            out = inflater.decompress(buf, min(INFLATE_STEP, budget))
        except zlib.error:
            _reject("decompression_mismatch")
        if out:
            start = produced
            produced += len(out)
            if produced > expected:
                _reject("decompression_mismatch")
            first = (-start) % row_len                  # offset of the first row start in `out`
            for i in range(first, len(out), row_len):
                if out[i] > 4:
                    _reject("decompression_mismatch")
        buf = inflater.unconsumed_tail
        if not out and not buf:
            return produced


def reencode_png(data, limits: Limits = DEFAULT_LIMITS) -> TrustedChart:
    """
    Validate untrusted PNG bytes, decode with a restricted Pillow workflow and
    re-encode the pixels with the host encoder. Returns a TrustedChart; the
    input bytes are never returned.
    """
    info = validate_png_structure(data, limits)
    from PIL import Image, ImageFile

    if ImageFile.LOAD_TRUNCATED_IMAGES:            # a global another library may have changed
        _reject("decode_failed")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(bytes(data)), formats=("PNG",)) as img:
                if img.format != "PNG" or img.size != (info.width, info.height) or img.mode != info.mode:
                    _reject("decode_failed")
                img.load()
                pixels = img.tobytes()
        if len(pixels) != info.width * info.height * info.channels:
            _reject("decode_failed")
        fresh = Image.frombytes(info.mode, (info.width, info.height), pixels)
        out = io.BytesIO()
        fresh.save(out, format="PNG", optimize=False, compress_level=6)
    except ArtifactRejectedError:
        raise
    except Exception:                               # any decoder failure is a rejection
        _reject("decode_failed")
    png = out.getvalue()
    check = validate_png_structure(png, limits)     # the host encoder's output must be canonical too
    if (check.width, check.height, check.color_type) != (info.width, info.height, info.color_type):
        _reject("decode_failed")
    return TrustedChart(png=png, width=info.width, height=info.height, sha256=hashlib.sha256(png).hexdigest())
