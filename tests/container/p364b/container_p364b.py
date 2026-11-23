"""
P3.6-4B: functional validation of the REAL ContainerRuntime on rootless Podman (CI only).

The file name does not match pytest's test-file pattern, so `pytest tests/` never collects it (and
never reports it as skipped). The P3.6-4B workflow names it explicitly. It FAILS, never skips,
unless the validation environment is present:

    IP_TEST_REQUIRE_CONTAINER=1  IP_TEST_CONTAINER_IMAGE=sha256:<64 hex>  IP_TEST_EVIDENCE_DIR=<dir>
    IP_TEST_EVIDENCE_NAME=<phase>

Hardened mode is entered ONLY through bootstrap.install_for_validation() with the exact
ContainerRuntime class, which runs the real preflight. Code executed in containers is limited to:
- the hash-pinned golden fixtures (scripts/p3/golden_fixtures.json, tests/v2/golden/scenarios.py);
- the scripted P5 fixtures (tests/v2/p5/scripted.py) through evaluation.multi_agent;
- the hash-pinned functional fixtures in fixtures_p364b.py.
No LLM is called; no network is used by any fixture except one blocked connection attempt.

Evidence (observations, timings, results) is written to IP_TEST_EVIDENCE_DIR/runtime_<phase>.json.
It is FUNCTIONAL evidence: the CI runner account is root-equivalent, so nothing here is a security
claim (VE-2, P3.7).
"""

import hashlib
import io
import json
import os
import re
import resource
import shutil
import statistics
import subprocess
import time
import uuid

import pytest

from tests.container.p364b.fixtures_p364b import fixture
from tests.v2.golden import reference as ref

HARD = {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"}
OPTIONS = {"server.fileWatcherType": "none", "server.runOnSave": False}
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
EVIDENCE = {"phase": None, "observations": {}, "timings": {}, "results": {}}


def _env():
    missing = [k for k in ("IP_TEST_REQUIRE_CONTAINER", "IP_TEST_CONTAINER_IMAGE", "IP_TEST_EVIDENCE_DIR",
                           "IP_TEST_EVIDENCE_NAME") if not os.environ.get(k)]
    if missing or os.environ["IP_TEST_REQUIRE_CONTAINER"] != "1":
        pytest.fail(f"real-container validation needs {missing or 'IP_TEST_REQUIRE_CONTAINER=1'}; refusing (no skip)")
    if not _IMAGE.fullmatch(os.environ["IP_TEST_CONTAINER_IMAGE"]):
        pytest.fail("IP_TEST_CONTAINER_IMAGE must be sha256:<64 hex>")
    if not re.fullmatch(r"[a-z0-9_]{1,40}", os.environ["IP_TEST_EVIDENCE_NAME"]):
        pytest.fail("IP_TEST_EVIDENCE_NAME must be a short lowercase name")
    return os.environ["IP_TEST_CONTAINER_IMAGE"], os.environ["IP_TEST_EVIDENCE_DIR"], os.environ["IP_TEST_EVIDENCE_NAME"]


def _save():
    _, out, name = _env()
    EVIDENCE["phase"] = name
    with open(os.path.join(out, f"runtime_{name}.json"), "w", encoding="utf-8") as f:
        json.dump(EVIDENCE, f, indent=1, sort_keys=True, default=str)


def _stats(ms):
    ms = sorted(ms)
    if not ms:
        return None
    return {"n": len(ms), "median_ms": statistics.median(ms), "p95_ms": ms[max(0, -(-95 * len(ms) // 100) - 1)],
            "min_ms": ms[0], "max_ms": ms[-1]}


def _rusage():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"utime_s": round(r.ru_utime, 3), "stime_s": round(r.ru_stime, 3), "maxrss_kib": r.ru_maxrss}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def parts(tmp_path_factory):
    from types import SimpleNamespace
    from v2.execution.chart_store import ChartStore
    from v2.execution.datasets import DatasetAuthorizer
    from v2.execution.lease import InstanceLease
    _env()
    base = tmp_path_factory.mktemp("p364b")
    charts = base / "charts"
    charts.mkdir(mode=0o755)
    lease = InstanceLease.acquire(root=str(base / "ip-root"))
    podman = os.path.realpath(shutil.which("podman") or "/usr/bin/podman")
    p = SimpleNamespace(lease=lease, store=ChartStore(str(charts)), authorizer=DatasetAuthorizer(), charts=str(charts),
                        podman=podman, base=str(base))
    EVIDENCE["observations"]["instance_label"] = "org.insightpilot.instance"
    yield p
    lease.release()


@pytest.fixture(scope="module")
def rt(parts):
    from v2.execution.container_runtime import ContainerRuntime
    from v2.execution.limits import DEFAULT_LIMITS
    image, _, _ = _env()
    return ContainerRuntime(image_id=image, podman=parts.podman, lease=parts.lease, authorizer=parts.authorizer,
                            chart_store=parts.store, limits=DEFAULT_LIMITS)


@pytest.fixture(scope="module")
def hardened(rt):
    """HARDENED bootstrap with the real runtime: the ONLY entry is install_for_validation (real preflight)."""
    from v2.execution import binding, bootstrap
    bootstrap._reset_for_tests()
    binding._reset_tripwire_hits_for_tests()
    env = {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1"}
    t0 = time.perf_counter()
    try:
        bootstrap.install_for_validation(rt, env=env, options=OPTIONS)
    except Exception as exc:                                     # recorded, then the dependent tests fail
        EVIDENCE["results"]["preflight"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]}
        _save()
        raise
    EVIDENCE["timings"]["preflight_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    EVIDENCE["results"]["preflight"] = {"ok": True}
    _save()
    yield rt
    bootstrap._reset_for_tests()


def _identity(name):
    from v2.execution.datasets import DEMO_DATASETS
    from v2.execution.runtime import DatasetIdentity
    return DatasetIdentity(name, DEMO_DATASETS[name], os.path.getsize(os.path.join(ref.DATA_DIR, name)))


def _execute(rt, code, *, dataset="sales_data.csv", prelude=None, task_id="p364b"):
    from v2.execution.runtime import ExecutionRequest, PreludeId
    req = ExecutionRequest(run_id=uuid.uuid4().hex, task_id=task_id, code=code, dataset=_identity(dataset),
                           prelude=prelude or PreludeId.V1)
    t0 = time.perf_counter()
    result = rt.execute(req)
    wall = round((time.perf_counter() - t0) * 1000, 1)
    EVIDENCE["timings"].setdefault("executions", []).append({"task": task_id, "prelude": req.prelude.value,
                                                            "container_ms": result.duration_ms, "host_wall_ms": wall})
    return result


def _leftovers(parts, rt):
    r = subprocess.run([parts.podman, "ps", "-a", "--filter", f"label=org.insightpilot.instance={parts.lease.instance_id}",
                        "--format", "{{.Names}}"], capture_output=True, text=True, env=rt._env(), timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    return {"containers": r.stdout.split(), "workspaces": sorted(os.listdir(parts.lease.runs_path))}


def _assert_clean(parts, rt):
    left = _leftovers(parts, rt)
    assert left == {"containers": [], "workspaces": []}, left


# ── 1. host, image, attestation and self-tests (observed before any verification) ──

def test_01_host_and_image_preconditions(rt, parts):
    from v2.execution.host_checks import evaluate_host
    facts = rt._host_facts()
    problems = evaluate_host(facts)
    info = facts.get("podman_info") or {}
    host = info.get("host") or {}
    EVIDENCE["observations"]["host"] = {
        "podman_version": facts.get("podman_version"), "problems": problems,
        "rootless": (host.get("security") or {}).get("rootless"),
        "seccomp_enabled": (host.get("security") or {}).get("seccompEnabled"),
        "selinux": (host.get("security") or {}).get("selinuxEnabled"),
        "apparmor": (host.get("security") or {}).get("apparmorEnabled"),
        "cgroup_version": host.get("cgroupVersion"), "cgroup_manager": host.get("cgroupManager"),
        "oci_runtime": {k: (host.get("ociRuntime") or {}).get(k) for k in ("name", "version")},
        "kernel": host.get("kernel"), "arch": host.get("arch"), "os": host.get("os")}
    _save()
    assert problems == []
    rt._check_image()


def test_02_attestation_under_the_reviewed_run_flags(rt):
    from v2.execution.frame import expected_attestation
    t0 = time.perf_counter()
    decoded = rt._self_test("attest-only")
    EVIDENCE["timings"]["attest_only_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    expected = expected_attestation(rt.limits)
    EVIDENCE["observations"]["attestation"] = decoded.attestation
    EVIDENCE["observations"]["attestation_expected"] = expected
    EVIDENCE["observations"]["attestation_differences"] = {
        k: {"observed": decoded.attestation.get(k), "required": expected.get(k)}
        for k in sorted(set(expected) | set(decoded.attestation)) if decoded.attestation.get(k) != expected.get(k)}
    _save()
    assert EVIDENCE["observations"]["attestation_differences"] == {}


@pytest.mark.parametrize("mode", ["selftest-oom", "selftest-pids"])
def test_03_resource_limit_self_tests(rt, mode):
    t0 = time.perf_counter()
    d = rt._self_test(mode)
    EVIDENCE["timings"][f"{mode}_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    EVIDENCE["observations"][mode] = {"child_exit": d.child_exit, "child_signal": d.child_signal,
                                      "oom_killed": d.oom_killed, "timed_out": d.timed_out}
    _save()
    if mode == "selftest-oom":
        assert d.oom_killed is True
    else:
        assert d.child_exit == 4


def test_04_preflight_passes_and_hardened_mode_uses_the_real_runtime(hardened, rt):
    from multi_agent import execution as ex
    from v2.execution import bootstrap
    assert bootstrap.mode() is bootstrap.Mode.HARDENED and bootstrap._state.runtime is rt
    assert type(rt).__name__ == "ContainerRuntime" and rt.is_test_double is False
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES), ex.ContainerExecutionPort)


# ── 2. isolation observed from inside, dataset integrity, fail-closed paths, cleanup ──

def test_05_isolation_observed_from_inside_the_container(hardened, rt, parts):
    import sys
    sys.path.insert(0, os.path.join(ref.ROOT, "v2", "execution", "image", "runtime"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    r = _execute(rt, fixture("observe"), task_id="observe")
    assert r.outcome.value == "succeeded", (r.outcome, r.stderr[-800:])
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("P364B_OBSERVE ")]
    obs = json.loads(line[-1][len("P364B_OBSERVE "):])
    EVIDENCE["observations"]["inside"] = obs
    _save()
    from v2.execution.datasets import DEMO_DATASETS
    assert (obs["uid"], obs["gid"], obs["euid"]) == (10001, 10001, 10001)
    st = obs["status"]
    assert st["CapEff"] == st["CapPrm"] == st["CapBnd"] == st["CapInh"] == st["CapAmb"] == "0" * 16
    assert st["NoNewPrivs"] == "1" and st["Seccomp"] == "2"
    assert obs["env_names"] == sorted(supervisor.CHILD_ENV)
    assert obs["in_entries"] == ["code.py", "dataset.csv"] and obs["dataset_sha256"] == DEMO_DATASETS["sales_data.csv"]
    assert obs["net_interfaces"] == ["lo"]
    assert obs["connect_tcp_192_0_2_1_80"] != "connected"
    w = obs["writes"]
    assert w["/tmp/p364b"] == "writable" and w["/out/p364b"] == "writable"
    assert all(w[p] != "writable" for p in w if p not in ("/tmp/p364b", "/out/p364b")), w
    _assert_clean(parts, rt)


@pytest.mark.parametrize("name,outcome,reason", [
    ("code_error", "code_error", None),
    ("sleep_past_wall", "timed_out", None),
    ("allocate_2_gib", "memory_limit", None),
    ("chart_symlink", "succeeded", "symlink"),
    ("chart_not_png", "succeeded", "decode_failed"),
])
def test_06_fail_closed_outcomes_and_cleanup(hardened, rt, parts, name, outcome, reason):
    charts_before = sorted(os.listdir(parts.charts))
    r = _execute(rt, fixture(name), task_id=name)
    EVIDENCE["results"].setdefault("fail_closed", {})[name] = {
        "outcome": r.outcome.value, "chart_path": r.chart_path, "chart_rejected_reason": r.chart_rejected_reason,
        "stderr_tail": (r.stderr or "")[-300:]}
    _save()
    assert r.outcome.value == outcome
    assert r.chart_path is None and r.chart_rejected_reason == reason
    assert sorted(os.listdir(parts.charts)) == charts_before                  # nothing was published
    _assert_clean(parts, rt)


def test_07_a_dataset_identity_mismatch_is_refused_before_any_container(hardened, rt, parts):
    from v2.execution.errors import DatasetNotAuthorizedError
    from v2.execution.runtime import DatasetIdentity, ExecutionRequest
    before = len(rt.diagnostics)
    bad = DatasetIdentity("sales_data.csv", "0" * 64, _identity("sales_data.csv").size)
    with pytest.raises(DatasetNotAuthorizedError):
        rt.execute(ExecutionRequest(run_id=uuid.uuid4().hex, task_id="bad", code="print(1)\n", dataset=bad))
    assert len(rt.diagnostics) == before                       # no container was started
    EVIDENCE["results"]["dataset_mismatch_refused_before_container"] = True
    _save()
    _assert_clean(parts, rt)


def test_08_no_fallback_to_the_v1_subprocess_executor(hardened):
    import sandbox.executor as v1
    from v2.execution import binding
    with pytest.raises(binding.V1ExecutorReached):
        v1.execute_code("print(1)", os.path.join(ref.DATA_DIR, "sales_data.csv"), "fallback")
    assert binding.tripwire_hits() == 1 and binding.verify() == []
    binding._reset_tripwire_hits_for_tests()
    EVIDENCE["results"]["v1_executor_tripwire"] = "refused"
    _save()


# ── 3. golden fixtures: numbers (28) and hermetic charts (12), both preludes ──

def _golden_fixtures():
    from agent.rules_engine import scan_code_safety
    from tests.v2.golden import scenarios as sc
    with open(os.path.join(ref.ROOT, "scripts", "p3", "golden_fixtures.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    out = []
    for fx in manifest["fixtures"]:
        scen = sc.SCENARIOS[fx["dataset"]]
        task = next(t for t in scen["tasks"] if t["id"] == fx["task_id"])
        code = scen["code"][task["title"]][-1]
        assert hashlib.sha256(code.encode()).hexdigest() == fx["code_sha256"], fx["task_id"]
        assert not scan_code_safety(code), fx["task_id"]                  # the V1 scan accepts every fixture
        assert ref.sha256_file(os.path.join(ref.DATA_DIR, scen["file"])) == fx["dataset_sha256"]
        out.append((fx, scen["file"], code))
    return out


def _pixels(path):
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        return {"size": [im.size[0], im.size[1]], "mode": im.mode, "pixels_sha256": hashlib.sha256(im.tobytes()).hexdigest()}


@pytest.mark.parametrize("prelude", ["v1", "lazy_seaborn"])
def test_09_golden_numbers_and_hermetic_charts(hardened, rt, parts, prelude):
    from tests.v2.golden.compare import mismatches, parse_golden
    from v2.execution.runtime import PreludeId
    fixtures = _golden_fixtures()
    assert len(fixtures) == 12
    printed, charts = {}, {}
    for fx, data_file, code in fixtures:
        r = _execute(rt, code, dataset=data_file, prelude=PreludeId(prelude), task_id=fx["task_id"])
        got = _pixels(r.chart_path) if r.chart_path else None
        charts[fx["task_id"]] = {"outcome": r.outcome.value, "chart": got, "expected": fx["chart_hermetic"],
                                 "match": got == fx["chart_hermetic"], "rejected": r.chart_rejected_reason,
                                 "stderr_tail": "" if r.outcome.value == "succeeded" else (r.stderr or "")[-300:]}
        printed.setdefault(fx["dataset"], {}).update(parse_golden(r.stdout))
    answers = {}
    for ds, got in printed.items():
        exp = ref.load_expected(ds)["answers"]
        answers[ds] = {"checked": len(exp), "mismatches": mismatches(got, exp), "missing": sorted(set(exp) - set(got))}
    checked = sum(a["checked"] for a in answers.values())
    wrong = sum(len(a["mismatches"]) + len(a["missing"]) for a in answers.values())
    EVIDENCE["results"][f"golden_{prelude}"] = {
        "numbers": {"checked": checked, "correct": checked - wrong, "per_dataset": answers},
        "charts": {"matched": sum(c["match"] for c in charts.values()), "total": len(charts), "tasks": charts}}
    _save()
    assert checked == 28 and wrong == 0, answers
    assert all(c["outcome"] == "succeeded" for c in charts.values()), charts
    assert sum(c["match"] for c in charts.values()) == 12, {k: v for k, v in charts.items() if not v["match"]}
    _assert_clean(parts, rt)


def test_10_the_v1_graph_node_runs_through_the_same_runtime(hardened, rt, parts):
    import agent.nodes.executor as node
    fx, data_file, code = _golden_fixtures()[0]
    ok, output, chart, err = node.execute_code(code, os.path.join(ref.DATA_DIR, data_file), fx["task_id"])
    EVIDENCE["results"]["v1_executor_node"] = {"ok": ok, "chart_in_store": bool(chart) and chart.startswith(parts.charts),
                                              "chart": _pixels(chart) if chart else None}
    _save()
    assert ok is True and chart.startswith(parts.charts) and _pixels(chart) == fx["chart_hermetic"]
    _assert_clean(parts, rt)


# ── 4. the P5 20-case suite through the container port ─────────────────────────

@pytest.mark.parametrize("strategy", ["v1", "lazy-seaborn"])
def test_11_p5_suite(hardened, rt, parts, strategy):
    import evaluation.multi_agent as ma
    from v2.execution import binding
    ru0 = _rusage()
    t0 = time.perf_counter()
    suite = ma.run_suite(os.path.join(parts.base, f"p5-{strategy}"), strategy=strategy, mode="subprocess")
    s = suite["summary"]
    EVIDENCE["results"][f"p5_{strategy}"] = {
        "summary": s, "wall_s": round(time.perf_counter() - t0, 1), "rusage_children_before": ru0,
        "rusage_children_after": _rusage(),
        "cases": {r["case"]: {"status": r["status"], "expectations": r["expectations"],
                              "latency": {k: (r.get("latency") or {}).get(k) for k in
                                          ("pipeline_ms", "sandbox_executions", "executions", "blocked_by_safety_scan")}}
                  for r in suite["cases"]}}
    _save()
    assert s["cases"] == 20 and s["expectations_met"] == 20, [
        (r["case"], r["expectations"]["problems"]) for r in suite["cases"] if not r["expectations"]["met"]]
    assert s["clean_runs_golden_correct"] == s["clean_runs_golden_expected"] == 28
    assert s["incorrect_claims_accepted"] == 0 and s["provider_calls"] == 0
    assert binding.tripwire_hits() == 0                                   # no V1 executor was reached
    _assert_clean(parts, rt)


# ── 5. summary and final cleanup (always selected) ─────────────────────────────

def test_99_summary_and_no_leftovers(rt, parts):
    ex = EVIDENCE["timings"].get("executions", [])
    EVIDENCE["timings"]["execution_container_ms"] = _stats([e["container_ms"] for e in ex])
    EVIDENCE["timings"]["execution_host_wall_ms"] = _stats([e["host_wall_ms"] for e in ex])
    EVIDENCE["observations"]["rusage_children_end"] = _rusage()
    left = _leftovers(parts, rt)
    EVIDENCE["results"]["leftovers"] = left
    _save()
    assert left == {"containers": [], "workspaces": []}
