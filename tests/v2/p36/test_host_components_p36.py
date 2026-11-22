"""P3.6-1: pure host components. No process, no container: argv, host rules, staging, mapping, charts."""

import io
import os
import sys

import pytest

from tests.v2.golden.reference import DATA_DIR, ROOT
from v2.execution import podman_argv as pa
from v2.execution.errors import ArtifactRejectedError, DatasetNotAuthorizedError, SandboxProtocolError
from v2.execution.limits import DEFAULT_LIMITS
from v2.execution.results import ExecutionOutcome
from v2.execution.runtime import DatasetIdentity, PreludeId

RUN = "a" * 32
INST = "b" * 32
IMG = "sha256:" + "c" * 64
PODMAN = "/usr/bin/podman"
INPUTS = f"/tmp/insightpilot-v2-1000/instances/{INST}/runs/{RUN}/in"


def _argv(**kw):
    args = dict(instance_id=INST, run_id=RUN, inputs_dir=INPUTS, prelude=PreludeId.V1)
    args.update(kw)
    return pa.run_argv(kw.pop("podman", PODMAN), kw.pop("image", IMG), **{k: v for k, v in args.items()
                                                                          if k not in ("podman", "image")})


# ── argv ─────────────────────────────────────────────────────────────────────

EXPECTED_RUN_ARGV = [
    PODMAN, "run", "--rm", "--name", f"ip-{INST[:8]}-{RUN[:12]}",
    "--label", f"org.insightpilot.instance={INST}", "--label", f"org.insightpilot.run={RUN}",
    "--label", "org.insightpilot.schema=1", "--pull=never", "--image-volume=ignore",
    "--read-only", "--read-only-tmpfs=false",
    "--tmpfs", "/tmp:rw,size=67108864,nr_inodes=4096,mode=1777,noexec,nosuid,nodev",
    "--tmpfs", "/out:rw,size=33554432,nr_inodes=64,mode=1777,noexec,nosuid,nodev",
    "--mount", f"type=bind,source={INPUTS},destination=/in,readonly=true",
    "--network=none", "--dns=none", "--no-hosts", "--http-proxy=false",
    "--user", "10001:10001", "--passwd=false", "--cap-drop=ALL", "--security-opt=no-new-privileges",
    "--ipc=none", "--cgroupns=private", "--log-driver=passthrough", "--env-host=false",
    "--memory=1073741824", "--memory-swap=1073741824", "--cpus=1", "--pids-limit=64",
    "--ulimit=nofile=256:256", "--ulimit=core=0", "--timeout=45",
    "--env", "LANG=C.UTF-8", "--env", "TZ=UTC", "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1",
    "--env", "MKL_NUM_THREADS=1",
    IMG, "--mode", "run", "--run-id", RUN, "--prelude", "v1", "--wall", "30",
    "--stdout-cap", "1048576", "--stderr-head", "131072", "--stderr-tail", "131072",
    "--chart-cap", "10485760", "--max-side", "8192", "--max-pixels", "16777216",
    "--memory-max", "1073741824", "--pids-max", "64",
]


def test_the_run_argv_is_exactly_the_reviewed_command():
    assert _argv() == EXPECTED_RUN_ARGV


def test_every_size_comes_from_the_limits_contract():
    from v2.execution.limits import Limits
    lim = Limits(memory_bytes=2 * 1024 ** 3, pids=32, cpus=0.5, tmp_bytes=8 * 1024 ** 2, out_bytes=4 * 1024 ** 2,
                 chart_bytes=2 * 1024 ** 2)
    argv = _argv(limits=lim)
    assert "--memory=2147483648" in argv and "--memory-swap=2147483648" in argv
    assert "--pids-limit=32" in argv and "--cpus=0.5" in argv
    assert any(a.startswith("/tmp:rw,size=8388608,") for a in argv)
    assert argv[argv.index("--pids-max") + 1] == "32"


def test_lazy_seaborn_prelude_and_self_test_modes_are_enum_values():
    lazy = _argv(prelude=PreludeId.LAZY_SEABORN)
    assert lazy[lazy.index("--prelude") + 1] == "lazy_seaborn"
    assert [a for a in lazy if a != "lazy_seaborn"] == [a for a in _argv() if a != "v1"]
    for mode in pa.SUPERVISOR_MODES:
        a = _argv(mode=mode)
        assert a[a.index("--mode") + 1] == mode
    with pytest.raises(pa.ArgvError):
        _argv(mode="shell")
    with pytest.raises(pa.ArgvError):
        _argv(prelude="v1")


@pytest.mark.parametrize("field,value", [
    ("podman", "podman"), ("podman", "/usr/bin/../bin/podman"), ("podman", "/usr/bin/pod man"),
    ("image", "localhost/insightpilot-sandbox:latest"), ("image", "sha256:" + "C" * 64), ("image", IMG + " "),
    ("instance_id", "b" * 31), ("instance_id", "B" * 32), ("run_id", "a" * 31), ("run_id", "x" * 32),
    ("inputs_dir", "relative/runs/" + RUN + "/in"), ("inputs_dir", INPUTS.replace("/in", "/in/")),
    ("inputs_dir", f"/tmp/x,destination=/etc/runs/{RUN}/in"), ("inputs_dir", f"/tmp/../etc/runs/{RUN}/in"),
    ("inputs_dir", f"/tmp//x/runs/{RUN}/in"), ("inputs_dir", f"/tmp/x/runs/{'f' * 32}/in"),
    ("inputs_dir", f"/tmp/$(id)/runs/{RUN}/in"), ("inputs_dir", f"/tmp/x y/runs/{RUN}/in"),
])
def test_unsafe_values_are_refused_before_any_argv_exists(field, value):
    with pytest.raises(pa.ArgvError):
        _argv(**{field: value})


def test_no_caller_text_reaches_the_argv():
    argv = _argv()
    joined = "\n".join(argv)
    for forbidden in ("task", "sales_data", DATA_DIR, "GOOGLE_API_KEY", "HOME=", "PATH=", "--privileged",
                      "--cap-add", "--network=host", "--userns=keep-id", "seccomp=unconfined", "--device",
                      "--volume"):
        assert forbidden not in joined, forbidden
    assert "-v" not in argv and "--mount" in argv and argv.count("--mount") == 1


@pytest.mark.parametrize("bad", ["a b", "a;b", "a\nb", "a\x00b", "$(x)", "`x`", "a|b", "a>b", "", "é"])
def test_validate_argv_rejects_shell_and_control_characters(bad):
    with pytest.raises(pa.ArgvError):
        pa.validate_argv([PODMAN, bad])


def test_auxiliary_commands_use_the_exact_container_name():
    name = pa.container_name(INST, RUN)
    assert pa.kill_argv(PODMAN, name) == [PODMAN, "kill", "--signal", "KILL", name]
    assert pa.rm_argv(PODMAN, name) == [PODMAN, "rm", "--force", "--time", "0", name]
    assert pa.exists_argv(PODMAN, name) == [PODMAN, "container", "exists", name]
    assert pa.image_inspect_argv(PODMAN, IMG) == [PODMAN, "image", "inspect", IMG]
    for bad in ("ip-*", "--all", "ip-bbbbbbbb-aaaaaaaaaaa", f"{name};rm"):
        with pytest.raises(pa.ArgvError):
            pa.rm_argv(PODMAN, bad)


# ── host rules: one implementation ───────────────────────────────────────────

def test_the_dev_driver_and_production_share_one_host_rule_set():
    from v2.execution import host_checks
    sys.path.insert(0, os.path.join(ROOT, "scripts", "p3"))
    try:
        import rootless
    finally:
        sys.path.pop(0)
    assert rootless.evaluate_host is host_checks.evaluate_host
    assert rootless.MIN_PODMAN == host_checks.MIN_PODMAN and rootless.OCI_ARCH is host_checks.OCI_ARCH
    with open(os.path.join(ROOT, "scripts", "p3", "rootless.py"), encoding="utf-8") as f:
        assert "def evaluate_host" not in f.read()


# ── dataset staging by identity ──────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path):
    from v2.execution.lease import InstanceLease
    from v2.execution.workspace import JobWorkspace
    root = tmp_path / "root"
    lease = InstanceLease.acquire(root=str(root))
    ws = JobWorkspace.create(lease)
    yield ws
    ws.cleanup()
    lease.release()


def _identity(name="sales_data.csv"):
    from v2.execution.datasets import DEMO_DATASETS
    return DatasetIdentity(name, DEMO_DATASETS[name], os.path.getsize(os.path.join(DATA_DIR, name)))


def test_identity_staging_copies_by_name_with_three_way_digest_agreement(workspace):
    from v2.execution.datasets import DatasetAuthorizer
    staged = DatasetAuthorizer().authorize_identity_into(_identity(), workspace)
    copy = os.path.join(workspace.input_dir, "dataset.csv")
    assert (staged.name, staged.sha256, staged.size) == (_identity().name, _identity().sha256, _identity().size)
    assert open(copy, "rb").read() == open(os.path.join(DATA_DIR, "sales_data.csv"), "rb").read()


@pytest.mark.parametrize("make", [
    lambda i: DatasetIdentity("other_data.csv", i.sha256, i.size),        # not approved
    lambda i: DatasetIdentity(i.name, "0" * 64, i.size),                    # identity digest != pin
    lambda i: DatasetIdentity(i.name, i.sha256, i.size + 1),                # size mismatch
])
def test_identity_staging_refuses_and_leaves_no_copy(workspace, make):
    from v2.execution.datasets import DatasetAuthorizer
    with pytest.raises(DatasetNotAuthorizedError):
        DatasetAuthorizer().authorize_identity_into(make(_identity()), workspace)
    assert not os.path.exists(os.path.join(workspace.input_dir, "dataset.csv"))


def test_identity_staging_refuses_a_copy_whose_bytes_differ_from_the_pin(workspace, tmp_path):
    from v2.execution.datasets import DatasetAuthorizer
    data = tmp_path / "data"
    data.mkdir(mode=0o755)
    (data / "sales_data.csv").write_bytes(b"region,amount\nNorth,1\n")
    with pytest.raises(DatasetNotAuthorizedError, match="approved SHA-256"):
        DatasetAuthorizer(data_dir=str(data)).authorize_identity_into(_identity(), workspace)
    assert not os.path.exists(os.path.join(workspace.input_dir, "dataset.csv"))
    with pytest.raises(TypeError):
        DatasetAuthorizer().authorize_identity_into({"name": "sales_data.csv"}, workspace)


# ── frame -> result mapping ──────────────────────────────────────────────────

def _frame(**kw):
    from v2.execution.frame import DecodedFrame
    base = dict(run_id=RUN, child_exit=0, child_signal=None, timed_out=False, oom_killed=False, attestation={},
                stdout=b"CLAIM row_count=293\nCHART_SAVED:/etc/passwd\n\x00\x07ok", stderr=b"",
                stdout_truncated=False, stderr_truncated=False, chart=None, chart_reject_reason=None,
                out_other_entries=0)
    base.update(kw)
    return DecodedFrame(**base)


@pytest.mark.parametrize("kw,outcome", [
    ({}, ExecutionOutcome.SUCCEEDED),
    ({"child_exit": 1}, ExecutionOutcome.CODE_ERROR),
    ({"child_exit": None, "child_signal": 11}, ExecutionOutcome.CODE_ERROR),
    ({"child_exit": None, "child_signal": 9, "timed_out": True}, ExecutionOutcome.TIMED_OUT),
    ({"child_exit": None, "child_signal": 9, "oom_killed": True}, ExecutionOutcome.MEMORY_LIMIT),
])
def test_every_frame_status_maps_to_exactly_one_outcome(kw, outcome):
    from v2.execution.result_mapping import result_from_frame
    r = result_from_frame(_frame(**kw), run_id=RUN, duration_ms=12, chart_path="/x.png")
    assert r.outcome is outcome and r.isolation.value == "rootless_container"
    assert "CHART_SAVED" not in r.stdout and "\x00" not in r.stdout and "\x07" not in r.stdout
    assert (r.chart_path is None) == (outcome is not ExecutionOutcome.SUCCEEDED)


def test_a_frame_for_another_run_is_a_protocol_error():
    from v2.execution.result_mapping import result_from_frame
    with pytest.raises(SandboxProtocolError):
        result_from_frame(_frame(run_id="d" * 32), run_id=RUN, duration_ms=0)


def _png(size=(6, 4), mode="RGB"):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, (10, 20, 30) if mode == "RGB" else (10, 20, 30, 255)).save(buf, "PNG")
    return buf.getvalue()


def test_chart_bytes_are_validated_reencoded_and_published_without_overwrite(tmp_path):
    from v2.execution.chart_store import ChartStore, chart_filename
    from v2.execution.result_mapping import publish_chart
    charts = tmp_path / "charts"
    charts.mkdir(mode=0o755)
    store = ChartStore(str(charts))
    path, reason = publish_chart(_frame(chart=_png()), store, "stu_t1_a1", RUN)
    assert reason is None and path == os.path.join(str(charts), chart_filename("stu_t1_a1", RUN))
    assert open(path, "rb").read().startswith(b"\x89PNG")
    from v2.execution.errors import SandboxUnavailableError
    before = open(path, "rb").read()
    with pytest.raises(SandboxUnavailableError, match="not overwritten"):     # a collision fails closed
        publish_chart(_frame(chart=_png((8, 8))), store, "stu_t1_a1", RUN)
    assert open(path, "rb").read() == before
    for bad, code in ((b"not a png", "bad_signature"), (_png()[:30], None), (_png() + b"trailing", "trailing_data")):
        p, r = publish_chart(_frame(chart=bad), store, "t", "e" * 32)
        assert p is None and r in {"bad_signature", "truncated", "trailing_data", "decompression_mismatch",
                                   "bad_crc"} and (code is None or r == code)
    p, r = publish_chart(_frame(chart_reject_reason="symlink"), store, "t", "f" * 32)
    assert (p, r) == (None, "symlink")


# ── dispatcher: charts only at the store-published name ──────────────────────

class _StoreRuntime:
    name, is_test_double = "store-double", True

    def __init__(self, chart_dir, make_path):
        from v2.execution.isolation import IsolationLevel
        self.isolation = IsolationLevel.ROOTLESS_CONTAINER
        self.chart_directory, self.make_path = chart_dir, make_path

    def preflight(self):
        pass

    def execute(self, request):
        from v2.execution.isolation import IsolationLevel
        from v2.execution.results import ExecutionResult
        return ExecutionResult(run_id=request.run_id, isolation=IsolationLevel.ROOTLESS_CONTAINER,
                               outcome=ExecutionOutcome.SUCCEEDED, stdout="ok", stderr="",
                               chart_path=self.make_path(request))


def _dispatch(tmp_path, make_path):
    from v2.execution.dispatch import HardenedDispatcher
    charts = tmp_path / "charts"
    charts.mkdir(mode=0o755, exist_ok=True)
    d = HardenedDispatcher(_StoreRuntime(str(charts), lambda r: make_path(str(charts), r)))
    return d("print(1)", os.path.join(DATA_DIR, "sales_data.csv"), "stu_t1_a1")


def test_dispatcher_accepts_only_the_store_published_chart(tmp_path):
    from v2.execution.chart_store import chart_filename

    def published(d, r):
        p = os.path.join(d, chart_filename(r.task_id, r.run_id))
        open(p, "wb").write(_png())
        return p
    ok, out, chart, err = _dispatch(tmp_path, published)
    assert ok is True and chart.startswith(str(tmp_path / "charts")) and chart.endswith(".png")

    def other_name(d, r):
        p = os.path.join(d, "stu_t1_a1.png")
        open(p, "wb").write(_png())
        return p

    def symlinked(d, r):
        p = os.path.join(d, chart_filename(r.task_id, r.run_id))
        os.symlink("/etc/hostname", p)
        return p
    for maker in (other_name, symlinked, lambda d, r: "/tmp/x.png",
                  lambda d, r: os.path.join(d, chart_filename(r.task_id, r.run_id))):   # missing file
        with pytest.raises(SandboxProtocolError):
            _dispatch(tmp_path, maker)
