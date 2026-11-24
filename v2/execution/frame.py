"""
IPX1 result-frame protocol (P3.4): one module, both directions.

    b"IPX1" | u32 big-endian header length | UTF-8 JSON header | stdout | stderr | chart

The future in-container supervisor writes exactly one frame after every
untrusted process is dead (design §8.4-8.5). The host treats the frame as
UNTRUSTED: a compromised container could forge it. decode_frame() therefore:

- reads at most the frame budget, incrementally, and never trusts a declared
  length: the header length and every section length are checked against
  their own caps BEFORE any payload is read, and short reads are detected;
- parses the header as strict JSON (UTF-8, no BOM, no duplicate keys, no
  floats/NaN/Infinity), with an exact key set at every level and exact
  types (bool is never accepted as int);
- checks every section's SHA-256, the run id, status/flag consistency and
  truncation flags, and rejects trailing bytes;
- builds plain data only (bytes, ints, str, bool): nothing is evaluated,
  imported or deserialised into executable objects.

Any violation raises SandboxProtocolError. Attestation that is well formed
but different from the expected isolation raises SandboxUnavailableError
(verify_attestation). Neither ever falls back to the V1 executor.

Not wired into the production dispatcher yet (P3.6).
"""

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from typing import Optional

from v2.execution.errors import ARTIFACT_REJECT_REASONS, SandboxProtocolError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import is_valid_run_id

MAGIC = b"IPX1"
SCHEMA = 1
PREFIX_BYTES = 8                       # magic + u32 header length
READ_CHUNK = 64 * 1024
SIGKILL = 9
MAX_SIGNAL = 64

TOP_KEYS = frozenset({"schema", "run_id", "status", "attestation", "sections", "out_other_entries"})
STATUS_KEYS = frozenset({"child_exit", "child_signal", "timed_out", "oom_killed"})
SECTION_NAMES = ("stdout", "stderr", "chart")            # fixed payload order
TEXT_SECTION_KEYS = frozenset({"length", "sha256", "truncated"})
CHART_SECTION_KEYS = frozenset({"length", "sha256", "reject_reason"})
ATTESTATION_KEYS = frozenset({
    "uid", "gid", "cap_eff", "cap_prm", "cap_bnd", "no_new_privs", "seccomp",
    "net_interfaces", "writable_mounts", "dev_writable",
    "memory_max", "memory_swap_max", "pids_max", "cpu_max",
})

_HEX64 = re.compile(r"[0-9a-f]{64}")
_CAP = re.compile(r"[0-9a-f]{16}")
_IFACE = re.compile(r"[A-Za-z0-9_.@-]{1,15}")
_CPU_MAX = re.compile(r"(max|[0-9]{1,12}) [0-9]{1,12}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MARKER = "CHART_SAVED:"


@dataclass(frozen=True)
class DecodedFrame:
    run_id: str
    child_exit: Optional[int]
    child_signal: Optional[int]
    timed_out: bool
    oom_killed: bool
    attestation: dict
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    chart: Optional[bytes]
    chart_reject_reason: Optional[str]
    out_other_entries: int

    @property
    def succeeded(self) -> bool:
        return self.child_exit == 0


def _fail(message: str):
    raise SandboxProtocolError(f"invalid result frame: {message}")


# ── strict JSON ──────────────────────────────────────────────────────────────

def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            _fail(f"duplicate header key {key!r}")
        out[key] = value
    return out


def _no_floats(text):
    _fail("floating-point numbers are not allowed in the header")


def _no_constants(text):
    _fail(f"non-finite constant {text} is not allowed in the header")


def _parse_header(raw: bytes) -> dict:
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("the header must not start with a byte-order mark")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("the header is not valid UTF-8")
    try:
        header = json.loads(text, object_pairs_hook=_no_duplicates, parse_float=_no_floats,
                            parse_constant=_no_constants)
    except SandboxProtocolError:
        raise
    except (ValueError, RecursionError):
        _fail("the header is not valid JSON")
    if not isinstance(header, dict):
        _fail("the header must be a JSON object")
    return header


# ── field validation (shared by encoder and decoder) ─────────────────────────

def _exact_keys(obj, keys, where):
    if not isinstance(obj, dict):
        _fail(f"{where} must be an object")
    if set(obj) != keys:
        missing, extra = sorted(keys - set(obj)), sorted(set(obj) - keys)
        _fail(f"{where} keys differ (missing {missing}, unexpected {extra})")


def _int(value, where, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        _fail(f"{where} must be an integer in {lo}..{hi}")
    return value


def _bool(value, where):
    if type(value) is not bool:
        _fail(f"{where} must be a boolean")
    return value


def _sha(value, where):
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        _fail(f"{where} must be 64 lowercase hex characters")
    return value


def _cgroup_value(value, where):
    if value == "max":
        return value
    return _int(value, where, 0, 2 ** 63 - 1)


def validate_attestation_shape(att) -> dict:
    _exact_keys(att, ATTESTATION_KEYS, "attestation")
    _int(att["uid"], "attestation.uid", 0, 2 ** 32 - 1)
    _int(att["gid"], "attestation.gid", 0, 2 ** 32 - 1)
    for key in ("cap_eff", "cap_prm", "cap_bnd"):
        if not isinstance(att[key], str) or not _CAP.fullmatch(att[key]):
            _fail(f"attestation.{key} must be 16 lowercase hex characters")
    _int(att["no_new_privs"], "attestation.no_new_privs", 0, 1)
    _int(att["seccomp"], "attestation.seccomp", 0, 2)
    for key, pattern, max_len in (("net_interfaces", _IFACE, 15), ("writable_mounts", None, 256)):
        items = att[key]
        if not isinstance(items, list) or len(items) > 64:
            _fail(f"attestation.{key} must be a list of at most 64 strings")
        for item in items:
            if not isinstance(item, str) or not item or len(item) > max_len or _CONTROL.search(item):
                _fail(f"attestation.{key} contains an invalid entry")
            if pattern is not None and not pattern.fullmatch(item):
                _fail(f"attestation.{key} contains an invalid entry")
            if pattern is None and not item.startswith("/"):
                _fail("attestation.writable_mounts entries must be absolute paths")
        if items != sorted(set(items)):
            _fail(f"attestation.{key} must be sorted and unique")
    _bool(att["dev_writable"], "attestation.dev_writable")
    for key in ("memory_max", "memory_swap_max", "pids_max"):
        _cgroup_value(att[key], f"attestation.{key}")
    if not isinstance(att["cpu_max"], str) or not _CPU_MAX.fullmatch(att["cpu_max"]):
        _fail("attestation.cpu_max must look like '<quota|max> <period>'")
    return att


def _validate_header(header, run_id, limits: Limits) -> dict:
    _exact_keys(header, TOP_KEYS, "header")
    if type(header["schema"]) is not int or header["schema"] != SCHEMA:
        _fail(f"unknown schema version {header['schema']!r}")
    if not is_valid_run_id(header["run_id"]):
        _fail("run_id must be 32 lowercase hex characters")
    if run_id is not None and header["run_id"] != run_id:
        _fail("the frame belongs to a different run")

    status = header["status"]
    _exact_keys(status, STATUS_KEYS, "status")
    exit_code, signal = status["child_exit"], status["child_signal"]
    if exit_code is not None:
        _int(exit_code, "status.child_exit", 0, 255)
    if signal is not None:
        _int(signal, "status.child_signal", 1, MAX_SIGNAL)
    if (exit_code is None) == (signal is None):
        _fail("exactly one of status.child_exit and status.child_signal must be set")
    timed_out = _bool(status["timed_out"], "status.timed_out")
    oom = _bool(status["oom_killed"], "status.oom_killed")
    if timed_out and oom:
        _fail("a run cannot be both timed out and OOM-killed")
    if (timed_out or oom) and signal != SIGKILL:
        _fail("a timed-out or OOM-killed run must have been terminated by SIGKILL")

    validate_attestation_shape(header["attestation"])

    sections = header["sections"]
    _exact_keys(sections, frozenset(SECTION_NAMES), "sections")
    caps = {"stdout": limits.stdout_bytes, "stderr": limits.stderr_bytes, "chart": limits.chart_bytes}
    for name in ("stdout", "stderr"):
        sec = sections[name]
        _exact_keys(sec, TEXT_SECTION_KEYS, f"sections.{name}")
        length = _int(sec["length"], f"sections.{name}.length", 0, caps[name])
        _sha(sec["sha256"], f"sections.{name}.sha256")
        if _bool(sec["truncated"], f"sections.{name}.truncated") and length != caps[name]:
            _fail(f"a truncated {name} section must be exactly its cap ({caps[name]} bytes)")
    chart = sections["chart"]
    _exact_keys(chart, CHART_SECTION_KEYS, "sections.chart")
    chart_length = _int(chart["length"], "sections.chart.length", 0, caps["chart"])
    if chart_length:
        _sha(chart["sha256"], "sections.chart.sha256")
    elif chart["sha256"] is not None:
        _fail("an empty chart section must have sha256 null")
    reason = chart["reject_reason"]
    # Type first: an untrusted array/object is unhashable, so a bare membership
    # test would raise TypeError instead of a protocol error.
    if reason is not None and (type(reason) is not str or reason not in ARTIFACT_REJECT_REASONS):
        _fail("sections.chart.reject_reason must be null or a known reason code")
    if chart_length and reason is not None:
        _fail("a chart cannot be both present and rejected")
    if (chart_length or reason is not None) and exit_code != 0:
        _fail("a chart is only examined after a successful run")

    _int(header["out_other_entries"], "out_other_entries", 0, limits.out_inodes)
    return header


# ── encoder (used by the future in-container supervisor and by tests) ──────

def encode_frame(*, run_id, child_exit, child_signal, timed_out, oom_killed, attestation,
                 stdout=b"", stderr=b"", stdout_truncated=False, stderr_truncated=False,
                 chart=None, chart_reject_reason=None, out_other_entries=0,
                 limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Build a frame. Validates with the decoder's own rules, so it cannot emit an invalid frame."""
    for name, value in (("stdout", stdout), ("stderr", stderr)):
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError(f"{name} must be bytes")
    chart = b"" if chart is None else chart
    if not isinstance(chart, (bytes, bytearray)):
        raise TypeError("chart must be bytes or None")
    header = {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": {"child_exit": child_exit, "child_signal": child_signal,
                   "timed_out": timed_out, "oom_killed": oom_killed},
        "attestation": attestation,
        "sections": {
            "stdout": {"length": len(stdout), "sha256": hashlib.sha256(stdout).hexdigest(),
                       "truncated": stdout_truncated},
            "stderr": {"length": len(stderr), "sha256": hashlib.sha256(stderr).hexdigest(),
                       "truncated": stderr_truncated},
            "chart": {"length": len(chart), "sha256": hashlib.sha256(chart).hexdigest() if chart else None,
                      "reject_reason": chart_reject_reason},
        },
        "out_other_entries": out_other_entries,
    }
    _validate_header(header, run_id, limits)
    raw = json.dumps(header, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if not 0 < len(raw) <= limits.frame_header_bytes:
        raise SandboxProtocolError("the frame header exceeds its size limit")
    return MAGIC + struct.pack(">I", len(raw)) + raw + bytes(stdout) + bytes(stderr) + bytes(chart)


# ── decoder (host side, untrusted input) ─────────────────────────────────────

class _BoundedReader:
    def __init__(self, stream, budget):
        self._stream = stream
        self._budget = budget
        self.consumed = 0

    def read_exact(self, n: int) -> bytes:
        if n < 0 or self.consumed + n > self._budget:
            _fail("the frame exceeds its byte budget")
        parts, remaining = [], n
        while remaining:
            chunk = self._stream.read(min(READ_CHUNK, remaining))
            if not chunk:
                _fail(f"the frame is truncated ({n - remaining} of {n} bytes)")
            if len(chunk) > remaining:          # a misbehaving stream
                _fail("the stream returned more bytes than requested")
            parts.append(chunk)
            remaining -= len(chunk)
        self.consumed += n
        return b"".join(parts)

    def read_section(self, n: int, expected_sha: Optional[str]) -> bytes:
        if self.consumed + n > self._budget:
            _fail("the frame exceeds its byte budget")
        digest, out, remaining = hashlib.sha256(), bytearray(), n
        while remaining:
            chunk = self._stream.read(min(READ_CHUNK, remaining))
            if not chunk:
                _fail(f"the frame is truncated inside a section ({n - remaining} of {n} bytes)")
            if len(chunk) > remaining:
                _fail("the stream returned more bytes than requested")
            digest.update(chunk)
            out += chunk
            remaining -= len(chunk)
        self.consumed += n
        if expected_sha is not None and digest.hexdigest() != expected_sha:
            _fail("a section's SHA-256 does not match its header")
        return bytes(out)

    def expect_end(self):
        if self._stream.read(1):
            _fail("trailing bytes after the last section")


def decode_frame(stream, run_id: str, limits: Limits = DEFAULT_LIMITS, expected_attestation=None) -> DecodedFrame:
    """
    Decode one frame from a binary stream (anything with .read(n)). Reads at
    most PREFIX_BYTES + limits.max_frame_bytes + 1 bytes.
    """
    if not is_valid_run_id(run_id):
        raise ValueError("run_id must be 32 lowercase hex characters")
    reader = _BoundedReader(stream, PREFIX_BYTES + limits.max_frame_bytes)
    if reader.read_exact(4) != MAGIC:
        _fail("wrong magic")
    (header_len,) = struct.unpack(">I", reader.read_exact(4))
    if not 0 < header_len <= limits.frame_header_bytes:
        _fail(f"header length {header_len} is outside 1..{limits.frame_header_bytes}")
    header = _validate_header(_parse_header(reader.read_exact(header_len)), run_id, limits)
    sections = header["sections"]
    if header_len + sum(sections[n]["length"] for n in SECTION_NAMES) > limits.max_frame_bytes:
        _fail("the declared sections exceed the frame budget")      # cannot happen with valid caps

    stdout = reader.read_section(sections["stdout"]["length"], sections["stdout"]["sha256"])
    stderr = reader.read_section(sections["stderr"]["length"], sections["stderr"]["sha256"])
    chart_len = sections["chart"]["length"]
    chart = reader.read_section(chart_len, sections["chart"]["sha256"]) if chart_len else None
    reader.expect_end()

    status = header["status"]
    frame = DecodedFrame(
        run_id=header["run_id"], child_exit=status["child_exit"], child_signal=status["child_signal"],
        timed_out=status["timed_out"], oom_killed=status["oom_killed"],
        attestation=dict(header["attestation"]),
        stdout=stdout, stderr=stderr,
        stdout_truncated=sections["stdout"]["truncated"], stderr_truncated=sections["stderr"]["truncated"],
        chart=chart, chart_reject_reason=sections["chart"]["reject_reason"],
        out_other_entries=header["out_other_entries"])
    if expected_attestation is not None:
        verify_attestation(frame.attestation, expected_attestation)
    return frame


def decode_frame_bytes(data: bytes, run_id: str, limits: Limits = DEFAULT_LIMITS, expected_attestation=None):
    import io
    if len(data) > PREFIX_BYTES + limits.max_frame_bytes:
        _fail("the frame exceeds its byte budget")
    return decode_frame(io.BytesIO(data), run_id, limits, expected_attestation)


# ── attestation policy ───────────────────────────────────────────────────────

CPU_PERIOD_US = 100_000
CONTAINER_UID = CONTAINER_GID = 10001


# P3.6-4B D2 (approved, Option A): rootless Podman 4.9.3 cannot create device nodes, so it bind-mounts the six
# standard host character devices into the read-only /dev, and it mounts a container-private devpts and the
# mqueue filesystem of the container's private IPC namespace. Each appears as an rw mount (CI run 45341414519).
# The set is EXACT: any additional or missing writable mount still fails closed. Residual risk (carried into
# P3.7): this runtime check matches mount-point PATHS only, not filesystem types or device numbers; types and
# device identities are verified by the real-container validation suite, not at run time.
STORAGE_WRITABLE_MOUNTS = ("/out", "/tmp")
ROOTLESS_DEV_WRITABLE_MOUNTS = ("/dev/full", "/dev/mqueue", "/dev/null", "/dev/pts", "/dev/random", "/dev/tty",
                                "/dev/urandom", "/dev/zero")


def expected_attestation(limits: Limits = DEFAULT_LIMITS) -> dict:
    """The isolation every run must report (design §8.6)."""
    return {
        "uid": CONTAINER_UID, "gid": CONTAINER_GID,
        "cap_eff": "0" * 16, "cap_prm": "0" * 16, "cap_bnd": "0" * 16,
        "no_new_privs": 1, "seccomp": 2,
        "net_interfaces": ["lo"], "dev_writable": False,
        "writable_mounts": sorted(STORAGE_WRITABLE_MOUNTS + ROOTLESS_DEV_WRITABLE_MOUNTS),
        "memory_max": limits.memory_bytes, "memory_swap_max": 0, "pids_max": limits.pids,
        "cpu_max": f"{int(round(limits.cpus * CPU_PERIOD_US))} {CPU_PERIOD_US}",
    }


def verify_attestation(actual: dict, expected: dict) -> None:
    validate_attestation_shape(actual)
    differing = sorted(k for k in ATTESTATION_KEYS if actual.get(k) != expected.get(k))
    if differing:
        raise SandboxUnavailableError(
            "sandbox attestation does not match the required isolation: " + ", ".join(differing))


# ── display of untrusted text ────────────────────────────────────────────────

_DISPLAY_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def display_text(data: bytes) -> str:
    """
    Untrusted output for display: UTF-8 with replacement, NUL/C0 controls
    removed (except newline and tab), CHART_SAVED marker lines dropped. A
    marker is never interpreted as a path.
    """
    text = _DISPLAY_CONTROL.sub("", bytes(data).decode("utf-8", errors="replace"))
    return "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(_MARKER))
