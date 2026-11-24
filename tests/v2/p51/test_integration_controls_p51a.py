"""
P5.1A: branch-integration and preservation controls.

- whole-repository executor call-site census (allowlist);
- bootstrap: the P5 orchestrator flag only together with hardened execution; production still fails closed;
- V2-specific LangGraph environment (requirements-v2.txt) and the unchanged examination requirements;
- V1 application, P4.1 optimisation/evidence and P5.0 evidence unchanged; standard launch unaffected.
"""

import ast
import os
import re
import subprocess
import sys

import pytest

from tests.v2.golden.reference import ROOT

EXAM_COMMIT = "fd5783c6202e3ea506a69e607160617a23eed807"
P5_COMMIT = "1444491e9fc64460c9db18f8c224b4ee19872b83"
P3_PROBE_COMMIT = "4cd107e0eeca61996a4e1b41fe473f93ed14a05a"


def _git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed (a real Git checkout is required): {r.stderr.strip()}")
    return r.stdout


# ── whole-repository executor census ─────────────────────────────────────────

# Every reference to an `execute_code` name outside tests/, with why it is allowed.
CENSUS_ALLOWLIST = {
    ("agent/nodes/executor.py", "import sandbox.executor"),        # V1 executor_node (P3 binding rebinds it)
    ("agent/nodes/executor.py", "name"),
    ("sandbox/executor.py", "def"),                                # the V1 executor (tripwired when hardened)
    ("sandbox_v2/executor.py", "def"),                             # the P4.1 executor (tripwired when hardened)
    ("evaluation/harness.py", "import sandbox_v2.executor"),       # P4.1 benchmark variant (V1 process model)
    ("evaluation/harness.py", "name"),
    ("evaluation/isolation.py", "attr executor_node"),             # P4 timing wrapper around executor_node
    ("evaluation/isolation.py", "string"),                         # setattr(executor_node, "execute_code", timed)
    ("multi_agent/execution.py", "attr sandbox.executor"),         # P5 legacy ports: fixtures, never hardened
    ("multi_agent/execution.py", "import sandbox_v2.executor"),
    ("multi_agent/execution.py", "name"),
    ("scripts/p3/workload_compat.py", "attr ex"),                  # P3.5a dev script: golden fixtures only
    ("v2/execution/binding.py", "attr node_module"),               # the binding and the tripwire itself
    ("v2/execution/binding.py", "attr v1_module"),
    ("v2/execution/binding.py", "attr lazy_module"),
    ("v2/execution/binding.py", "attr _installed['lazy_module']"),
    ("evaluation/live.py", "attr m"),                              # P6: replaces both V1 executors with a refusing tripwire
}
_SKIP_DIRS = {".git", "venv", "tests", "docs", "__pycache__", "node_modules"}


def _census():
    refs = set()
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=rel)
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and any(a.name == "execute_code" for a in n.names):
                    refs.add((rel, "import " + (n.module or "")))
                elif isinstance(n, ast.Import) and any(a.name.endswith(".execute_code") for a in n.names):
                    refs.add((rel, "import-module"))
                elif isinstance(n, ast.Attribute) and n.attr == "execute_code":
                    refs.add((rel, "attr " + ast.unparse(n.value)))
                elif isinstance(n, ast.Name) and n.id == "execute_code":
                    refs.add((rel, "name"))
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "execute_code":
                    refs.add((rel, "def"))
                elif isinstance(n, ast.Constant) and n.value == "execute_code":
                    refs.add((rel, "string"))          # getattr(..., "execute_code") and similar
    return refs


def test_every_executor_reference_in_the_repository_is_allowlisted():
    found = _census()
    assert found - CENSUS_ALLOWLIST == set(), sorted(found - CENSUS_ALLOWLIST)
    assert CENSUS_ALLOWLIST - found == set(), "stale allowlist entries: " + repr(sorted(CENSUS_ALLOWLIST - found))


def test_no_new_subprocess_launch_sites_outside_the_known_executors():
    allowed = {"sandbox/executor.py", "sandbox_v2/executor.py", "scripts/p3/rootless.py", "scripts/p3/image_tool.py",
               "scripts/p3/workload_compat.py", "scripts/p3/hermetic_reference.py", "scripts/p3/ci_report.py",
               "scripts/p3/image_inspect.py", "evaluation/harness.py",
               "v2/execution/image/runtime/supervisor.py",          # P3.6-2: PID 1 starts the runner child
               "v2/execution/container_runtime.py",                 # P3.6-3: starts only the podman CLI
               "scripts/p3/argv_compat.py"}                         # P3.6-4B: podman create/rm/exists/--help only
    offenders = set()
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                if attrs & {"run", "Popen", "call", "check_output", "check_call"} and "subprocess" in {
                        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}:
                    offenders.add(rel)
    assert offenders <= allowed, sorted(offenders - allowed)
    for rel in ("multi_agent/execution.py", "multi_agent/agents.py", "multi_agent/supervisor.py",
                "v2/execution/dispatch.py", "v2/execution/bootstrap.py", "v2/execution/binding.py"):
        assert rel not in offenders


# ── bootstrap and flags ──────────────────────────────────────────────────────

HARD = {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"}
OPTS = {"server.fileWatcherType": "none", "server.runOnSave": False}


def test_orchestrator_alone_is_still_refused():
    from v2.execution import bootstrap
    with pytest.raises(bootstrap.BootstrapRefused) as info:
        bootstrap.ensure_ready(env={"IP_FF_ORCHESTRATOR": "v2"}, options=OPTS)
    # both independent refusal reasons must be present (mutation check N6)
    assert "not integrated yet: IP_FF_ORCHESTRATOR" in str(info.value)
    assert "IP_FF_ORCHESTRATOR requires hardened container execution" in str(info.value)


def test_orchestrator_with_hardening_still_fails_closed_in_production():
    from v2.execution import bootstrap
    with pytest.raises(bootstrap.BootstrapRefused) as info:
        bootstrap.ensure_ready(env={**HARD, "IP_FF_ORCHESTRATOR": "v2"}, options=OPTS)
    msg = str(info.value)
    assert "no validated container backend" in msg and "never used as a fallback" in msg
    assert "not integrated yet" not in msg


def test_other_v2_features_with_hardening_are_still_refused():
    from v2.execution import bootstrap
    for extra in ({"IP_FF_TRACING": "1"}, {"IP_FF_VERIFIER": "shadow"}, {"IP_FF_MAX_PARALLEL_TASKS": "2",
                                                                         "IP_FF_ORCHESTRATOR": "v2"}):
        bootstrap._reset_for_tests()
        with pytest.raises(bootstrap.BootstrapRefused, match="not integrated yet"):
            bootstrap.ensure_ready(env={**HARD, **extra}, options=OPTS)


def test_hardened_test_mode_accepts_the_orchestrator(hardened_p5):
    assert hardened_p5.bootstrap.mode() is hardened_p5.bootstrap.Mode.HARDENED
    assert hardened_p5.bootstrap.status()["runtime_is_test_double"] is True


def test_the_launcher_still_refuses_every_non_default_flag():
    import importlib
    launcher = importlib.import_module("v2.launch_app")
    with pytest.raises(launcher.LaunchRefused):
        launcher.check_flags({"IP_FF_ORCHESTRATOR": "v2", **HARD})


def test_resolve_port_follows_the_hardened_flags():
    from config.feature_flags import load_flags
    from multi_agent import execution as ex
    flags = load_flags({**HARD, "IP_FF_ORCHESTRATOR": "v2"})
    for trust in ex.CodeTrust:
        assert isinstance(ex.resolve_port(trust, "lazy-seaborn", flags=flags), ex.ContainerExecutionPort)
    assert isinstance(ex.resolve_port(ex.CodeTrust.SCRIPTED_FIXTURES, flags=load_flags({})),
                      ex.LegacyV1SubprocessExecution)         # fixture-only offline tests keep working


# ── V2-specific LangGraph environment ────────────────────────────────────────

def _pins(path):
    out = {}
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if "==" in line:
                name, version = line.split("==")
                out[name.strip().lower().replace("_", "-")] = version.strip()
    return out


def test_examination_requirements_are_unchanged():
    assert _git("show", f"{EXAM_COMMIT}:requirements.txt") == open(os.path.join(ROOT, "requirements.txt"),
                                                                    encoding="utf-8").read()


def test_v2_requirements_pin_the_tested_pair_from_environment_b():
    from multi_agent.compat import TESTED_PAIR
    v2 = _pins("requirements-v2.txt")
    env_b = _pins("docs/v2/ENV_B_PINS.txt")
    assert v2 == TESTED_PAIR
    assert all(env_b[k] == v for k, v in v2.items())
    with open(os.path.join(ROOT, "requirements-v2.txt"), encoding="utf-8") as f:
        assert "-r requirements.txt" in f.read()


def test_the_running_environment_has_every_feature_multi_agent_needs():
    from multi_agent import compat
    assert compat.missing_features() == []
    assert type(compat.in_memory_saver()).__name__ == "InMemorySaver"


def test_a_too_old_langgraph_fails_with_a_clear_message(monkeypatch):
    from multi_agent import compat
    monkeypatch.setattr(compat, "missing_features", lambda: ["langgraph.types.interrupt / Command"])
    with pytest.raises(compat.MultiAgentEnvironmentError, match="requirements-v2.txt"):
        compat.in_memory_saver()


# ── V1, P4.1 and P5.0 preservation ───────────────────────────────────────────

def test_v1_application_paths_are_byte_identical_to_the_examination_commit():
    changed = _git("diff", "--name-only", EXAM_COMMIT, "--", "ui", "agent", "sandbox", "utils", "rules",
                   "skills", "data", "lessons", "requirements.txt").split()
    assert changed == []
    assert _git("status", "--porcelain", "--", "ui", "agent", "sandbox", "utils", "rules", "skills", "data",
                "lessons").strip() == ""


def test_p41_optimisation_and_all_benchmark_evidence_are_unchanged():
    changed = _git("diff", "--name-only", P5_COMMIT, "--", "sandbox_v2", "docs/v2/p4", "docs/v2/p5",
                   "evaluation/harness.py", "evaluation/paired.py", "evaluation/isolation.py",
                   "evaluation/metrics.py", "evaluation/provenance.py", "evaluation/reports.py").split()
    assert changed == []
    from evaluation import HARNESS_VERSION
    assert HARNESS_VERSION == "p4.1"


P51A_P3_FILES = {"v2/execution/runtime.py", "v2/execution/datasets.py", "v2/execution/dispatch.py",
                 "v2/execution/binding.py", "v2/execution/bootstrap.py", "tests/v2/p3/leak_guard.py",
                 "tests/v2/p3/test_supervisor_helpers_p34.py", "tests/v2/p3/test_workspace_lease_p33.py"}
# P3.6 stages 0-3 (approved R1-R6): each file listed deliberately; nothing else under these roots may change.
P36_P3_FILES = {
    # P3.6-0: evidence reconciliation (comment/docstring/limit alignment only)
    ".github/workflows/p3-ubuntu-rootless-probe.yml", "scripts/p3/rootless.py", "tests/v2/p3/test_rootless_p35b.py",
    "v2/__init__.py", "v2/execution/results.py", "v2/execution/image/image-expectations.json",
    # P3.6-1: pure host components
    "v2/execution/host_checks.py", "v2/execution/podman_argv.py", "v2/execution/result_mapping.py",
    "tests/v2/p3/test_contract_purity.py",
    # P3.6-2: trusted in-image source (outside the build context), prelude generator, image-listing guard
    "v2/execution/image/runtime/supervisor.py", "v2/execution/image/runtime/runner.py",
    "v2/execution/image/runtime/selftest.py", "v2/execution/image/runtime/preludes.json",
    "scripts/p3/gen_preludes.py", "tests/v2/p3/test_image_p35a.py",
    # P3.6-3: the runtime and its validation-only bootstrap entry point
    "v2/execution/container_runtime.py", "tests/v2/p3/test_bootstrap_p3.py",
    # P3.6-4A (approved, offline only): image-source integration
    "v2/execution/image_layout.py", "v2/execution/image/Containerfile", "scripts/p3/image_tool.py",
    "scripts/p3/image_inspect.py",
    # P3.6-4B (approved): CI revision and report; D3 argument-compatibility diagnostic
    "scripts/p3/ci_report.py", "scripts/p3/argv_compat.py",
    # P3.6-4B D2 (approved): exact writable-mount set
    "v2/execution/frame.py",
}
# UI-1 (approved, isolated branch): a NEW UI-agnostic application service under v2/; it changes no P3 file and is
# not wired to any entry point (tests/v2/ui1, tests/v2/p3/test_contract_purity.py).
UI1_FILES = {"v2/app_service.py"}


def test_p3_lineage_is_fully_contained_and_only_reviewed_p3_files_changed():
    _git("merge-base", "--is-ancestor", P3_PROBE_COMMIT, "HEAD")
    _git("merge-base", "--is-ancestor", P5_COMMIT, "HEAD")
    changed = set(_git("diff", "--name-only", P3_PROBE_COMMIT, "--", "v2", "scripts/p3", "tests/v2/p3",
                       ".github").split())
    approved = P51A_P3_FILES | P36_P3_FILES | UI1_FILES
    assert changed <= approved, sorted(changed - approved)


def test_the_ci_workflow_keeps_every_original_gate_and_phase_unchanged():
    """P3.6-4B (approved) may ADD the content gate and Phase F; no original step may change or disappear."""
    import yaml
    path = ".github/workflows/p3-ubuntu-rootless-probe.yml"
    old = yaml.safe_load(_git("show", f"{P3_PROBE_COMMIT}:{path}"))["jobs"]["probe"]["steps"]
    new = yaml.safe_load(open(os.path.join(ROOT, path), encoding="utf-8"))["jobs"]["probe"]["steps"]
    names = [s["name"] for s in new]
    assert [n for n in names if n in {s["name"] for s in old}] == [s["name"] for s in old]   # all kept, same order
    by_name = {s["name"]: s for s in new}
    readiness = "Readiness tests, including the workflow-policy test (must run, never skip)"
    for step in old:
        if step["name"] == readiness:                     # one extra offline test file, nothing removed
            assert re.sub(r" \\\n +tests/v2/p36/test_container_suite_static_p364b\.py", "",
                          by_name[step["name"]]["run"]) == step["run"]
            continue
        assert by_name[step["name"]] == step, step["name"]
    added = [n for n in names if n not in {s["name"] for s in old}]
    assert added[0].startswith("Approved-content gate") and all(
        n.startswith("Phase F - ") for n in added[1:]) and len(added) == 6, added    # D3: + compat diagnostic


def test_standard_streamlit_entry_point_imports_no_v2_code():
    code = ("import sys, runpy; sys.argv=['x']; import ast; "
            "src=open('ui/app.py').read(); t=ast.parse(src); "
            "mods={n.module for n in ast.walk(t) if isinstance(n, ast.ImportFrom)} | "
            "{a.name for n in ast.walk(t) if isinstance(n, ast.Import) for a in n.names}; "
            "bad=[m for m in mods if m and m.split('.')[0] in ('v2','multi_agent','sandbox_v2','evaluation')]; "
            "print(bad)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stderr
