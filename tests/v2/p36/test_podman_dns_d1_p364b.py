"""
P3.6-4B D1 (approved): --dns=none removed from the run argv because Podman 4.9.3 refuses it with
--network=none (CI run 45122041350: "Error: conflicting options: dns and the network mode: none").

Network isolation is unchanged: --network=none, --no-hosts and --http-proxy=false stay required.
The fake podman (fake_podman_main.py) reproduces the 4.9.3 refusal, so a regression that re-adds any
--dns flag fails offline exactly as it failed on the real runtime: closed, with nothing left behind.
"""

import pytest

from tests.v2.p36.conftest import make_runtime
from v2.execution import podman_argv as pa
from v2.execution.errors import SandboxProtocolError, SandboxUnavailableError

INST, RUN = "a" * 32, "b" * 32
IMG = "sha256:" + "c" * 64
INPUTS = f"/var/lib/ip/runs/{RUN}/in"
NETWORK_ISOLATION = ("--network=none", "--no-hosts", "--http-proxy=false")


@pytest.mark.parametrize("mode", sorted(pa.SUPERVISOR_MODES))
def test_every_run_argv_keeps_network_isolation_and_has_no_dns_flag(mode):
    argv = pa.run_argv("/usr/bin/podman", IMG, instance_id=INST, run_id=RUN, inputs_dir=INPUTS, mode=mode)
    for flag in NETWORK_ISOLATION:
        assert argv.count(flag) == 1, flag
    assert [a for a in argv if a.startswith("--network")] == ["--network=none"]
    assert not [a for a in argv if a.split("=", 1)[0] in pa.DNS_FLAGS_INCOMPATIBLE_WITH_NETWORK_NONE]
    assert not [a for a in argv if a.startswith("--dns")]


@pytest.mark.parametrize("dns", ["--dns=none", "--dns=192.0.2.53", "--dns-option=ndots:1", "--dns-search=example.invalid"])
def test_the_grammar_refuses_any_dns_flag_together_with_network_none(dns):
    argv = pa.run_argv("/usr/bin/podman", IMG, instance_id=INST, run_id=RUN, inputs_dir=INPUTS)
    with pytest.raises(pa.ArgvError, match="cannot be combined with --network=none"):
        pa.validate_argv(argv + [dns])
    with pytest.raises(pa.ArgvError):
        pa.validate_argv(argv[:argv.index("--network=none")] + [dns] + argv[argv.index("--network=none"):])


def test_a_reintroduced_dns_flag_fails_closed_exactly_as_podman_4_9_3_did(runtime_parts, monkeypatch):
    real = pa.run_argv

    def with_dns(*a, **kw):                                   # bypasses the grammar, as a regression would
        argv = real(*a, **kw)
        i = argv.index("--network=none")
        return argv[:i + 1] + ["--dns=none"] + argv[i + 1:]
    monkeypatch.setattr(pa, "run_argv", with_dns)
    rt = make_runtime(runtime_parts)
    with pytest.raises((SandboxProtocolError, SandboxUnavailableError)):
        rt.preflight()
    assert rt._preflight_ok is False
    refused = [c for c in runtime_parts.podman.calls() if c.get("refused") == "dns_with_network_none"]
    assert len(refused) == 1                                   # the first container start was refused; nothing ran
    assert any("conflicting options: dns and the network mode: none" in d for d in rt.diagnostics)
    assert runtime_parts.podman.containers() == [] and runtime_parts.lease.runs_path and \
        __import__("os").listdir(runtime_parts.lease.runs_path) == []


def test_without_the_dns_flag_the_same_runtime_passes_preflight(runtime_parts):
    rt = make_runtime(runtime_parts)
    rt.preflight()
    assert rt._preflight_ok is True
    runs = runtime_parts.podman.runs()
    assert runs and all("refused" not in r and r["missing_flags"] == [] for r in runs)
    assert all(not [a for a in r["argv"] if a.startswith("--dns")] for r in runs)
