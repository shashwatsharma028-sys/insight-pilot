"""
P3.6-4B: offline checks of the real-container validation suite and of the workflow that runs it.

Standard library and PyYAML only (also run in the CI readiness step). Nothing here starts a container.
"""

import ast
import hashlib
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SUITE_DIR = os.path.join(ROOT, "tests", "container", "p364b")
SUITE = os.path.join(SUITE_DIR, "container_p364b.py")
FIXTURES = os.path.join(SUITE_DIR, "fixtures_p364b.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "p3-ubuntu-rootless-probe.yml")
FIXTURE_IMPORTS = {"hashlib", "json", "os", "socket", "time"}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _fixtures_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("p364b_fixtures_static", FIXTURES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_functional_fixture_is_pinned_small_and_benign():
    mod = _fixtures_module()
    assert set(mod.FIXTURES) == set(mod.SHA256) == {"observe", "code_error", "sleep_past_wall", "allocate_2_gib",
                                                     "chart_symlink", "chart_not_png"}
    for name, code in mod.FIXTURES.items():
        assert hashlib.sha256(code.encode()).hexdigest() == mod.SHA256[name], name
        assert mod.fixture(name) == code
        tree = ast.parse(code)
        imports = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)], name
        assert imports <= FIXTURE_IMPORTS, (name, imports)
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not calls & {"exec", "eval", "compile", "__import__", "breakpoint"}, name
        assert not attrs & {"system", "popen", "fork", "execv", "execve", "spawn", "kill", "setuid", "chmod",
                            "chown", "mount", "ptrace", "unshare"}, (name, attrs)
    observe = mod.FIXTURES["observe"]
    hosts = set(re.findall(r'_connect\(\("([0-9.]+)"', observe))
    assert hosts == {"192.0.2.1", "127.0.0.1"}            # one RFC 5737 documentation address, one loopback
    assert set(re.findall(r'_lookup\("([^"]+)"\)', observe)) == {"example.com"}   # RFC 2606 reserved name
    for name in ("code_error", "sleep_past_wall", "allocate_2_gib", "chart_symlink", "chart_not_png"):
        assert len(mod.FIXTURES[name]) < 200, name


def test_the_suite_is_never_collected_by_the_offline_suite():
    name = os.path.basename(SUITE)
    assert not re.fullmatch(r"test_.*\.py|.*_test\.py", name)
    for cfg in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
        path = os.path.join(ROOT, cfg)
        if os.path.exists(path):
            assert "python_files" not in _read(path), cfg
    found = [f for _, _, fs in os.walk(SUITE_DIR) for f in fs if re.fullmatch(r"test_.*\.py|.*_test\.py", f)]
    assert found == []


def test_the_suite_fails_and_never_skips_without_the_validation_environment(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest", "-x",
                          SUITE], capture_output=True, text=True, cwd=ROOT, env=env, timeout=300)
    text = out.stdout + out.stderr
    assert out.returncode != 0, text[-1500:]
    assert " skipped" not in text and "SKIPPED" not in text, text[-1500:]


def test_the_suite_reaches_hardened_mode_only_through_install_for_validation_and_calls_no_llm():
    tree = ast.parse(_read(SUITE))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
        {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "install_for_validation" in names
    assert not names & {"install_for_tests", "TestContainerRuntime", "FakePodman", "ChatGoogleGenerativeAI"}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
        {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in imported if m and m.split(".")[0] in ("utils", "langchain_google_genai", "google",
                                                                  "langchain", "requests", "urllib", "http")}
    runtime_ctor = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ContainerRuntime"]
    assert len(runtime_ctor) == 1                                # exactly one real runtime, never a subclass
    assert "evaluation.multi_agent" in imported or "import evaluation.multi_agent as ma" in _read(SUITE)


def _workflow():
    import yaml                                                  # required: never skipped
    return yaml.safe_load(_read(WORKFLOW))


def test_the_content_gate_pins_exactly_this_commits_reviewed_content():
    text = _read(WORKFLOW)
    pinned = re.findall(r'APPROVED="([0-9a-f]{64})"', text)
    assert len(pinned) == 1, "the approved-content digest must be pinned exactly once"
    tree = subprocess.run(["git", "-C", ROOT, "ls-tree", "HEAD"], capture_output=True, check=True, timeout=60).stdout
    kept = b"".join(line + b"\n" for line in tree.splitlines()
                    if not re.search(rb"\t(\.github|docs)$", line))
    assert hashlib.sha256(kept).hexdigest() == pinned[0], \
        "the committed code differs from the content the workflow approves (update the pin in a reviewed commit)"
    assert "git ls-tree HEAD | grep -v -P '\\t(\\.github|docs)$' | sha256sum" in text


def test_phase_f_runs_only_the_suite_with_the_explicit_validation_environment():
    job = _workflow()["jobs"]["probe"]
    steps = [s for s in job["steps"] if s["name"].startswith("Phase F - ") and "container_p364b.py" in s.get("run", "")]
    assert len(steps) == 3
    for s in steps:
        run = s["run"]
        assert "IP_TEST_REQUIRE_CONTAINER=1" in run and "tests/container/p364b/container_p364b.py" in run
        assert '"$RUNNER_TEMP/hostenv/bin/python" -m pytest' in run
        assert "GOOGLE_API_KEY" not in run and "secrets." not in run
    assert [s["name"] for s in job["steps"]].index(
        "Approved-content gate - the pushed code is exactly the reviewed P3.6-4B content (stops the job on failure)") == 1
