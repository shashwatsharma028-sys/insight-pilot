"""
P3.4: IPX1 result-frame protocol (strict host decoder).

Benign byte fixtures only; nothing is executed. Every malformed frame must
raise SandboxProtocolError; a well-formed but wrong attestation raises
SandboxUnavailableError (and is not a protocol error).
"""

import ast
import hashlib
import io
import json
import os
import struct

import pytest

from v2.execution import frame
from v2.execution.errors import SandboxProtocolError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
RID = "0123456789abcdef" * 2
OTHER_RID = "fedcba9876543210" * 2
ATT = frame.expected_attestation()
CAP_OUT = DEFAULT_LIMITS.stdout_bytes
CAP_ERR = DEFAULT_LIMITS.stderr_bytes


def make(**kw):
    args = dict(run_id=RID, child_exit=0, child_signal=None, timed_out=False, oom_killed=False,
                attestation=ATT, stdout=b"rows 293\n", stderr=b"", chart=None)
    args.update(kw)
    return frame.encode_frame(**args)


def header_of(data):
    (n,) = struct.unpack(">I", data[4:8])
    return json.loads(data[8:8 + n]), data[8 + n:]


def rebuild(header, payload, raw_header=None):
    raw = raw_header if raw_header is not None else json.dumps(header).encode()
    return frame.MAGIC + struct.pack(">I", len(raw)) + raw + payload


def decode(data, **kw):
    return frame.decode_frame_bytes(data, kw.pop("run_id", RID), **kw)


def protocol_error(data, match=None, **kw):
    with pytest.raises(SandboxProtocolError, match=match):
        decode(data, **kw)


class OneByteStream:
    """Returns at most one byte per read: the decoder must handle short reads."""

    def __init__(self, data):
        self._data, self._pos, self.reads = data, 0, 0

    def read(self, n):
        self.reads += 1
        chunk = self._data[self._pos:self._pos + min(n, 1)]
        self._pos += len(chunk)
        return chunk


class CountingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.total = 0

    def read(self, n=-1):
        assert n is not None and n >= 0, "the decoder must never read unbounded"
        chunk = super().read(n)
        self.total += len(chunk)
        return chunk


# ── valid frames ─────────────────────────────────────────────────────────────

def test_round_trip_preserves_every_field():
    chart = bytes(range(256)) * 10
    data = make(stdout=b"out\x00\xff", stderr=b"Traceback\nKeyError: 'x'\n", chart=chart, out_other_entries=2)
    f = decode(data, expected_attestation=ATT)
    assert (f.run_id, f.child_exit, f.child_signal, f.timed_out, f.oom_killed) == (RID, 0, None, False, False)
    assert f.stdout == b"out\x00\xff" and f.stderr.endswith(b"KeyError: 'x'\n")
    assert f.chart == chart and f.chart_reject_reason is None and f.out_other_entries == 2
    assert f.attestation == ATT and f.succeeded


def test_round_trip_through_one_byte_reads():
    data = make(stdout=b"x" * 5000, chart=b"c" * 3000)
    stream = OneByteStream(data)
    f = frame.decode_frame(stream, RID)
    assert f.stdout == b"x" * 5000 and f.chart == b"c" * 3000


@pytest.mark.parametrize("status", [
    dict(child_exit=1, child_signal=None, timed_out=False, oom_killed=False),
    dict(child_exit=None, child_signal=15, timed_out=False, oom_killed=False),
    dict(child_exit=None, child_signal=9, timed_out=True, oom_killed=False),
    dict(child_exit=None, child_signal=9, timed_out=False, oom_killed=True),
])
def test_failure_statuses_round_trip(status):
    f = decode(make(**status))
    assert (f.child_exit, f.child_signal, f.timed_out, f.oom_killed) == tuple(status.values())
    assert not f.succeeded and f.chart is None


def test_rejected_chart_reason_round_trips():
    assert decode(make(chart_reject_reason="symlink")).chart_reject_reason == "symlink"


# ── prefix and budget ────────────────────────────────────────────────────────

def test_wrong_magic():
    protocol_error(b"IPX2" + make()[4:], "wrong magic")
    protocol_error(b"", "truncated")


@pytest.mark.parametrize("length", [0, DEFAULT_LIMITS.frame_header_bytes + 1, 2 ** 32 - 1])
def test_header_length_is_checked_before_reading_the_header(length):
    class Guard:
        def __init__(self):
            self.data = frame.MAGIC + struct.pack(">I", length)

        def read(self, n):
            assert self.data, "decoder read past the prefix of an invalid header length"
            chunk, self.data = self.data[:n], self.data[n:]
            return chunk
    with pytest.raises(SandboxProtocolError, match="header length"):
        frame.decode_frame(Guard(), RID)


def test_declared_section_lengths_are_checked_before_any_payload_is_read():
    header, _ = header_of(make())
    header["sections"]["stdout"]["length"] = CAP_OUT + 1
    raw = json.dumps(header).encode()

    class Guard:
        def __init__(self):
            self.data = frame.MAGIC + struct.pack(">I", len(raw)) + raw

        def read(self, n):
            assert self.data, "decoder tried to read a payload after an invalid header"
            chunk, self.data = self.data[:n], self.data[n:]
            return chunk
    with pytest.raises(SandboxProtocolError, match="stdout.length"):
        frame.decode_frame(Guard(), RID)


def test_reads_are_bounded_even_with_endless_trailing_garbage():
    stream = CountingStream(make() + b"G" * (50 * 1024 * 1024))
    with pytest.raises(SandboxProtocolError, match="trailing bytes"):
        frame.decode_frame(stream, RID)
    assert stream.total <= len(make()) + 1


def test_oversized_whole_frame_is_refused_before_parsing():
    limits = Limits(stdout_bytes=1024, stderr_head_bytes=512, stderr_tail_bytes=512,
                    chart_bytes=1024, frame_header_bytes=4096)
    protocol_error(b"IPX1" + b"\x00" * (8 + limits.max_frame_bytes), "byte budget", limits=limits)


def test_stream_returning_more_than_requested_is_refused():
    class Liar:
        def __init__(self, data):
            self.data = data

        def read(self, n):
            return self.data             # ignores n
    with pytest.raises(SandboxProtocolError, match="more bytes than requested"):
        frame.decode_frame(Liar(make()), RID)


# ── header parsing ───────────────────────────────────────────────────────────

def _raw_header_frame(raw: bytes):
    return frame.MAGIC + struct.pack(">I", len(raw)) + raw


@pytest.mark.parametrize("raw,match", [
    (b"\xff\xfe{}", "UTF-8"),
    (b"\xef\xbb\xbf{}", "byte-order mark"),
    (b"{not json", "not valid JSON"),
    (b"[1, 2]", "must be a JSON object"),
    (b'{"schema": 1, "schema": 1}', "duplicate header key"),
    (b'{"schema": 1.0}', "floating-point"),
    (b'{"schema": NaN}', "non-finite"),
    (b'{"schema": Infinity}', "non-finite"),
    (b'{"schema": ' + b"9" * 5000 + b"}", "not valid JSON"),
    (b"[" * 100000 + b"]" * 100000, "not valid JSON|header length"),
])
def test_malformed_headers(raw, match):
    protocol_error(_raw_header_frame(raw), match)


def _mutated(path, value, delete=False):
    header, payload = header_of(make())
    obj = header
    for key in path[:-1]:
        obj = obj[key]
    if delete:
        del obj[path[-1]]
    else:
        obj[path[-1]] = value
    return rebuild(header, payload)


@pytest.mark.parametrize("path", [("schema",), ("status", "timed_out"), ("sections", "chart"),
                                  ("sections", "stdout", "sha256"), ("attestation", "uid")])
def test_missing_fields(path):
    protocol_error(_mutated(path, None, delete=True), "keys differ")


@pytest.mark.parametrize("path", [("extra",), ("status", "core_dumped"), ("sections", "logs"),
                                  ("sections", "stderr", "encoding"), ("attestation", "hostname")])
def test_unexpected_fields(path):
    protocol_error(_mutated(path, 1), "keys differ")


@pytest.mark.parametrize("path,value,match", [
    (("schema",), 2, "unknown schema"),
    (("schema",), True, "unknown schema"),
    (("schema",), "1", "unknown schema"),
    (("run_id",), "x" * 32, "run_id"),
    (("run_id",), OTHER_RID, "different run"),
    (("sections", "stdout", "length"), -1, "stdout.length"),
    (("sections", "stdout", "length"), "9", "stdout.length"),
    (("sections", "stdout", "length"), True, "stdout.length"),
    (("sections", "stdout", "length"), CAP_OUT + 1, "stdout.length"),
    (("sections", "stderr", "length"), CAP_ERR + 1, "stderr.length"),
    (("sections", "chart", "length"), DEFAULT_LIMITS.chart_bytes + 1, "chart.length"),
    (("sections", "stdout", "sha256"), "A" * 64, "sha256"),
    (("sections", "stdout", "truncated"), 0, "truncated"),
    (("out_other_entries",), DEFAULT_LIMITS.out_inodes + 1, "out_other_entries"),
    (("out_other_entries",), -1, "out_other_entries"),
])
def test_invalid_field_values(path, value, match):
    protocol_error(_mutated(path, value), match)


def test_fractional_length_is_refused():
    header, payload = header_of(make())
    raw = json.dumps(header).replace('"length": 9', '"length": 9.0', 1).encode()
    protocol_error(rebuild(None, payload, raw_header=raw), "floating-point")


# ── payload integrity ────────────────────────────────────────────────────────

@pytest.mark.parametrize("section", ["stdout", "stderr", "chart"])
def test_wrong_sha256_is_refused(section):
    data = make(stdout=b"abc", stderr=b"def", chart=b"ghi")
    header, payload = header_of(data)
    index = {"stdout": 0, "stderr": 3, "chart": 6}[section]
    tampered = bytearray(payload)
    tampered[index] ^= 0x01
    protocol_error(rebuild(header, bytes(tampered)), "SHA-256")


def test_wrong_declared_length_misaligns_and_is_refused():
    data = make(stdout=b"abcd", stderr=b"efgh")
    header, payload = header_of(data)
    header["sections"]["stdout"]["length"] = 3
    header["sections"]["stdout"]["sha256"] = hashlib.sha256(b"abc").hexdigest()
    protocol_error(rebuild(header, payload))            # stderr hash then fails, or trailing bytes


@pytest.mark.parametrize("cut", [2, 6, 20, -12, -1])
def test_truncated_frames_are_refused(cut):
    data = make(stdout=b"0123456789", chart=b"chart-bytes")
    protocol_error(data[:cut], "truncated")


def test_trailing_bytes_are_refused():
    protocol_error(make() + b"\x00", "trailing bytes")


# ── status and flag consistency ──────────────────────────────────────────────

@pytest.mark.parametrize("status,match", [
    (dict(child_exit=0, child_signal=9), "exactly one"),
    (dict(child_exit=None, child_signal=None), "exactly one"),
    (dict(child_exit=None, child_signal=15, timed_out=True), "SIGKILL"),
    (dict(child_exit=137, child_signal=None, oom_killed=True), "SIGKILL"),
    (dict(child_exit=None, child_signal=9, timed_out=True, oom_killed=True), "both timed out and OOM"),
    (dict(child_exit=256, child_signal=None), "child_exit"),
    (dict(child_exit=-1, child_signal=None), "child_exit"),
    (dict(child_exit=None, child_signal=0), "child_signal"),
    (dict(child_exit=None, child_signal=65), "child_signal"),
    (dict(child_exit=False, child_signal=None), "child_exit"),
])
def test_inconsistent_status_is_refused(status, match):
    header, payload = header_of(make())
    header["status"].update(status)
    protocol_error(rebuild(header, payload), match)


@pytest.mark.parametrize("kw,match", [
    (dict(child_exit=1, chart=b"png"), "only examined after a successful run"),
    (dict(child_exit=None, child_signal=9, timed_out=True, chart_reject_reason="symlink"),
     "only examined after a successful run"),
    (dict(chart=b"png", chart_reject_reason="symlink"), "both present and rejected"),
    (dict(chart_reject_reason="evil reason"), "reject_reason"),
])
def test_inconsistent_chart_metadata_is_refused_by_the_encoder(kw, match):
    with pytest.raises(SandboxProtocolError, match=match):
        make(**kw)


def test_inconsistent_chart_metadata_is_refused_by_the_decoder():
    header, payload = header_of(make())
    header["sections"]["chart"]["sha256"] = "0" * 64          # empty chart with a digest
    protocol_error(rebuild(header, payload), "sha256 null")
    header["sections"]["chart"]["sha256"] = None
    header["status"].update(child_exit=1)
    header["sections"]["chart"]["reject_reason"] = "too_large"
    protocol_error(rebuild(header, payload), "only examined after a successful run")


# ── truncation at exact boundaries ───────────────────────────────────────────

def test_stdout_exactly_at_cap_is_not_truncated():
    f = decode(make(stdout=b"a" * CAP_OUT))
    assert len(f.stdout) == CAP_OUT and not f.stdout_truncated


def test_truncated_stdout_must_be_exactly_the_cap():
    assert decode(make(stdout=b"a" * CAP_OUT, stdout_truncated=True)).stdout_truncated
    with pytest.raises(SandboxProtocolError, match="exactly its cap"):
        make(stdout=b"a" * (CAP_OUT - 1), stdout_truncated=True)
    with pytest.raises(SandboxProtocolError, match="stdout.length"):
        make(stdout=b"a" * (CAP_OUT + 1))


def test_truncated_stderr_must_be_exactly_head_plus_tail():
    assert decode(make(stderr=b"e" * CAP_ERR, stderr_truncated=True)).stderr_truncated
    with pytest.raises(SandboxProtocolError, match="exactly its cap"):
        make(stderr=b"e" * (CAP_ERR - 1), stderr_truncated=True)
    with pytest.raises(SandboxProtocolError, match="stderr.length"):
        make(stderr=b"e" * (CAP_ERR + 1))


# ── attestation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,value", [
    ("cap_eff", "0" * 15), ("cap_eff", "F" * 16), ("uid", True), ("uid", -1),
    ("no_new_privs", 2), ("seccomp", 3), ("net_interfaces", ["lo", "eth0"]),
    ("net_interfaces", ["lo", "lo"]), ("net_interfaces", "lo"), ("net_interfaces", ["x" * 16]),
    ("writable_mounts", ["tmp"]), ("writable_mounts", ["/tmp", "/out"]), ("dev_writable", 0),
    ("memory_max", "1g"), ("memory_max", 1.5), ("cpu_max", "100000"), ("cpu_max", "max max"),
])
def test_malformed_attestation_is_a_protocol_error(key, value):
    att = {**ATT, key: value}
    header, payload = header_of(make())
    header["attestation"] = att
    raw = json.dumps(header).encode()
    if isinstance(value, float):
        protocol_error(rebuild(None, payload, raw_header=raw), "floating-point")
    else:
        protocol_error(rebuild(header, payload), "attestation")


@pytest.mark.parametrize("key,value", [
    ("uid", 0), ("gid", 0), ("cap_eff", "0000003fffffffff"), ("cap_bnd", "0000000000000001"),
    ("no_new_privs", 0), ("seccomp", 0), ("net_interfaces", ["eth0", "lo"]),
    ("writable_mounts", ["/", "/out", "/tmp"]), ("dev_writable", True),
    ("memory_max", "max"), ("memory_swap_max", 1024), ("pids_max", "max"), ("cpu_max", "max 100000"),
])
def test_attestation_mismatch_means_isolation_is_unavailable(key, value):
    data = make(attestation={**ATT, key: value})
    with pytest.raises(SandboxUnavailableError, match=key) as info:
        decode(data, expected_attestation=ATT)
    assert type(info.value) is SandboxUnavailableError       # not a protocol error
    assert decode(data).attestation[key] == value             # well formed; only the policy differs


def test_expected_attestation_follows_the_limits():
    att = frame.expected_attestation(Limits(memory_bytes=2 * 1024 ** 3, pids=32, cpus=0.5))
    assert (att["memory_max"], att["pids_max"], att["cpu_max"]) == (2 * 1024 ** 3, 32, "50000 100000")


# ── display text and code-execution hygiene ──────────────────────────────────

def test_display_text_never_treats_markers_as_paths():
    out = frame.display_text(b"rows 293\nCHART_SAVED:/etc/passwd\n  CHART_SAVED:../x\n\x00\x1b[31m red\t!\n\xff end")
    assert "CHART_SAVED" not in out and "/etc/passwd" not in out and "../x" not in out
    assert out == "rows 293\n[31m red\t!\n� end"


def test_frame_module_never_evaluates_or_imports_untrusted_content():
    with open(os.path.join(ROOT, "v2", "execution", "frame.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    builtin_calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not {"eval", "exec", "compile", "__import__", "open", "getattr", "setattr"} & builtin_calls
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not {"system", "popen", "Popen", "fork", "kill", "loads_unsafe", "load"} & attrs
    modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
              {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert modules <= {"hashlib", "json", "re", "struct", "dataclasses", "typing", "io", "v2"}
