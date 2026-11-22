"""P3.6-2: trusted in-image runner, preludes and PID-1 supervisor, tested offline (nothing is executed)."""

import ast
import json
import os
import signal
import sys

import pytest

from tests.v2.golden import scenarios as sc
from tests.v2.golden.reference import ROOT
from tests.v2.p36.fake_os import FakeOS, png, supervisor_args
from v2.execution import frame
from v2.execution.errors import SandboxUnavailableError
from v2.execution.limits import DEFAULT_LIMITS, Limits

RUNTIME_DIR = os.path.join(ROOT, "v2", "execution", "image", "runtime")
sys.path.insert(0, RUNTIME_DIR)
import runner          # noqa: E402
import supervisor      # noqa: E402
sys.path.remove(RUNTIME_DIR)

RUN = "0123456789abcdef" * 2


# ── preludes and the runner ──────────────────────────────────────────────────

def _host_wrappers(code):
    import sandbox.executor as v1
    from sandbox_v2.lazy_prelude import wrap_code_lazy_seaborn
    saved = v1.CHARTS_DIR
    try:
        v1.CHARTS_DIR = "/out"
        return (v1.wrap_code_safely(code, "/in/dataset.csv", "chart"),
                wrap_code_lazy_seaborn(code, "/in/dataset.csv", "chart"))
    finally:
        v1.CHARTS_DIR = saved


def test_committed_preludes_equal_a_fresh_generation_from_the_host_functions():
    sys.path.insert(0, os.path.join(ROOT, "scripts", "p3"))
    try:
        import gen_preludes
    finally:
        sys.path.pop(0)
    fresh = json.dumps(gen_preludes.generate(), indent=1, sort_keys=True) + "\n"
    assert open(os.path.join(RUNTIME_DIR, "preludes.json"), encoding="utf-8").read() == fresh


def test_prelude_file_is_pinned_and_tampering_is_refused(tmp_path):
    good = runner.load_preludes()
    assert set(good) == {"v1", "lazy_seaborn"}
    data = json.load(open(runner.PRELUDES_FILE, encoding="utf-8"))
    data["preludes"]["v1"]["prelude"] = data["preludes"]["v1"]["prelude"].replace("plt.style.use", "import os;plt.style.use")
    bad = tmp_path / "preludes.json"
    bad.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(runner.RunnerError, match="SHA-256"):
        runner.load_preludes(str(bad))


def _all_fixture_codes():
    for name, scen in sorted(sc.SCENARIOS.items()):
        for title, attempts in scen["code"].items():
            for i, code in enumerate(attempts):
                yield f"{name}:{title}:{i}", code


ADVERSARIAL = {
    "empty": "",
    "whitespace_only_lines": "x = 1\n   \n\t\nprint(x)\n",
    "indented_block": "for i in range(2):\n    print(i)\n",
    "all_indented": "    print('indented')\n    print('more')\n",
    "tabs": "if True:\n\tprint('tab')\n",
    "marker_in_code": "print('# ── Agent-generated code starts here')\nimport seaborn as sns\n",
    "sentinel": "__INSIGHTPILOT_GENERATED_CODE_SENTINEL__\n",
    "unicode": "print('€ ✓ 漢字')\n",
    "no_trailing_newline": "print(1)",
    "crlf": "print(1)\r\nprint(2)\r\n",
    "chart_path_override": "CHART_PATH = '/etc/passwd'\n",
}


@pytest.mark.parametrize("case", sorted(ADVERSARIAL) + ["fixtures"])
def test_runner_composition_equals_the_v1_and_p41_wrappers_byte_for_byte(case):
    preludes = runner.load_preludes()
    codes = list(_all_fixture_codes()) if case == "fixtures" else [(case, ADVERSARIAL[case])]
    assert codes
    for label, code in codes:
        v1_text, lazy_text = _host_wrappers(code)
        assert runner.compose(code, "v1", preludes) == v1_text, label
        assert runner.compose(code, "lazy_seaborn", preludes) == lazy_text, label


def test_the_v1_prelude_keeps_the_eager_import_and_the_lazy_one_replaces_only_that_line():
    p = runner.load_preludes()
    v1, lazy = p["v1"]["prelude"], p["lazy_seaborn"]["prelude"]
    assert v1.count("import seaborn as sns\n") == 1 and 'r"/in/dataset.csv"' in v1 and 'r"/out/chart.png"' in v1
    from sandbox_v2.lazy_prelude import LAZY_SEABORN_BLOCK, V1_SEABORN_LINE
    assert lazy == v1.replace(V1_SEABORN_LINE, LAZY_SEABORN_BLOCK, 1)


@pytest.mark.parametrize("argv", [[], ["--prelude"], ["--prelude", "v2"], ["--prelude", "v1", "extra"],
                                  ["--code", "/etc/passwd"], ["--prelude", "lazy-seaborn"]])
def test_runner_rejects_any_other_argv_without_running_anything(argv, capsys):
    assert runner.main(argv) == runner.EXIT_USAGE
    assert "insightpilot-runner" in capsys.readouterr().err


def test_runner_without_its_container_input_refuses_and_executes_nothing(monkeypatch, capsys):
    assert not os.path.exists(runner.CODE_FILE)
    calls = []
    monkeypatch.setattr(runner, "compose", lambda *a: calls.append(a))
    assert runner.main(["--prelude", "v1"]) == runner.EXIT_USAGE and calls == []


def test_runner_has_exactly_one_execution_point_inside_main():
    tree = ast.parse(open(os.path.join(RUNTIME_DIR, "runner.py"), encoding="utf-8").read())
    execs = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) in ("exec", "eval")]
    assert len(execs) == 1
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert execs[0] in list(ast.walk(main))
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                and any(a.name.split(".")[0] in ("subprocess", "socket", "ctypes", "multiprocessing")
                        for a in n.names)]


# ── supervisor argv ──────────────────────────────────────────────────────────

def test_supervisor_accepts_exactly_the_argv_the_host_builds():
    mode, run_id, prelude, limits = supervisor.parse_args(supervisor_args(RUN))
    assert (mode, run_id, prelude) == ("run", RUN, "v1")
    for field in ("code_wall_seconds", "stdout_bytes", "stderr_head_bytes", "stderr_tail_bytes", "chart_bytes",
                  "chart_max_side", "chart_max_pixels", "memory_bytes", "pids"):
        assert getattr(limits, field) == getattr(DEFAULT_LIMITS, field), field


def _mutate(argv, flag, value=None, drop=False, dup=False):
    out = list(argv)
    i = out.index(flag)
    if drop:
        del out[i:i + 2]
    elif dup:
        out += [flag, out[i + 1]]
    else:
        out[i + 1] = value
    return out


@pytest.mark.parametrize("make", [
    lambda a: _mutate(a, "--wall", drop=True), lambda a: _mutate(a, "--wall", dup=True),
    lambda a: _mutate(a, "--wall", "0"), lambda a: _mutate(a, "--wall", "030"), lambda a: _mutate(a, "--wall", "3 0"),
    lambda a: _mutate(a, "--wall", "-1"), lambda a: _mutate(a, "--stdout-cap", "1e6"),
    lambda a: _mutate(a, "--mode", "shell"), lambda a: _mutate(a, "--prelude", "custom"),
    lambda a: _mutate(a, "--run-id", "X" * 32), lambda a: a + ["--code", "print(1)"], lambda a: a + ["--extra"],
    lambda a: _mutate(a, "--chart-cap", str(64 * 1024 * 1024)),        # violates Limits (chart > /out)
])
def test_any_other_supervisor_argv_exits_78_without_a_frame_or_a_child(make):
    os_api = FakeOS(code="print('x')")
    assert supervisor.supervise(make(supervisor_args(RUN)), os_api) == supervisor.EXIT_USAGE
    assert os_api.frames == [] and not [c for c in os_api.calls if c[0] in ("spawn", "install_signal_plan")]


# ── supervisor lifecycle ─────────────────────────────────────────────────────

def _decode(os_api, run_id=RUN, limits=DEFAULT_LIMITS):
    assert len(os_api.frames) == 1
    return frame.decode_frame_bytes(os_api.frames[0], run_id, limits)


def test_success_produces_one_valid_frame_after_every_safety_step():
    os_api = FakeOS(code="print('CLAIM row_count=293')\nprint('done')")
    assert supervisor.supervise(supervisor_args(RUN), os_api) == 0
    d = _decode(os_api)
    assert d.child_exit == 0 and d.stdout == b"CLAIM row_count=293\ndone\n" and d.stderr == b""
    frame.verify_attestation(d.attestation, frame.expected_attestation(DEFAULT_LIMITS))
    order = [c[0] for c in os_api.calls]
    assert order.index("install_signal_plan") < order.index("set_not_dumpable") < order.index("spawn")
    assert order.index("spawn") < order.index("kill_all") < order.index("reap_all") < order.index("write_frame")
    plan = next(c[1] for c in os_api.calls if c[0] == "install_signal_plan")
    assert plan["SIGCHLD"] == "handle" and plan["SIGPIPE"] == "ignore" and plan["SIGINT"] == "default"
    assert "SIGKILL" not in plan
    argv, env = next(c[1:] for c in os_api.calls if c[0] == "spawn")
    assert argv == ["/usr/local/bin/python3", "-I", "-B", "/opt/ip/runner.py", "--prelude", "v1"]
    assert env == supervisor.CHILD_ENV and not {"GOOGLE_API_KEY", "HTTP_PROXY", "https_proxy"} & set(env)


def test_lazy_seaborn_reaches_the_runner_only_as_an_enum():
    os_api = FakeOS(code="print('x')")
    assert supervisor.supervise(supervisor_args(RUN, prelude="lazy_seaborn"), os_api) == 0
    assert next(c[1] for c in os_api.calls if c[0] == "spawn")[-2:] == ["--prelude", "lazy_seaborn"]


def test_code_errors_timeouts_and_oom_are_reported_in_the_frame():
    os_api = FakeOS(code="print('a')\nfail(\"KeyError: 'Sales'\")", chart=png())
    assert supervisor.supervise(supervisor_args(RUN), os_api) == 0
    d = _decode(os_api)
    assert d.child_exit == 1 and b"KeyError: 'Sales'" in d.stderr and d.chart is None
    assert not [c for c in os_api.calls if c[0] == "read_nofollow"]          # no chart from a failed run

    hung = FakeOS(code="print('x')", hang=True)
    assert supervisor.supervise(supervisor_args(RUN), hung) == 0
    d = _decode(hung)
    assert d.timed_out and d.child_signal == signal.SIGKILL and not d.oom_killed

    oom = FakeOS(code="print('x')", oom=True)
    assert supervisor.supervise(supervisor_args(RUN), oom) == 0
    d = _decode(oom)
    assert d.oom_killed and d.child_signal == signal.SIGKILL and not d.timed_out


def test_output_floods_are_capped_while_collecting():
    lim = Limits(stdout_bytes=200_000)
    os_api = FakeOS(stdout=b"", flood=50)
    assert supervisor.supervise(supervisor_args(RUN, limits=lim), os_api) == 0
    d = _decode(os_api, limits=lim)
    assert d.stdout_truncated and len(d.stdout) == lim.stdout_bytes


@pytest.mark.parametrize("kw", [{"survivors": [77]}, {"unreaped": True}])
def test_surviving_processes_or_an_unreaped_child_mean_no_frame(kw):
    os_api = FakeOS(code="print('x')", **kw)
    assert supervisor.supervise(supervisor_args(RUN), os_api) == supervisor.EXIT_UNSAFE_STATE
    assert os_api.frames == [] and os_api.diagnostics


def test_self_test_modes_run_only_the_fixed_trusted_programs():
    a = FakeOS()
    assert supervisor.supervise(supervisor_args(RUN, mode="attest-only"), a) == 0
    assert not [c for c in a.calls if c[0] == "spawn"]
    frame.verify_attestation(_decode(a).attestation, frame.expected_attestation(DEFAULT_LIMITS))
    for mode, arg in (("selftest-oom", str(DEFAULT_LIMITS.memory_bytes)), ("selftest-pids", "64")):
        s = FakeOS(stdout=b"", exit_code=4)
        supervisor.supervise(supervisor_args(RUN, mode=mode), s)
        assert next(c[1] for c in s.calls if c[0] == "spawn") == [
            "/usr/local/bin/python3", "-I", "-B", "/opt/ip/selftest.py", mode.split("-")[1], arg]


@pytest.mark.parametrize("kw", [
    {"status": __import__("tests.v2.p36.fake_os", fromlist=["GOOD_STATUS"]).GOOD_STATUS.replace(
        "CapEff:\t0000000000000000", "CapEff:\t00000000a80425fb")},
    {"net": ("lo", "eth0")}, {"memory_max": "max\n"}, {"pids_max": "4096\n"}, {"dev_writable": True},
    {"status": __import__("tests.v2.p36.fake_os", fromlist=["GOOD_STATUS"]).GOOD_STATUS.replace(
        "Seccomp:\t2", "Seccomp:\t0")},
])
def test_a_weakened_container_is_reported_and_refused_by_the_host(kw):
    os_api = FakeOS(code="print('x')", **kw)
    supervisor.supervise(supervisor_args(RUN), os_api)
    with pytest.raises(SandboxUnavailableError, match="attestation does not match"):
        d = frame.decode_frame_bytes(os_api.frames[0], RUN, DEFAULT_LIMITS)
        frame.verify_attestation(d.attestation, frame.expected_attestation(DEFAULT_LIMITS))


# ── stage A charts ───────────────────────────────────────────────────────────

def test_a_matplotlib_style_chart_is_canonicalised_and_passes_host_stage_b():
    from PIL import Image
    import io
    from v2.execution.artifacts import reencode_png, validate_png_structure
    raw = png((40, 30), metadata=True)
    with pytest.raises(Exception):
        validate_png_structure(raw)                         # tEXt/pHYs: stage B alone rejects raw charts (B-6)
    os_api = FakeOS(code="print('chart')", chart=raw, out_extra=["notes.txt", "x.png"])
    assert supervisor.supervise(supervisor_args(RUN), os_api) == 0
    d = _decode(os_api)
    assert d.chart_reject_reason is None and d.out_other_entries == 2
    validate_png_structure(d.chart)
    trusted = reencode_png(d.chart)
    with Image.open(io.BytesIO(raw)) as a, Image.open(io.BytesIO(trusted.png)) as b:
        assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes()      # pixels preserved


@pytest.mark.parametrize("kind,chart,reason", [
    ("symlink", b"x", "symlink"), ("hardlink", None, "hardlink"), ("fifo", b"", "not_regular_file"),
    ("swapped", None, "truncated"), ("file", b"\x89PNG\r\n\x1a\nnot really", "decode_failed"),
    ("file", b"", "empty"),
])
def test_unsafe_or_invalid_chart_entries_are_rejected_by_reason_code(kind, chart, reason):
    os_api = FakeOS(code="print('x')", chart=png() if chart is None else chart, chart_kind=kind)
    assert supervisor.supervise(supervisor_args(RUN), os_api) == 0
    d = _decode(os_api)
    assert d.chart is None and d.chart_reject_reason == reason
    if kind in ("symlink", "hardlink", "fifo"):
        assert not [c for c in os_api.calls if c[0] == "read_nofollow"]


def test_oversized_and_overdimensioned_charts_are_rejected():
    lim = Limits(chart_max_side=16, chart_max_pixels=256)
    os_api = FakeOS(code="print('x')", chart=png((32, 32)))
    supervisor.supervise(supervisor_args(RUN, limits=lim), os_api)
    assert _decode(os_api, limits=lim).chart_reject_reason == "dimensions_exceeded"
    big = FakeOS(code="print('x')", chart=b"\x89PNG" + b"0" * (DEFAULT_LIMITS.chart_bytes + 1))
    supervisor.supervise(supervisor_args(RUN), big)
    assert _decode(big).chart_reject_reason == "too_large"


# ── what P3.6-4 must place in the image (/opt/ip) ────────────────────────────

IMAGE_LAYOUT_MODULES = {"v2", "v2.execution", "v2.execution.errors", "v2.execution.isolation",
                        "v2.execution.limits", "v2.execution.results", "v2.execution.frame",
                        "v2.execution.supervisor_helpers", "config", "config.feature_flags"}


def test_the_supervisor_and_runner_import_closure_is_exactly_the_planned_image_layout():
    import subprocess
    code = ("import sys; sys.path[:0] = [%r, %r]; before = set(sys.modules)\n"
            "import supervisor, runner, selftest\n"
            "new = set(sys.modules) - before\n"
            "print(','.join(sorted(m for m in new if m.split('.')[0] in ('v2', 'config', 'agent', 'sandbox',"
            " 'sandbox_v2', 'utils', 'ui', 'multi_agent', 'evaluation', 'streamlit', 'pandas', 'langgraph'))))"
            ) % (RUNTIME_DIR, ROOT)
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert set(out.stdout.strip().split(",")) == IMAGE_LAYOUT_MODULES
