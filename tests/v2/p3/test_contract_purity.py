"""
P3.1: the execution contracts are pure, and V1 never imports V2.

- The contract modules import only an explicit allowlist and call nothing
  that executes code, spawns processes or touches files or the network.
- Importing v2.execution in a fresh interpreter loads no process, network,
  LLM, Streamlit or V1 application module.
- No V1 module (agent/, sandbox/, utils/, ui/) imports the v2 package.
"""

import ast
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

CONTRACT_MODULES = [
    "v2/__init__.py",
    "v2/execution/__init__.py",
    "v2/execution/isolation.py",
    "v2/execution/limits.py",
    "v2/execution/errors.py",
    "v2/execution/results.py",
    "v2/execution/runtime.py",     # P3.2b: request type and runtime interface
    "v2/execution/host_checks.py",  # P3.6-1: pure host-suitability rules (moved from scripts/p3/rootless.py)
    "v2/execution/podman_argv.py",  # P3.6-1: pure Podman argv builders and grammar
]
ALLOWED_IMPORT_ROOTS = {"dataclasses", "enum", "typing", "re", "config", "v2"}
ALLOWED_FROM_CONFIG = {"config.feature_flags"}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open", "breakpoint", "input"}
FORBIDDEN_ATTRIBUTES = {"system", "popen", "spawn", "fork", "execv", "execve", "run_path"}


@pytest.mark.parametrize("relpath", CONTRACT_MODULES)
def test_contract_module_is_pure(relpath):
    with open(os.path.join(ROOT, relpath), encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=relpath)
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_IMPORT_ROOTS:
                    problems.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or mod.split(".")[0] not in ALLOWED_IMPORT_ROOTS:
                problems.append(f"from {mod} import ...")
            elif mod.startswith("config") and mod not in ALLOWED_FROM_CONFIG:
                problems.append(f"from {mod} import ...")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in FORBIDDEN_CALLS:
            problems.append(f"call {node.func.id}()")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            problems.append(f"attribute .{node.attr}")
    assert problems == [], f"{relpath}: {problems}"


# Reviewed V2 modules that are NOT pure contracts. Each entry needs its own
# review: P3.2a added the flags-off launcher (constrained by
# test_launcher_p3.py); P3.2b added the bootstrap, the executor binding and
# the dispatcher (constrained by test_bootstrap_p3.py). No container runtime,
# subprocess executor or supervisor exists.
REVIEWED_NON_CONTRACT_MODULES = [
    "v2/launch_app.py",              # P3.2a
    "v2/execution/bootstrap.py",     # P3.2b
    "v2/execution/binding.py",       # P3.2b
    "v2/execution/dispatch.py",      # P3.2b
    "v2/execution/datasets.py",      # P3.3 (not wired into production yet)
    "v2/execution/workspace.py",     # P3.3 (not wired into production yet)
    "v2/execution/lease.py",         # P3.3 (not wired into production yet)
    "v2/execution/frame.py",         # P3.4 (not wired into production yet)
    "v2/execution/artifacts.py",     # P3.4 (not wired into production yet)
    "v2/execution/chart_store.py",   # P3.4 (not wired into production yet)
    "v2/execution/supervisor_helpers.py",  # P3.4 pure helpers only
    "v2/execution/image/inert_entrypoint.py",  # P3.5a image entrypoint; inert (test_image_p35a.py)
    "v2/execution/result_mapping.py",  # P3.6-1: decoded frame -> ExecutionResult; chart validation and publication
    # P3.6-2: trusted in-image source (not in the build context until P3.6-4; tests/v2/p36)
    "v2/execution/image/runtime/supervisor.py",
    "v2/execution/image/runtime/runner.py",
    "v2/execution/image/runtime/selftest.py",
    "v2/execution/container_runtime.py",  # P3.6-3: rootless Podman runtime (fake podman tests only)
]


def test_every_v2_python_file_is_a_reviewed_module():
    """Scope guard: every Python file under v2/ is explicitly reviewed."""
    found = sorted(
        os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/")
        for dirpath, _, files in os.walk(os.path.join(ROOT, "v2"))
        for name in files if name.endswith(".py")
    )
    assert found == sorted(CONTRACT_MODULES + REVIEWED_NON_CONTRACT_MODULES)
    assert not set(CONTRACT_MODULES) & set(REVIEWED_NON_CONTRACT_MODULES)


def test_importing_contracts_loads_no_process_network_llm_or_v1_modules():
    code = (
        "import sys; sys.path.insert(0, %r); before = set(sys.modules)\n"
        "import v2.execution\n"
        "new = set(sys.modules) - before\n"
        "assert 'v2.launch_app' not in new, 'contracts must not import the launcher'\n"
        "assert not new & {'v2.execution.bootstrap', 'v2.execution.binding', 'v2.execution.dispatch', "
        "'v2.execution.datasets', 'v2.execution.workspace', 'v2.execution.lease', 'v2.execution.frame', "
        "'v2.execution.artifacts', 'v2.execution.chart_store', 'v2.execution.supervisor_helpers'}, "
        "'contracts must not import the bootstrap, binding, dispatcher, datasets, workspace or lease'\n"
        "roots = {m.split('.')[0] for m in new}\n"
        "print(','.join(sorted(roots)))"
    ) % ROOT
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    # -I ignores PYTHON* variables (including PYTHONDONTWRITEBYTECODE), so -B is
    # passed explicitly: the check must not write bytecode into the checkout.
    out = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    loaded = set(filter(None, out.stdout.strip().split(",")))
    forbidden = {"subprocess", "socket", "ssl", "http", "urllib", "multiprocessing", "ctypes",
                 "runpy", "threading", "agent", "sandbox", "utils", "ui", "streamlit",
                 "langchain", "langchain_core", "langchain_google_genai", "langgraph",
                 "google", "dotenv", "pandas"}
    assert loaded & forbidden == set(), loaded & forbidden
    assert "v2" in loaded and "config" in loaded


def test_no_v1_module_imports_v2():
    offenders = []
    for folder in ("agent", "sandbox", "utils", "ui"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import) and any(
                            a.name.split(".")[0] == "v2" for a in node.names):
                        offenders.append(path)
                    elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "v2":
                        offenders.append(path)
    assert offenders == []
