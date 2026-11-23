"""
P3.6-3: ContainerRuntime against a FAKE podman executable. No real container is started and no
generated code is executed (the fake drives the real supervisor with a literal-only FakeOS).
"""

import base64
import os
import subprocess
import time

import pytest

from tests.v2.golden.reference import DATA_DIR, DATASETS
from tests.v2.p36.conftest import GOOD_IMAGE, IMAGE_ID, TestContainerRuntime, make_runtime
from tests.v2.p36.fake_os import png
from v2.execution import podman_argv as pa
from v2.execution.errors import DatasetNotAuthorizedError, SandboxProtocolError, SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits
from v2.execution.results import ExecutionOutcome
from v2.execution.runtime import DatasetIdentity, ExecutionRequest, PreludeId

REQ_ID = "1234567890abcdef" * 2


def _identity(name="sales_data.csv"):
    from v2.execution.datasets import DEMO_DATASETS
    return DatasetIdentity(name, DEMO_DATASETS[name], os.path.getsize(os.path.join(DATA_DIR, name)))


def _request(code="print('CLAIM row_count=293')", task="sales_t1_a1", **kw):
    return ExecutionRequest(run_id=REQ_ID, task_id=task, code=code, dataset=kw.pop("dataset", _identity()), **kw)


def _ready(parts, **kw):
    rt = make_runtime(parts, **kw)
    rt.preflight()
    return rt


def _runs_left(parts):
    return os.listdir(parts.lease.runs_path)


# ── preflight ────────────────────────────────────────────────────────────────

def test_preflight_checks_host_image_and_three_trusted_self_tests(runtime_parts):
    rt = _ready(runtime_parts)
    calls = [c["argv"][:2] for c in runtime_parts.podman.calls()]
    assert calls[:3] == [["version", "--format"], ["info", "--format"], ["image", "inspect"]]
    assert [r["mode"] for r in runtime_parts.podman.runs()] == ["attest-only", "selftest-oom", "selftest-pids"]
    for r in runtime_parts.podman.runs():
        assert r["missing_flags"] == [] and r["inputs"] == []          # self-tests get an empty workspace
        pa.validate_argv([runtime_parts.podman.path] + r["argv"][1:])
    assert runtime_parts.podman.containers() == [] and _runs_left(runtime_parts) == []
    assert rt._preflight_ok is True


@pytest.mark.parametrize("scenario,match", [
    ({"image": None}, "not present"),
    ({"image": {**GOOD_IMAGE, "Config": {**GOOD_IMAGE["Config"],
                                         "Entrypoint": ["/usr/local/bin/python3", "-I", "-B",
                                                        "/opt/ip/inert_entrypoint.py"]}}}, "trusted supervisor"),
    ({"image": {**GOOD_IMAGE, "Config": {**GOOD_IMAGE["Config"], "Labels": {"io.insightpilot.stage": "P3.5a-inert"}}}},
     "not a P3.6 sandbox image"),
    ({"image": {**GOOD_IMAGE, "Config": {**GOOD_IMAGE["Config"], "Labels": {
        **GOOD_IMAGE["Config"]["Labels"], "io.insightpilot.runtime.sha256": "0" * 64}}}}, "runtime label"),
    ({"image": {**GOOD_IMAGE, "Config": {**GOOD_IMAGE["Config"], "Labels": {"io.insightpilot.stage": "P3.6"}}}},
     "runtime label"),
    ({"image": {**GOOD_IMAGE, "Config": {**GOOD_IMAGE["Config"], "User": "0:0"}}}, "image user"),
    ({"image": {**GOOD_IMAGE, "Config": {**GOOD_IMAGE["Config"], "Volumes": {"/data": {}}}}}, "volumes"),
    ({"image": {**GOOD_IMAGE, "Id": "e" * 64}}, "pinned id"),
    ({"modes": {"attest-only": {"net": ["lo", "eth0"]}}}, "attestation does not match"),
    ({"modes": {"attest-only": {"memory_max": "268435456\n"}}}, "attestation does not match"),
    ({"modes": {"selftest-oom": {"stdout": "", "exit_code": 3}}}, "memory limit was not enforced"),
    ({"modes": {"selftest-pids": {"stdout": "", "exit_code": 0}}}, "pids limit was not enforced"),
    ({"info": {"host": {"security": {"rootless": False}}}}, "not rootless"),
])
def test_preflight_refuses_and_stays_unusable(runtime_parts, scenario, match):
    runtime_parts.podman.scenario(**scenario)
    rt = make_runtime(runtime_parts)
    with pytest.raises(SandboxUnavailableError, match=match):
        rt.preflight()
    assert rt._preflight_ok is False and runtime_parts.podman.containers() == []
    with pytest.raises(SandboxUnavailableError, match="preflight"):
        rt.execute(_request())


def test_preflight_refuses_root_and_unsuitable_hosts(runtime_parts):
    for override in ({"uid": 0}, {"cgroup_fstype": "cgroup"}, {"remote_env": ["DOCKER_HOST"]}):
        rt = make_runtime(runtime_parts)
        rt.host_overrides = override
        with pytest.raises(SandboxUnavailableError, match="not suitable"):
            rt.preflight()


def test_the_real_class_refuses_this_root_test_container_by_itself(runtime_parts):
    from v2.execution.container_runtime import ContainerRuntime
    rt = make_runtime(runtime_parts, cls=ContainerRuntime)
    with pytest.raises(SandboxUnavailableError, match="not suitable"):
        rt.preflight()                                  # uid 0 and this host's cgroup setup are refused


def test_the_podman_binary_must_be_trustworthy(runtime_parts, tmp_path):
    bad = tmp_path / "podman"
    bad.write_text("#!/bin/sh\n")
    os.chmod(bad, 0o777)
    for path in (str(bad), str(tmp_path / "missing")):
        with pytest.raises(SandboxUnavailableError):
            TestContainerRuntime(image_id=IMAGE_ID, podman=path, lease=runtime_parts.lease,
                                 authorizer=runtime_parts.authorizer, chart_store=runtime_parts.store)
    with pytest.raises(pa.ArgvError):
        TestContainerRuntime(image_id="insightpilot:latest", podman=runtime_parts.podman.path,
                             lease=runtime_parts.lease, authorizer=runtime_parts.authorizer,
                             chart_store=runtime_parts.store)


# ── execution ────────────────────────────────────────────────────────────────

def test_execution_stages_inputs_by_identity_and_cleans_up_everything(runtime_parts):
    rt = _ready(runtime_parts)
    r = rt.execute(_request())
    assert r.outcome is ExecutionOutcome.SUCCEEDED and r.run_id == REQ_ID and r.stdout.strip() == "CLAIM row_count=293"
    assert r.isolation.value == "rootless_container" and r.chart_path is None
    run = runtime_parts.podman.runs("run")[-1]
    assert run["inputs"] == ["code.py", "dataset.csv"] and run["code"] == "print('CLAIM row_count=293')"
    assert run["dataset_sha256"] == _identity().sha256 and run["missing_flags"] == []
    assert run["argv"][run["argv"].index("--prelude") + 1] == "v1"
    assert DATA_DIR not in " ".join(run["argv"]) and "sales_t1" not in " ".join(run["argv"])
    name = run["argv"][run["argv"].index("--name") + 1]
    after = [c["argv"] for c in runtime_parts.podman.calls()][-2:]
    assert after == [["rm", "--force", "--time", "0", name], ["container", "exists", name]]
    assert runtime_parts.podman.containers() == [] and _runs_left(runtime_parts) == []


def test_the_lazy_seaborn_prelude_is_requested_by_enum(runtime_parts):
    rt = _ready(runtime_parts)
    rt.execute(_request(prelude=PreludeId.LAZY_SEABORN))
    argv = runtime_parts.podman.runs("run")[-1]["argv"]
    assert argv[argv.index("--prelude") + 1] == "lazy_seaborn"


def test_a_valid_chart_is_validated_reencoded_and_published(runtime_parts):
    from v2.execution.chart_store import chart_filename
    runtime_parts.podman.scenario(modes={"run": {"chart_b64": base64.b64encode(png((48, 32), metadata=True)).decode()}})
    rt = _ready(runtime_parts)
    r = rt.execute(_request())
    assert r.chart_path == os.path.join(runtime_parts.charts, chart_filename("sales_t1_a1", REQ_ID))
    assert open(r.chart_path, "rb").read().startswith(b"\x89PNG") and r.chart_rejected_reason is None


@pytest.mark.parametrize("chart,reason", [(b"\x89PNG\r\n\x1a\nbroken", "decode_failed"), (b"GIF89a", "decode_failed")])
def test_invalid_chart_bytes_are_rejected_with_a_reason_and_nothing_published(runtime_parts, chart, reason):
    runtime_parts.podman.scenario(modes={"run": {"chart_b64": base64.b64encode(chart).decode()}})
    r = _ready(runtime_parts).execute(_request())
    assert r.outcome is ExecutionOutcome.SUCCEEDED and r.chart_path is None and r.chart_rejected_reason == reason
    assert os.listdir(runtime_parts.charts) == []


@pytest.mark.parametrize("mode,outcome", [({"exit_code": 1, "stdout": ""}, ExecutionOutcome.CODE_ERROR),
                                          ({"hang": True}, ExecutionOutcome.TIMED_OUT),
                                          ({"oom": True, "stdout": ""}, ExecutionOutcome.MEMORY_LIMIT)])
def test_code_errors_timeouts_and_oom_are_ordinary_outcomes(runtime_parts, mode, outcome):
    runtime_parts.podman.scenario(modes={"run": mode})
    r = _ready(runtime_parts).execute(_request())
    assert r.outcome is outcome and r.chart_path is None
    assert runtime_parts.podman.containers() == [] and _runs_left(runtime_parts) == []


@pytest.mark.parametrize("behaviour", [
    {"raw_stdout_b64": base64.b64encode(b"").decode()},                                  # no frame at all
    {"raw_stdout_b64": base64.b64encode(b"IPX1\x00\x00\x00\x05{bad}").decode()},          # forged header
    {"raw_stdout_b64": base64.b64encode(b"hello, I am a frame").decode()},                 # wrong magic
    {"append_b64": base64.b64encode(b"trailing").decode()},                                # bytes after the frame
    {"exit": 3},                                                                           # nonzero after a frame
    {"survivors": [99]},                                                                   # supervisor: no frame
])
def test_malformed_or_missing_frames_fail_closed_and_still_clean_up(runtime_parts, behaviour):
    runtime_parts.podman.scenario(modes={"run": behaviour})
    rt = _ready(runtime_parts)
    with pytest.raises(SandboxUnavailableError):            # SandboxProtocolError is a subclass
        rt.execute(_request())
    assert runtime_parts.podman.containers() == [] and _runs_left(runtime_parts) == []


def test_a_weakened_container_at_run_time_is_refused(runtime_parts):
    runtime_parts.podman.scenario(modes={"run": {"net": ["lo", "eth0"]}})
    rt = _ready(runtime_parts)
    with pytest.raises(SandboxUnavailableError, match="attestation does not match"):
        rt.execute(_request())


def test_a_frame_for_another_run_is_refused(runtime_parts):
    runtime_parts.podman.scenario(modes={"run": {"run_id_override": "9" * 32}})
    rt = _ready(runtime_parts)
    with pytest.raises(SandboxProtocolError):
        rt.execute(_request())
    assert runtime_parts.podman.containers() == [] and _runs_left(runtime_parts) == []


def test_the_host_deadline_kills_and_removes_the_container(runtime_parts):
    lim = Limits(code_wall_seconds=1, host_grace_seconds=1)
    runtime_parts.podman.scenario(modes={"run": {"sleep": 30}})
    rt = make_runtime(runtime_parts, limits=lim)
    rt._preflight_ok = True                    # preflight is covered above; this test is about the deadline
    t0 = time.monotonic()
    with pytest.raises(SandboxUnavailableError, match="host deadline"):
        rt.execute(_request())
    assert time.monotonic() - t0 < 15
    kinds = [c["argv"][0] for c in runtime_parts.podman.calls()]
    assert "kill" in kinds and kinds[-2:] == ["rm", "container"]
    assert _runs_left(runtime_parts) == []


def test_a_container_that_survives_removal_is_an_error_after_cleanup_was_attempted(runtime_parts):
    rt = _ready(runtime_parts)
    runtime_parts.podman.scenario(persist=True)
    with pytest.raises(SandboxUnavailableError, match="still present"):
        rt.execute(_request())
    assert _runs_left(runtime_parts) == []


def test_cancellation_mid_run_still_removes_the_container_and_workspace(runtime_parts, monkeypatch):
    rt = _ready(runtime_parts)
    import v2.execution.container_runtime as cr

    def interrupted(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(cr, "decode_frame", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rt.execute(_request())
    kinds = [c["argv"][:2] for c in runtime_parts.podman.calls()][-2:]
    assert kinds == [["rm", "--force"], ["container", "exists"]]
    assert _runs_left(runtime_parts) == [] and runtime_parts.podman.containers() == []


@pytest.mark.parametrize("identity,match", [
    (DatasetIdentity("sales_data.csv", "0" * 64, 1), "approved SHA-256"),
    (DatasetIdentity("other_data.csv", "0" * 64, 1), "only the four bundled"),
])
def test_rejected_datasets_never_reach_a_container(runtime_parts, identity, match):
    rt = _ready(runtime_parts)
    n = len(runtime_parts.podman.runs())
    with pytest.raises(DatasetNotAuthorizedError, match=match):
        rt.execute(_request(dataset=identity))
    assert len(runtime_parts.podman.runs()) == n and _runs_left(runtime_parts) == []
    with pytest.raises(DatasetNotAuthorizedError, match="no dataset identity"):
        rt.execute(ExecutionRequest(run_id=REQ_ID, task_id="t", code="print('x')"))


def test_the_concurrency_limit_refuses_when_busy(runtime_parts):
    rt = _ready(runtime_parts, limits=Limits(max_concurrent=1, queue_wait_seconds=1))
    assert rt._sem.acquire(timeout=1)
    try:
        with pytest.raises(SandboxUnavailableError, match="busy"):
            rt.execute(_request())
    finally:
        rt._sem.release()


def test_only_the_resolved_podman_binary_is_ever_started(runtime_parts, monkeypatch):
    started = []
    real = subprocess.Popen

    def spy(argv, *a, **k):
        started.append(list(argv))
        assert k.get("start_new_session") is True and k.get("close_fds") is True
        assert set(k.get("env", {})) <= {"PATH", "HOME", "XDG_RUNTIME_DIR", "LANG"}
        return real(argv, *a, **k)
    rt = make_runtime(runtime_parts, popen=spy)
    rt.preflight()
    rt.execute(_request())
    assert started and {a[0] for a in started} == {runtime_parts.podman.path}


# ── the validation-only entry point and production ───────────────────────────

HARD = {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"}
OPTS = {"server.fileWatcherType": "none", "server.runOnSave": False}


def test_install_for_validation_refuses_root_doubles_subclasses_and_missing_opt_in(runtime_parts):
    from v2.execution import bootstrap
    from v2.execution.container_runtime import ContainerRuntime
    real = make_runtime(runtime_parts, cls=ContainerRuntime)
    double = make_runtime(runtime_parts)
    for runtime, env, needle in ((real, {**HARD}, "IP_TEST_REQUIRE_CONTAINER=1"),
                                 (real, {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1"}, "euid 0 refused"),
                                 (double, {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1"}, "only the real ContainerRuntime")):
        with pytest.raises(bootstrap.BootstrapRefused, match=needle):
            bootstrap.install_for_validation(runtime, env=env, options=OPTS)
        assert bootstrap.mode() is bootstrap.Mode.UNINITIALIZED
    assert runtime_parts.podman.runs() == []              # refused before any preflight container


def test_production_cannot_reach_hardened_mode_by_flags_env_or_import():
    from v2.execution import bootstrap
    import v2.execution.container_runtime  # noqa: F401   importing changes nothing
    assert bootstrap.mode() is bootstrap.Mode.UNINITIALIZED
    for env in ({**HARD}, {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1"},
                {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1", "IP_FF_ORCHESTRATOR": "v2"}):
        bootstrap._reset_for_tests()
        with pytest.raises(bootstrap.BootstrapRefused, match="no validated container backend"):
            bootstrap.ensure_ready(env=env, options=OPTS)
    bootstrap._reset_for_tests()


def test_install_for_validation_is_called_only_from_tests():
    import ast
    from tests.v2.golden.reference import ROOT
    callers = set()
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in (".git", "venv", "tests", "docs", "__pycache__")]
        for f in files:
            if f.endswith(".py"):
                tree = ast.parse(open(os.path.join(d, f), encoding="utf-8").read())
                if any(isinstance(n, ast.Attribute) and n.attr == "install_for_validation" or
                       isinstance(n, ast.Name) and n.id == "install_for_validation" for n in ast.walk(tree)):
                    callers.add(os.path.relpath(os.path.join(d, f), ROOT))
    assert callers == set()


# ── P5 and V1 through the real runtime class (fake podman) ───────────────────

@pytest.fixture
def hardened_container(runtime_parts, monkeypatch):
    from tests.v2.p3.conftest import HARDENED_OPTIONS
    from v2.execution import bootstrap
    for k, v in {**HARD, "IP_FF_ORCHESTRATOR": "v2"}.items():
        monkeypatch.setenv(k, v)
    from streamlit import config
    saved = {k: config.get_option(k) for k in HARDENED_OPTIONS}
    for k, v in HARDENED_OPTIONS.items():
        config.set_option(k, v)
    runtime_parts.podman.scenario(modes={"run": {"chart_b64": base64.b64encode(png()).decode()}})
    rt = make_runtime(runtime_parts)
    bootstrap.install_for_tests(rt)                     # runs the real preflight through the fake podman
    yield rt
    bootstrap._reset_for_tests()
    for k, v in saved.items():
        config.set_option(k, v)


@pytest.mark.parametrize("dataset", sorted(DATASETS))
def test_p5_runs_end_to_end_through_the_container_runtime(hardened_container, runtime_parts, dataset):
    from multi_agent import execution as ex
    from multi_agent.team import build_team
    from multi_agent.verification import values_match
    from tests.v2.p5 import scripted
    port = ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES)
    assert isinstance(port, ex.ContainerExecutionPort)
    sup, _ = build_team(scripted.ScriptedAgentLLM(dataset, mode="literal"), port, scripted.approve_all)
    out = sup.run(os.path.join(DATA_DIR, DATASETS[dataset]))
    f = out["final"]
    golden = scripted.golden_answers(dataset)
    assert f["run_complete"] is True and set(f["accepted_claims"]) == set(golden)
    assert all(values_match(f["accepted_claims"][k], golden[k]) for k in golden)
    charts = [m["payload"]["execution"]["chart_path"] for m in out["messages"] if m["kind"] == "analysis_result"
              and m["payload"]["execution"]["ok"]]
    assert charts and all(c.startswith(runtime_parts.charts) and os.path.isfile(c) for c in charts)
    assert runtime_parts.podman.containers() == [] and _runs_left(runtime_parts) == []
    from v2.execution import binding
    assert binding.tripwire_hits() == 0


def test_a_container_infrastructure_failure_terminates_the_p5_run(hardened_container, runtime_parts):
    from multi_agent import execution as ex
    from multi_agent.team import build_team
    from tests.v2.p5 import scripted
    runtime_parts.podman.scenario(modes={"run": {"raw_stdout_b64": base64.b64encode(b"garbage").decode()}})
    sup, _ = build_team(scripted.ScriptedAgentLLM("student", mode="literal"),
                        ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES), scripted.approve_all)
    out = sup.run(os.path.join(DATA_DIR, DATASETS["student"]))
    f = out["final"]
    assert f["status"] == "isolation_unavailable" and f["run_complete"] is False and f["accepted_claims"] == {}
    assert len(runtime_parts.podman.runs("run")) == 1                         # no retry, no fallback
    assert [e["error_class"] for e in out["events"] if e["type"] == "execution_terminated"] == ["SandboxProtocolError"]


def test_the_v1_graph_path_uses_the_same_runtime_through_executor_node(hardened_container, runtime_parts):
    import agent.nodes.executor as node
    ok, output, chart, err = node.execute_code("print('CLAIM row_count=293')", os.path.join(DATA_DIR, "sales_data.csv"),
                                               "sales_t1")
    assert ok is True and output == "CLAIM row_count=293" and chart.startswith(runtime_parts.charts)
    assert runtime_parts.podman.runs("run")[-1]["dataset_sha256"] == _identity().sha256
