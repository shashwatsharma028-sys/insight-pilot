"""
P4.1: the lazy-seaborn V2 sandbox prelude and executor, and the A/B benchmark machinery.

Parity is checked against the unchanged V1 executor through real sandbox
subprocesses. Probe scripts that inspect sys.modules are trusted test code,
run directly on the wrapper output; generated-code-style snippets go through
both executors, including the AST safety scan.
"""

import ast
import hashlib
import os
import subprocess
import sys

import pytest

import sandbox.executor as v1
from evaluation import harness, paired
from sandbox_v2 import executor as v2
from sandbox_v2.lazy_prelude import (LAZY_SEABORN_BLOCK, USER_CODE_MARKER, V1_SEABORN_LINE, PreludeMismatch,
                                     derive_lazy_wrapper_text, wrap_code_lazy_seaborn)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CSV = os.path.join(ROOT, "data", "sales_data.csv")
SEABORN_CMAPS = ["rocket", "mako", "flare", "crest", "vlag", "icefire"]


def _run_script(text, tmp_path, name="probe.py"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return subprocess.run([sys.executable, str(p)], capture_output=True, text=True, timeout=120,
                          env={"PATH": os.environ.get("PATH", "/usr/bin"), "MPLBACKEND": "Agg"})


def _pixels(path):
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        return img.size, hashlib.sha256(img.mode.encode() + img.tobytes()).hexdigest()


# ── prelude derivation ───────────────────────────────────────────────────────

def test_v2_wrapper_is_v1_with_exactly_one_line_replaced():
    code = "print(df.shape)\n"
    a, b = v1.wrap_code_safely(code, CSV, "t1"), wrap_code_lazy_seaborn(code, CSV, "t1")
    assert b.replace(LAZY_SEABORN_BLOCK, V1_SEABORN_LINE, 1) == a
    prelude = b.split(USER_CODE_MARKER)[0]
    assert "\n" + V1_SEABORN_LINE not in prelude                          # no top-level eager import left
    assert "except Exception:\n    import seaborn as sns\n" in prelude     # only the documented fallback
    assert b.split(USER_CODE_MARKER)[1] == a.split(USER_CODE_MARKER)[1]


def test_user_code_is_never_rewritten():
    code = "import seaborn as sns\nimport seaborn as sns\nprint(1)\n"
    b = wrap_code_lazy_seaborn(code, CSV, "t1")
    assert b.split(USER_CODE_MARKER)[1].count(V1_SEABORN_LINE) == 2


@pytest.mark.parametrize("text", [
    "\nimport pandas as pd\nimport seaborn as sns\n",                                  # no marker
    "\nimport pandas as pd\n" + USER_CODE_MARKER + "\nimport seaborn as sns\n",         # line only in user code
    "\nimport pandas as pd\nimport seaborn as sns\nimport seaborn as sns\n" + USER_CODE_MARKER,  # twice
    "\nimport polars as pl\nimport seaborn as sns\n" + USER_CODE_MARKER,               # unexpected prelude
])
def test_unexpected_v1_prelude_shapes_are_refused(text):
    with pytest.raises(PreludeMismatch):
        derive_lazy_wrapper_text(text)


def test_v2_refuses_to_guess_if_the_v1_prelude_changes(monkeypatch):
    monkeypatch.setattr(v1, "wrap_code_safely", lambda c, p, t: "\nimport pandas as pd\n" + USER_CODE_MARKER + c)
    with pytest.raises(PreludeMismatch):
        wrap_code_lazy_seaborn("print(1)", CSV, "t1")


def test_v2_executor_is_statement_for_statement_the_v1_executor():
    """AST of V2 execute_code == V1's after mapping _v1.X -> X and the wrapper name; docstrings and comments ignored."""
    def body(module_path, name):
        with open(module_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        fn.body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None),
                                                                                      ast.Constant))]
        return fn

    class Normalize(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.value, ast.Name) and node.value.id == "_v1":
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return node

        def visit_Name(self, node):
            if node.id == "wrap_code_lazy_seaborn":
                node.id = "wrap_code_safely"
            return node
    a = body(os.path.join(ROOT, "sandbox", "executor.py"), "execute_code")
    b = Normalize().visit(body(os.path.join(ROOT, "sandbox_v2", "executor.py"), "execute_code"))
    assert ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


# ── the lazy proxy in a real subprocess ──────────────────────────────────────

PROBE = '''
import sys as _s
import numpy as _n
import matplotlib as _m
print("BEFORE", "seaborn" in _s.modules, "scipy" in _s.modules, type(sns).__name__)
_names = %r
print("CMAPS", all(n in _m.colormaps and n + "_r" in _m.colormaps for n in _names))
_luts = {n: _n.asarray(_m.colormaps[n].colors).tolist() for n in _names}
_luts.update({n + "_r": _n.asarray(_m.colormaps[n + "_r"].colors).tolist() for n in _names})
import seaborn as _real
print("SAME_DATA", all(_luts[k] == _n.asarray(getattr(_real.cm, k).colors).tolist() for k in _luts))
''' % (SEABORN_CMAPS,)


def test_code_that_never_uses_sns_does_not_import_seaborn_or_scipy_but_has_its_colormaps(tmp_path):
    r = _run_script(wrap_code_lazy_seaborn(PROBE, CSV, "t1"), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "BEFORE False False _IPLazySeaborn" in r.stdout
    assert "CMAPS True" in r.stdout and "SAME_DATA True" in r.stdout
    v1r = _run_script(v1.wrap_code_safely(PROBE, CSV, "t1"), tmp_path, "v1.py")
    assert "BEFORE True True module" in v1r.stdout and "SAME_DATA True" in v1r.stdout


USE = '''
import sys as _s, warnings as _w
before = "seaborn" in _s.modules
pal = sns.color_palette("deep", 3)
import seaborn as _real
print("USE", before, "seaborn" in _s.modules, sns is _real, len(pal))
print("FILTER0", _w.filters[0][:3])
print("DIR", "barplot" in dir(_real))
try:
    sns.definitely_not_an_attribute
except AttributeError as e:
    print("ATTR", type(e).__name__)
'''


def test_first_use_imports_seaborn_binds_the_module_and_restores_v1_warning_precedence(tmp_path):
    r2 = _run_script(wrap_code_lazy_seaborn(USE, CSV, "t1"), tmp_path)
    r1 = _run_script(v1.wrap_code_safely(USE, CSV, "t1"), tmp_path, "v1.py")
    assert r2.returncode == 0 and r1.returncode == 0, (r2.stderr, r1.stderr)
    assert "USE False True True 3" in r2.stdout and "USE True True True 3" in r1.stdout
    f1 = [ln for ln in r1.stdout.splitlines() if ln.startswith("FILTER0")]
    f2 = [ln for ln in r2.stdout.splitlines() if ln.startswith("FILTER0")]
    assert f1 == f2 == ["FILTER0 ('ignore', None, <class 'Warning'>)"]
    assert "DIR True" in r2.stdout and "ATTR AttributeError" in r2.stdout


@pytest.mark.parametrize("snippet,expect", [
    ("print(sorted(dir(sns))[:1] != [])", "True"),
    ("print(repr(sns).startswith(\"<module 'seaborn'\"))", "True"),
    ("import seaborn as sns\nprint(type(sns).__name__)", "module"),
    ("sns = 5\nprint(sns + 1)", "6"),
    ("del sns\nprint('deleted')", "deleted"),
    ("s = sns\nprint(s.__name__)", "seaborn"),
])
def test_adversarial_and_unusual_uses_of_sns(tmp_path, snippet, expect):
    r = _run_script(wrap_code_lazy_seaborn(snippet + "\n", CSV, "t1"), tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == expect


def test_documented_difference_type_before_first_use(tmp_path):
    r = _run_script(wrap_code_lazy_seaborn("import types\nprint(isinstance(sns, types.ModuleType))\n", CSV, "t"),
                    tmp_path)
    assert r.stdout.strip() == "False"                                       # documented in lazy_prelude.py


def test_unexpected_colormap_source_falls_back_to_the_eager_v1_import(tmp_path):
    text = wrap_code_lazy_seaborn('import sys\nprint("seaborn" in sys.modules, type(sns).__name__)\n', CSV, "t1")
    broken = text.replace('_head = "from matplotlib import colors', '_head = "from somewhere import colors', 1)
    assert broken != text
    r = _run_script(broken, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "True module", r.stderr


# ── executor parity with V1 (real sandbox, AST scan included) ────────────────

MPL_CHART = '''
by_region = df.groupby("region")["sales_amount"].sum()
by_region.plot(kind="bar", title="Sales by region")
plt.savefig(CHART_PATH, bbox_inches="tight")
plt.close("all")
print("rows", len(df))
'''
SNS_CHART = '''
sns.barplot(data=df, x="region", y="sales_amount", errorbar=None)
plt.savefig(CHART_PATH, bbox_inches="tight")
plt.close("all")
print("rows", len(df))
'''
CMAP_CHART = '''
plt.scatter(df["sales_amount"], df["sales_target"], c=df["customer_satisfaction"].fillna(0), cmap="mako")
plt.savefig(CHART_PATH, bbox_inches="tight")
plt.close("all")
print("ok")
'''


@pytest.mark.parametrize("code,task", [(MPL_CHART, "p41_mpl"), (SNS_CHART, "p41_sns"), (CMAP_CHART, "p41_cmap")])
def test_charts_are_pixel_identical_between_v1_and_v2(code, task, isolated_outputs):
    r1 = v1.execute_code(code, CSV, task + "_v1")
    r2 = v2.execute_code(code, CSV, task + "_v2")
    assert r1[0] and r2[0], (r1[1][-300:], r2[1][-300:])
    assert r1[1] == r2[1] and r1[3] is r2[3] is None
    assert os.path.dirname(r2[2]) == str(isolated_outputs["charts"])
    assert _pixels(r1[2]) == _pixels(r2[2])


@pytest.mark.parametrize("code", [
    'print(df["no_such_column"].sum())',
    'def broken(:\n    pass',
    'print(1 / 0)',
    'raise ValueError("bad value")',
    'import os\nos.listdir(".")',
    'open("/etc/passwd").read()',
])
def test_errors_and_safety_blocks_are_classified_identically(code, isolated_outputs):
    r1, r2 = v1.execute_code(code, CSV, "p41_err"), v2.execute_code(code, CSV, "p41_err")
    assert (r1[0], r1[2], r1[3]) == (r2[0], r2[2], r2[3])
    assert not r1[0]
    last = lambda s: s.strip().splitlines()[-1] if s.strip() else ""  # noqa: E731
    assert last(r1[1]) == last(r2[1])


def test_timeouts_are_identical(monkeypatch, isolated_outputs):
    monkeypatch.setattr(v1, "MAX_EXECUTION_TIME", 2)
    r1, r2 = v1.execute_code("while True:\n    pass", CSV, "t"), v2.execute_code("while True:\n    pass", CSV, "t")
    assert r1 == r2 == (False, "Execution timed out after 2s.", None, r1[3])


def test_no_temp_files_environment_changes_or_output_leaks(tmp_path, monkeypatch, isolated_outputs):
    import tempfile
    tmpdir = tmp_path / "tmpfiles"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    env_before = dict(os.environ)
    repo_reports = sorted(os.listdir(os.path.join(ROOT, "reports")))
    for code in (MPL_CHART, 'print(df["nope"])', "import os"):
        v2.execute_code(code, CSV, "p41_leak")
    assert list(tmpdir.iterdir()) == []
    assert dict(os.environ) == env_before
    assert sorted(os.listdir(os.path.join(ROOT, "reports"))) == repo_reports


def test_no_v1_module_imports_sandbox_v2():
    for folder in ("agent", "sandbox", "utils", "ui"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        assert "sandbox_v2" not in f.read(), os.path.join(dirpath, name)


# ── variants ─────────────────────────────────────────────────────────────────

def test_variants_select_the_implementation_only_when_asked():
    assert harness.VARIANTS["v1"].execute_factory is None
    assert harness.VARIANTS["v1-control"].execute_factory is None
    assert harness.VARIANTS["v2-lazy-seaborn"].execute_factory() is v2.execute_code


def test_a_v2_run_uses_v2_and_restores_v1_afterwards(tmp_path, monkeypatch):
    import agent.nodes.executor as node
    original = node.execute_code
    calls = {"v2": 0}
    real_v2 = v2.execute_code

    def counting(*a, **k):
        calls["v2"] += 1
        return real_v2(*a, **k)
    monkeypatch.setattr(v2, "execute_code", counting)
    r = harness.run_benchmark(str(tmp_path / "b2"), datasets=["ecommerce"], repetitions=1, variant="v2-lazy-seaborn")
    assert calls["v2"] == 3 and node.execute_code is original
    assert r["summary"]["answers"]["correct"] == 7 and r["summary"]["charts"]["valid"] == 1
    calls["v2"] = 0
    harness.run_benchmark(str(tmp_path / "b1"), datasets=["ecommerce"], repetitions=1, variant="v1")
    assert calls["v2"] == 0 and node.execute_code is original


def test_schedule_is_counterbalanced():
    s = harness.ab_schedule(["v1", "v2", "c"], ["a", "b", "c2", "d"], 6)
    assert len(s) == 3 * 4 * 6
    bal = harness.schedule_balance(s)
    assert all(bal[v] == {0: 8, 1: 8, 2: 8} for v in ("v1", "v2", "c"))
    cells = {}
    for x in s:
        cells.setdefault(x["cell"], []).append(x["variant"])
    assert all(sorted(v) == ["c", "v1", "v2"] for v in cells.values())
    two = harness.schedule_balance(harness.ab_schedule(["v1", "v2"], ["a", "b"], 3))
    assert two == {"v1": {0: 3, 1: 3}, "v2": {0: 3, 1: 3}}


@pytest.mark.parametrize("kwargs", [dict(variants=["v1"]), dict(variants=["v1", "v1"]),
                                    dict(variants=["v1", "nope"]), dict(repetitions=0)])
def test_ab_runner_validates_arguments(tmp_path, kwargs):
    with pytest.raises(ValueError):
        harness.run_ab_benchmark(str(tmp_path), **kwargs)


# ── paired engine: new correctness gates ─────────────────────────────────────

def _fake(pixels="p" * 64, retries=4, calls=30, failed=4):
    runs = [{"dataset": "sales", "repetition": r, "variant": "x", "wall_seconds": 1.0,
             "executions": [{"task_id": "t1", "seconds": 0.5, "blocked_by_safety_scan": False}],
             "charts": {"per_task": {"t1": {"pixels_sha256": pixels}}}} for r in range(6)]
    s = {"repetitions": 6, "answers": {"correct": 28}, "quality": {"correct": 20},
         "tasks": {"completed_of_expected": 12, "expected_failures_observed": 1, "status_as_expected": 13,
                   "retries": retries},
         "sandbox_executions": {"total": 16, "failed": failed, "blocked_by_safety_scan": 3},
         "charts": {"valid": 7}, "reports": {"valid": 12}, "behavior_matches_v1_snapshot": 4,
         "deterministic_across_repetitions": {"sales": True}, "errors": [], "llm_calls_per_benchmark_pass": calls,
         "llm_calls_by_skill": {}, "tokens_per_benchmark_pass": {"classes": {"mock_scripted/estimated_chars_div_4": {
             "calls": calls, "input_tokens": 1, "output_tokens": 1}}}}
    return {"variant": "x", "workload": {"sha256": "w"}, "environment": {}, "summary": s, "runs": runs}


@pytest.mark.parametrize("change,metric", [
    (dict(pixels="q" * 64), "chart_pixels_identical"),
    (dict(retries=5), "retries_total"),
    (dict(retries=3), "retries_total"),
    (dict(calls=29), "llm_calls"),
    (dict(failed=5), "sandbox_failed_executions"),
])
def test_any_behaviour_change_is_a_regression(change, metric):
    c = paired.compare(_fake(), _fake(**change), resamples=200)
    assert metric in c["regressions"] and c["claims"] == [] and c["verdict"] == "REGRESSION"
    assert paired.compare(_fake(), _fake(), resamples=200)["regressions"] == []


# ── integration: full workload with the V2 executor ──────────────────────────

def test_v2_meets_every_reference_on_all_four_datasets(tmp_path):
    r = harness.run_benchmark(str(tmp_path), repetitions=1, variant="v2-lazy-seaborn")
    s = r["summary"]
    assert s["errors"] == [] and s["answers"]["correct"] == 28 and s["quality"]["correct"] == 20
    assert s["tasks"]["completed_of_expected"] == 12 and s["tasks"]["expected_failures_observed"] == 1
    assert s["tasks"]["retries_as_expected"] == 13 and s["sandbox_executions"]["blocked_by_safety_scan"] == 3
    assert s["charts"] == {"valid": 7, "expected": 7} and s["reports"]["valid"] == 12
    assert s["behavior_matches_v1_snapshot"] == 4
