"""
P3.6-4B D3 (approved): nr_inodes removed from the /tmp and /out tmpfs options because Podman 4.9.3
rejects it (CI run 45125846903: 'Error: unknown mount option "nr_inodes=4096": invalid mount option').

NOT equivalent protection: the explicit inode-count limits (Limits.tmp_inodes / out_inodes) are no
longer enforced by the launcher. Byte sizes, mode=1777, noexec, nosuid, nodev and the 1 GiB memory
limit are unchanged; inode memory is bounded only indirectly by the memory cgroup. frame.py still
refuses a frame reporting more than Limits.out_inodes other /out entries (fail closed, unchanged).
"""

import os

import pytest

from tests.v2.p36.conftest import make_runtime
from v2.execution import podman_argv as pa
from v2.execution.errors import SandboxProtocolError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS

INST, RUN = "a" * 32, "b" * 32
IMG = "sha256:" + "c" * 64
INPUTS = f"/var/lib/ip/runs/{RUN}/in"


def _tmpfs(argv):
    return {argv[i + 1].split(":", 1)[0]: argv[i + 1].split(":", 1)[1].split(",")
            for i, a in enumerate(argv[:-1]) if a == "--tmpfs"}


@pytest.mark.parametrize("mode", sorted(pa.SUPERVISOR_MODES))
def test_tmpfs_keeps_byte_sizes_and_hardening_options_but_no_inode_option(mode):
    argv = pa.run_argv("/usr/bin/podman", IMG, instance_id=INST, run_id=RUN, inputs_dir=INPUTS, mode=mode)
    t = _tmpfs(argv)
    assert set(t) == {"/tmp", "/out"}
    assert t["/tmp"] == ["rw", f"size={DEFAULT_LIMITS.tmp_bytes}", "mode=1777", "noexec", "nosuid", "nodev"]
    assert t["/out"] == ["rw", f"size={DEFAULT_LIMITS.out_bytes}", "mode=1777", "noexec", "nosuid", "nodev"]
    assert not any(o.startswith("nr_inodes") for opts in t.values() for o in opts)
    assert f"--memory={DEFAULT_LIMITS.memory_bytes}" in argv and DEFAULT_LIMITS.memory_bytes == 1024 ** 3
    assert "--read-only" in argv and "--read-only-tmpfs=false" in argv


def test_the_grammar_refuses_an_inode_option_podman_4_9_3_cannot_parse():
    argv = pa.run_argv("/usr/bin/podman", IMG, instance_id=INST, run_id=RUN, inputs_dir=INPUTS)
    i = argv.index("--tmpfs") + 1
    bad = argv[:i] + [argv[i] + ",nr_inodes=4096"] + argv[i + 1:]
    with pytest.raises(pa.ArgvError, match="nr_inodes"):
        pa.validate_argv(bad)


def test_a_reintroduced_inode_option_fails_closed_exactly_as_podman_4_9_3_did(runtime_parts, monkeypatch):
    real = pa.run_argv

    def with_inodes(*a, **kw):                                # bypasses the grammar, as a regression would
        argv = real(*a, **kw)
        i = argv.index("--tmpfs") + 1
        return argv[:i] + [argv[i].replace(",mode=", ",nr_inodes=4096,mode=")] + argv[i + 1:]
    monkeypatch.setattr(pa, "run_argv", with_inodes)
    rt = make_runtime(runtime_parts)
    with pytest.raises((SandboxProtocolError, SandboxUnavailableError)):
        rt.preflight()
    assert rt._preflight_ok is False
    assert [c.get("refused") for c in runtime_parts.podman.calls() if c.get("refused")] == ["tmpfs_nr_inodes"]
    assert any('unknown mount option "nr_inodes=4096"' in d for d in rt.diagnostics)
    assert runtime_parts.podman.containers() == [] and os.listdir(runtime_parts.lease.runs_path) == []


def test_the_out_entry_bound_is_still_enforced_when_the_frame_is_decoded():
    """Without nr_inodes, more /out entries are possible; the decoder still refuses such a frame (fail closed)."""
    import inspect
    from v2.execution import frame
    src = inspect.getsource(frame)
    assert '_int(header["out_other_entries"], "out_other_entries", 0, limits.out_inodes)' in src
