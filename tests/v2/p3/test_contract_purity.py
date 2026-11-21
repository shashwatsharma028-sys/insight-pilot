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


def test_every_v2_python_file_is_a_reviewed_contract_module():
    """P3.1 scope guard: no executor, launcher or runtime code exists yet."""
    found = sorted(
        os.path.relpath(os.path.join(dirpath, name), ROOT).replace(os.sep, "/")
        for dirpath, _, files in os.walk(os.path.join(ROOT, "v2"))
        for name in files if name.endswith(".py")
    )
    assert found == sorted(CONTRACT_MODULES)


def test_importing_contracts_loads_no_process_network_llm_or_v1_modules():
    code = (
        "import sys; sys.path.insert(0, %r); before = set(sys.modules)\n"
        "import v2.execution\n"
        "roots = {m.split('.')[0] for m in set(sys.modules) - before}\n"
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
