"""
P3.6 fixtures. The P3 leak guard is autouse here too (no V2 binding or tripwire may leak).

fake_podman: a root-owned, non-writable launcher script that runs tests/v2/p36/fake_podman_main.py
with its own state directory. It is a trusted test double: it never starts a container and never
executes generated code.

TestContainerRuntime: the REAL ContainerRuntime code with two test-only changes, both confined to
this subclass: is_test_double = True (so only bootstrap.install_for_tests accepts it) and host
facts taken from the reviewed P3.5b GOOD_FACTS for the parts that read this machine (this test
container is uid 0 with cgroup v1, which the real preflight correctly refuses). The Podman
version and info still come through the runtime's real CLI path, from the fake.
"""

import copy
import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from tests.v2.golden.reference import ROOT
from tests.v2.p3.leak_guard import v2_global_state_guard  # noqa: F401  (autouse fixture)
from v2.execution.container_runtime import EXPECTED_ENTRYPOINT, ContainerRuntime

IMAGE_ID = "sha256:" + "d" * 64
MAP = [{"container_id": 0, "host_id": 1001, "size": 1}, {"container_id": 1, "host_id": 100000, "size": 65536}]
GOOD_INFO = {"host": {"arch": "amd64", "os": "linux", "cgroupVersion": "v2", "cgroupManager": "systemd",
                      "cgroupControllers": ["cpu", "io", "memory", "pids"], "serviceIsRemote": False,
                      "security": {"rootless": True, "seccompEnabled": True},
                      "idMappings": {"uidmap": MAP, "gidmap": MAP}}}
GOOD_IMAGE = {"Id": "d" * 64, "Config": {"User": "10001:10001", "Entrypoint": EXPECTED_ENTRYPOINT,
                                         "Labels": {"io.insightpilot.stage": "P3.6"}, "Volumes": None,
                                         "ExposedPorts": None}}
GOOD_MODES = {"selftest-oom": {"oom": True, "stdout": ""}, "selftest-pids": {"exit_code": 4, "stdout": ""}}


def _good_host_facts():
    from tests.v2.p3.test_rootless_p35b import GOOD_FACTS
    return copy.deepcopy(GOOD_FACTS)


class TestContainerRuntime(ContainerRuntime):
    __test__ = False
    is_test_double = True
    host_overrides = {}

    def _host_facts(self):
        real = super()._host_facts()                  # exercises the real CLI path (version, info)
        facts = _good_host_facts()
        facts["podman"] = self.podman
        facts["podman_version"], facts["podman_info"] = real["podman_version"], real["podman_info"]
        facts.update(self.host_overrides)
        return facts


class FakePodman:
    def __init__(self, base):
        self.state = os.path.join(base, "podman-state")
        os.makedirs(os.path.join(self.state, "containers"))
        bindir = os.path.join(base, "bin")
        os.makedirs(bindir)
        self.path = os.path.join(bindir, "podman")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"#!{sys.executable} -IB\n"
                    f"import sys\nsys.path.insert(0, {ROOT!r})\n"
                    f"from tests.v2.p36.fake_podman_main import main\n"
                    f"sys.exit(main(sys.argv[1:], {self.state!r}))\n")
        os.chmod(self.path, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        self.scenario(modes=GOOD_MODES)

    def scenario(self, **kw):
        sc = {"info": GOOD_INFO, "image": GOOD_IMAGE, "modes": dict(GOOD_MODES)}
        sc.update(kw)
        if "modes" in kw:
            sc["modes"] = {**GOOD_MODES, **kw["modes"]}
        with open(os.path.join(self.state, "scenario.json"), "w", encoding="utf-8") as f:
            json.dump(sc, f)

    def calls(self):
        path = os.path.join(self.state, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    def runs(self, mode=None):
        return [c for c in self.calls() if c["argv"][0] == "run" and (mode is None or c["mode"] == mode)]

    def containers(self):
        return os.listdir(os.path.join(self.state, "containers"))


@pytest.fixture
def fake_podman(tmp_path):
    return FakePodman(str(tmp_path / "fake"))


@pytest.fixture
def runtime_parts(tmp_path, fake_podman):
    from v2.execution.chart_store import ChartStore
    from v2.execution.datasets import DatasetAuthorizer
    from v2.execution.lease import InstanceLease
    charts = tmp_path / "charts"
    charts.mkdir(mode=0o755)
    lease = InstanceLease.acquire(root=str(tmp_path / "ip-root"))
    parts = SimpleNamespace(lease=lease, store=ChartStore(str(charts)), authorizer=DatasetAuthorizer(),
                            podman=fake_podman, charts=str(charts))
    yield parts
    lease.release()


def make_runtime(parts, limits=None, cls=TestContainerRuntime, **kw):
    from v2.execution.limits import DEFAULT_LIMITS
    return cls(image_id=IMAGE_ID, podman=parts.podman.path, lease=parts.lease, authorizer=parts.authorizer,
               chart_store=parts.store, limits=limits or DEFAULT_LIMITS, **kw)
