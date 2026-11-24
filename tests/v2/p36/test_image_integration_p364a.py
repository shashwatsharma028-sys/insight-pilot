"""
P3.6-4A: trusted container image integration, OFFLINE ONLY.

No image is built and no container runtime (Podman, Docker, Buildah) is invoked. These checks prove
properties of the committed image INPUTS and of a staged copy of the trusted runtime:

 1. the entrypoint is exactly the supervisor, in exec form, consistent across every contract;
 2. the staged /opt/ip tree alone satisfies the import closure, and every staged file is needed;
 3. the build context is exactly the allowlist (decoy secrets, .git, venvs, datasets stay out);
 4. the prelude SHA-256 pins match their generated sources;
 5. importing the runtime or building the image executes no code (static and audit-hook checks);
 6. the trusted code compiles and imports on Python 3.11;
 7. the dependency lock and the runtime-tree digest are reproducible and pinned by labels;
 8. the image configuration matches the P3.6 runtime contracts;
 9. V1, P4.1 evidence and the reviewed runtime sources are unchanged;
10. production stays fail-closed.

Plus the P3.6-4A security review (imports, processes, environment, argv, writable paths).
What a BUILT image or a RUNNING container does is NOT established here (P3.6-4B, VE-2).
"""

import ast
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from tests.v2.golden.reference import ROOT
from tests.v2.p3.test_image_p35a import RECORD, _fake_image_dir, _instructions, inspector, tool
from v2.execution import container_runtime as cr
from v2.execution import image_layout as layout
from v2.execution import podman_argv as pa
from v2.execution.limits import DEFAULT_LIMITS, Limits

IMAGE = os.path.join(ROOT, "v2", "execution", "image")
REFERENCE = os.path.join(ROOT, "docs", "v2", "p3", "p35a_inert_reference")
P36_BASE = "41536829957c27a053775989aa619d10cd114406"      # verified P3.6-3 head (base of P3.6-4A)
EXAM_COMMIT = "fd5783c6202e3ea506a69e607160617a23eed807"


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


CONTAINERFILE = _read(os.path.join(IMAGE, "Containerfile"))
EXPECT = json.loads(_read(os.path.join(IMAGE, "image-expectations.json")))
INSTR = _instructions(CONTAINERFILE)
SOURCES = {dest: os.path.join(ROOT, src) for dest, src in layout.RUNTIME_FILES}
PY_FILES = {dest: path for dest, path in SOURCES.items() if dest.endswith(".py")}
ENTRY_FILES = ("supervisor.py", "runner.py", "selftest.py")


def _stages():
    stages = []
    for kw, arg in INSTR:
        if kw == "FROM":
            stages.append({"from": arg, "instr": []})
        else:
            stages[-1]["instr"].append((kw, arg))
    return stages


def _labels():
    return dict(re.findall(r'([\w.]+)="([^"]*)"', " ".join(arg for kw, arg in INSTR if kw == "LABEL")))


def _module_name(dest):
    name = dest[:-3].replace("/", ".")
    return name[:-len(".__init__")] if name.endswith(".__init__") else name


def _git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=True,
                          timeout=60).stdout


def _stage_tree(tmp_path):
    """The /opt/ip tree as the build would install it: exactly RUNTIME_FILES, read-only."""
    opt = tmp_path / "root" / "opt" / "ip"
    for dest, src in layout.RUNTIME_FILES:
        target = opt / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(layout.read_source(src))
    for d, dirs, files in os.walk(opt, topdown=False):
        for f in files:
            os.chmod(os.path.join(d, f), 0o444)
        os.chmod(d, 0o555)
    return opt


@pytest.fixture
def staged_tree(tmp_path):
    opt = _stage_tree(tmp_path)
    yield opt
    for d, dirs, files in os.walk(opt):
        os.chmod(d, 0o755)


def _run_python(args, cwd, timeout=60):
    return subprocess.run([sys.executable, "-I", "-B", *args], capture_output=True, cwd=cwd, timeout=timeout,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, stdin=subprocess.DEVNULL)


# ── 1 / 8. entrypoint and image configuration match the P3.6 contracts ───────

def test_entrypoint_is_exactly_the_trusted_supervisor_in_exec_form():
    final = _stages()[1]["instr"]
    entry = [json.loads(arg) for kw, arg in final if kw == "ENTRYPOINT"]
    cmd = [json.loads(arg) for kw, arg in final if kw == "CMD"]
    assert entry == [list(layout.ENTRYPOINT)] == [EXPECT["entrypoint"]] == [cr.EXPECTED_ENTRYPOINT]
    assert cmd == [[]] == [EXPECT["cmd"]]
    assert layout.ENTRYPOINT == ("/usr/local/bin/python3", "-I", "-B", "/opt/ip/supervisor.py")
    sys.path.insert(0, os.path.join(ROOT, "v2", "execution", "image", "runtime"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    assert supervisor.PYTHON == layout.ENTRYPOINT[0]
    assert {supervisor.RUNNER, supervisor.SELFTEST} == {layout.OPT_IP + "/runner.py", layout.OPT_IP + "/selftest.py"}
    assert {supervisor.RUNNER[len("/opt/ip/"):], supervisor.SELFTEST[len("/opt/ip/"):], "supervisor.py"} <= set(SOURCES)


def test_labels_pin_stage_lock_runtime_and_base():
    labels = _labels()
    assert sorted(labels) == EXPECT["labels"]
    assert labels["io.insightpilot.stage"] == layout.STAGE_LABEL == cr.EXPECTED_STAGE_LABEL == EXPECT["stage_label"]
    assert labels[layout.RUNTIME_LABEL] == layout.runtime_tree_sha256()
    assert cr.EXPECTED_RUNTIME_LABEL == layout.RUNTIME_LABEL
    assert labels["io.insightpilot.lock.sha256"] == hashlib.sha256(
        _read(os.path.join(IMAGE, "requirements.lock"), "rb")).hexdigest()
    assert labels["org.opencontainers.image.base.digest"] == RECORD["index_digest"]


def test_user_environment_and_declarations_are_unchanged_by_the_integration():
    final = _stages()[1]["instr"]
    kws = [kw for kw, _ in final]
    assert [arg for kw, arg in final if kw == "USER"] == [EXPECT["user"]] == [pa.CONTAINER_USER]
    assert "RUN" not in kws[kws.index("USER"):]
    ours = set(re.findall(r"(\w+)=", " ".join(arg for kw, arg in final if kw == "ENV")))
    base = {e.split("=", 1)[0] for e in json.loads(_read(os.path.join(IMAGE, "base", "config-amd64.json")))
            ["config"]["Env"]}
    assert sorted(ours | base) == EXPECT["env_names_allowed"]
    assert not re.search(r"(?im)^\s*(EXPOSE|VOLUME|ARG|ADD|HEALTHCHECK|ONBUILD|SHELL)\b", CONTAINERFILE)
    assert EXPECT["exposed_ports"] == [] and EXPECT["volumes"] == []
    for s in _stages():
        assert s["from"].split(" AS ")[0] == f"{RECORD['reference']}@{RECORD['index_digest']}"
    body = CONTAINERFILE.split("# Base:")[1]
    for forbidden in ("--mount", "type=secret", "curl", "wget", "apt-get", "--network", "sudo", "chmod 777",
                      "chmod u+s", "setcap", "http://", "ssh", "--chmod"):
        assert forbidden not in body, forbidden
    assert "does NOT implement network isolation, seccomp, cgroup limits" in " ".join(
        CONTAINERFILE.split("FROM ")[0].split())


def test_runtime_paths_match_the_launcher_and_workspace_contracts():
    from v2.execution import workspace
    sys.path.insert(0, os.path.join(ROOT, "v2", "execution", "image", "runtime"))
    try:
        import runner
    finally:
        sys.path.pop(0)
    argv = pa.run_argv("/usr/bin/podman", "sha256:" + "a" * 64, instance_id="b" * 32, run_id="c" * 32,
                       inputs_dir="/var/lib/ip/runs/" + "c" * 32 + "/in")
    assert "type=bind,source=/var/lib/ip/runs/" + "c" * 32 + "/in,destination=/in,readonly=true" in argv
    assert runner.CODE_FILE == "/in/" + workspace.CODE_NAME
    assert os.path.basename(runner.PRELUDES_FILE) == "preludes.json" and "preludes.json" in SOURCES
    assert argv[argv.index("sha256:" + "a" * 64) + 1:argv.index("sha256:" + "a" * 64) + 3] == ["--mode", "run"]


# ── 2. the staged tree alone satisfies the import closure ───────────────────

PLANNED_MODULES = {"v2", "v2.execution", "v2.execution.errors", "v2.execution.isolation", "v2.execution.limits",
                   "v2.execution.results", "v2.execution.frame", "v2.execution.supervisor_helpers", "config",
                   "config.feature_flags"}
_AUDIT = r'''
import json, os, sys
OPT = sys.argv[1]
events = []
PROCESS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
           "os.kill", "os.killpg", "socket.", "ctypes.", "pty.")
def hook(ev, args):
    if ev.startswith(PROCESS):
        events.append(["process", ev])
    elif ev in ("exec", "compile"):
        fn = str(getattr(args[0], "co_filename", None) if ev == "exec" else args[1])
        if fn.startswith("/") or fn.startswith("<frozen "):
            events.append([ev, fn])
        else:                                   # generated source: record which file asked for it
            f = sys._getframe(1)
            while f is not None and f.f_code.co_filename == fn:
                f = f.f_back
            events.append([ev + "-generated", fn, f.f_code.co_filename if f is not None else None])
    elif ev == "open":
        path, mode, flags = str(args[0]), args[1], args[2]
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
            (mode is None and isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
        if writes:
            events.append(["write", path])
        if path == "/in" or path.startswith(("/in/", "/out", "/proc", "/sys")):
            events.append(["io", path])
sys.addaudithook(hook)
sys.path.insert(0, OPT)
import supervisor, runner, selftest
modules = {n: m.__file__ for n, m in sys.modules.items() if (getattr(m, "__file__", None) or "").startswith(OPT)}
preludes = sorted(runner.load_preludes())
rc = None
if len(sys.argv) > 2 and sys.argv[2] == "supervise-no-args":
    rc = supervisor.supervise([], supervisor.RealOS())
print(json.dumps({"events": events, "modules": modules, "preludes": preludes, "rc": rc,
                  "repo_on_path": any(p and os.path.exists(os.path.join(p, "agent")) for p in sys.path)}))
'''


def _audit(staged_tree, *extra):
    script = staged_tree.parent.parent / "audit.py"
    script.write_text(_AUDIT)
    out = _run_python([str(script), str(staged_tree), *extra], cwd=str(staged_tree.parent.parent))
    assert out.returncode == 0, out.stderr.decode()
    return json.loads(out.stdout.decode().strip().splitlines()[-1]), out


def test_the_staged_tree_alone_satisfies_the_import_closure_and_every_file_is_needed(staged_tree):
    report, _ = _audit(staged_tree)
    assert report["repo_on_path"] is False                      # -I: neither the repository nor cwd is importable
    assert set(report["modules"]) == PLANNED_MODULES | {"supervisor", "runner", "selftest"}
    used = {os.path.relpath(f, staged_tree) for f in report["modules"].values()} | {"preludes.json"}
    assert used == set(SOURCES) == set(EXPECT["opt_ip_files"])
    assert {_module_name(d) for d in PY_FILES} - {"supervisor", "runner", "selftest"} == PLANNED_MODULES
    assert report["preludes"] == ["lazy_seaborn", "v1"]          # the staged copy verifies its own pin


def test_the_planned_layout_in_the_p36_2_closure_test_is_the_installed_layout():
    from tests.v2.p36.test_image_runtime_p36 import IMAGE_LAYOUT_MODULES
    assert IMAGE_LAYOUT_MODULES == PLANNED_MODULES


def test_the_inspector_accepts_the_staged_tree_against_the_live_expectations(tmp_path):
    root = tmp_path / "root"
    _stage_tree(tmp_path)
    img = inspector.inspect_image(str(root))
    assert img["opt_ip"] == EXPECT["opt_ip_files"]
    assert img["world_writable_files"] == [] and img["secret_like_files"] == [] and img["dev_dirs"] == []
    for d, _, _ in os.walk(root):
        os.chmod(d, 0o755)


def test_the_inspector_reports_modes_owners_and_hashes_of_the_staged_tree(tmp_path):
    """P3.6-4B: the fields rootless.verify checks on a built image, produced by the same inspector code."""
    root = tmp_path / "root"
    opt = _stage_tree(tmp_path)
    entries = inspector.inspect_image(str(root))["opt_ip_entries"]
    assert set(entries) == {"."} | set(SOURCES) | {"config", "v2", "v2/execution"}
    for dest, path in SOURCES.items():
        e = entries[dest]
        assert (e["type"], e["mode"], e["size"]) == ("file", "0444", os.path.getsize(path))
        assert e["sha256"] == hashlib.sha256(_read(path, "rb")).hexdigest()
    assert {entries[d]["mode"] for d in ("config", "v2", "v2/execution", ".")} == {"0555"}
    uid = os.getuid()
    rules = dict(EXPECT["opt_ip_permissions"])
    rules = {k: (dict(v, uid=uid, gid=os.getgid()) if isinstance(v, dict) else v) for k, v in rules.items()}
    assert inspector.evaluate_opt_ip_permissions(entries, rules) == []
    for d, _, _ in os.walk(root):
        os.chmod(d, 0o755)


# ── 3. the build context is exactly the allowlist ───────────────────────────

def _fake_repo_with_runtime(tmp_path):
    img, cache, inv = _fake_image_dir(tmp_path, {"alpha": b"A" * 64})
    repo = tmp_path / "repo"
    for _, src in layout.RUNTIME_FILES:
        (repo / src).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, src), repo / src)
    decoys = {"data/customers.csv": "id,email\n1,x@example.invalid\n", "config/secrets.json": '{"k": "v"}',
              "v2/execution/bootstrap.py": "raise SystemExit\n", "v2/execution/image/runtime/extra.py": "x = 1\n",
              "v2/execution/__pycache__/frame.cpython-311.pyc": "x", ".env": "GOOGLE_API_KEY=not-a-real-key\n",
              ".git/config": "[core]\n", "venv/pyvenv.cfg": "home = /usr\n", "agent/graph.py": "x = 1\n"}
    for rel, text in decoys.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    return img, cache, inv, str(repo), decoys


def test_staged_context_holds_exactly_the_allowlist_and_no_decoy(tmp_path):
    img, cache, inv, repo, decoys = _fake_repo_with_runtime(tmp_path)
    files, digest = tool.stage("x86_64", cache, str(tmp_path / "ctx"), inv=inv, image_dir=img, source_root=repo)
    expected = sorted(["Containerfile", "requirements.lock", "wheels/alpha-1.0-py3-none-any.whl"]
                      + [f"opt-ip/{d}" for d in SOURCES])
    assert files == expected
    names = {f for _, _, fs in os.walk(tmp_path / "ctx") for f in fs}
    assert not names & {"customers.csv", "secrets.json", "bootstrap.py", "extra.py", ".env", "config",
                        "pyvenv.cfg", "graph.py", "frame.cpython-311.pyc", "inert_entrypoint.py",
                        "dependency-inventory.json", "image-expectations.json"}
    for rel in files:
        if rel.startswith("opt-ip/"):
            assert _read(str(tmp_path / "ctx" / rel), "rb") == _read(SOURCES[rel[len("opt-ip/"):]], "rb")
    again = tool.stage("x86_64", cache, str(tmp_path / "ctx2"), inv=inv, image_dir=img, source_root=repo)
    assert again == (files, digest)                                  # deterministic context
    assert tool.staged_runtime_digest(str(tmp_path / "ctx")) == layout.runtime_tree_sha256() \
        == layout.runtime_tree_sha256(repo) == _labels()[layout.RUNTIME_LABEL]


def test_the_staged_digest_is_computed_from_the_context_not_the_repository(tmp_path):
    """build() compares the label with what will actually be copied: a context altered after staging must differ."""
    img, cache, inv, repo, _ = _fake_repo_with_runtime(tmp_path)
    ctx = tmp_path / "ctx"
    tool.stage("x86_64", cache, str(ctx), inv=inv, image_dir=img, source_root=repo)
    assert tool.staged_runtime_digest(str(ctx)) == layout.runtime_tree_sha256(repo)
    staged = ctx / "opt-ip" / "runner.py"
    staged.write_bytes(staged.read_bytes() + b"\n")
    assert tool.staged_runtime_digest(str(ctx)) != layout.runtime_tree_sha256(repo)
    os.unlink(ctx / "opt-ip" / "v2" / "execution" / "frame.py")
    with pytest.raises(OSError):
        tool.staged_runtime_digest(str(ctx))


def test_every_containerfile_copy_source_is_a_staged_entry():
    copies = [arg.split() for s in _stages() for kw, arg in s["instr"] if kw == "COPY"]
    sources = {c[0] for c in copies if not c[0].startswith("--from=")}
    assert sources == {"requirements.lock", "wheels/", "opt-ip/"}
    assert set(tool.CONTEXT_FILES) == {"Containerfile", "requirements.lock"}
    assert layout.CONTEXT_DIR == "opt-ip"
    assert copies == [["requirements.lock", "/build/requirements.lock"], ["wheels/", "/build/wheels/"],
                      ["opt-ip/", "/stage/opt-ip/"],
                      ["--from=build", "/stage/site-packages/", "/usr/local/lib/python3.11/site-packages/"],
                      ["--from=build", "/stage/opt-ip/", "/opt/ip/"]]


@pytest.mark.parametrize("problem", ["missing", "symlink", "symlink_inside_repo", "escaping_dir", "fifo"])
def test_staging_refuses_a_missing_or_unsafe_runtime_source(tmp_path, problem):
    img, cache, inv, repo, _ = _fake_repo_with_runtime(tmp_path)
    target = os.path.join(repo, "v2", "execution", "frame.py")
    if problem == "missing":
        os.unlink(target)
    elif problem == "symlink":
        real = tmp_path / "elsewhere.py"
        os.replace(target, real)
        os.symlink(real, target)
    elif problem == "symlink_inside_repo":            # another repository file under a reviewed name
        os.unlink(target)
        os.symlink(os.path.join(repo, "v2", "execution", "bootstrap.py"), target)
    elif problem == "escaping_dir":
        outside = tmp_path / "outside"
        shutil.move(os.path.join(repo, "config"), outside)
        os.symlink(outside, os.path.join(repo, "config"))
    else:
        os.unlink(target)
        os.mkfifo(target)
    with pytest.raises(SystemExit, match="runtime file missing or unsafe"):
        tool.stage("x86_64", cache, str(tmp_path / "ctx"), inv=inv, image_dir=img, source_root=repo)


@pytest.mark.parametrize("files", [
    (("../evil.py", "config/__init__.py"),),
    (("/abs.py", "config/__init__.py"),),
    (("a//b.py", "config/__init__.py"),),
    (("x.py", "../outside.py"),),
    (("x.sh", "config/__init__.py"),),
    (("b.py", "config/__init__.py"), ("a.py", "config/__init__.py")),          # unsorted
    (("a.py", "config/__init__.py"), ("a.py", "config/feature_flags.py")),     # duplicate
])
def test_layout_validation_rejects_unsafe_unsorted_or_duplicate_entries(files):
    with pytest.raises(layout.LayoutError):
        layout.check_layout(files)


def test_the_committed_layout_is_valid():
    layout.check_layout()
    assert [d for d, _ in layout.RUNTIME_FILES] == EXPECT["opt_ip_files"]


def test_the_runtime_digest_changes_with_any_byte_name_or_file(tmp_path):
    base = layout.tree_digest([(d, b"x") for d in ("a.py", "b.py")])
    assert layout.tree_digest([(d, b"x") for d in ("b.py", "a.py")]) == base           # order-independent
    assert layout.tree_digest([("a.py", b"x"), ("b.py", b"y")]) != base
    assert layout.tree_digest([("a.py", b"x"), ("c.py", b"x")]) != base
    assert layout.tree_digest([("a.py", b"x")]) != base
    _, _, _, repo, _ = _fake_repo_with_runtime(tmp_path)
    p = os.path.join(repo, "v2", "execution", "frame.py")
    with open(p, "ab") as f:
        f.write(b"\n")
    assert layout.runtime_tree_sha256(repo) != layout.runtime_tree_sha256()


# ── 4. preludes ─────────────────────────────────────────────────────────────

def test_prelude_pins_match_their_generated_sources():
    import importlib.util
    spec = importlib.util.spec_from_file_location("p364a_gen_preludes", os.path.join(ROOT, "scripts", "p3",
                                                                                      "gen_preludes.py"))
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    committed = _read(SOURCES["preludes.json"])
    assert committed == json.dumps(gen.generate(), indent=1, sort_keys=True) + "\n"
    data = json.loads(committed)
    body = json.dumps(data["preludes"], sort_keys=True, ensure_ascii=True).encode()
    assert data["sha256"] == hashlib.sha256(body).hexdigest()
    assert sorted(data["preludes"]) == ["lazy_seaborn", "v1"]


# ── 5. nothing executes on import or at image construction ──────────────────

ALLOWED_IMPORT_TIME_CALLS = {"os.path.join", "os.path.dirname", "os.path.abspath", "re.compile", "sys.path.insert"}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_main_guard(node):
    return isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and \
        _dotted(node.test.left) == "__name__" and \
        [getattr(c, "value", None) for c in node.test.comparators] == ["__main__"]


@pytest.mark.parametrize("name", ENTRY_FILES)
def test_entry_modules_only_define_things_at_import_time(name):
    tree = ast.parse(_read(SOURCES[name]))
    for i, node in enumerate(tree.body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                                                    # module docstring
        if _is_main_guard(node):
            assert node is tree.body[-1]
            continue
        assert isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.FunctionDef, ast.ClassDef, ast.If)), \
            ast.dump(node)[:120]
        if isinstance(node, ast.If):                                     # supervisor: the /opt/ip path guard only
            assert name == "supervisor.py" and ast.unparse(node).strip() == \
                "if HERE not in sys.path:\n    sys.path.insert(0, HERE)"
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            assert node.decorator_list == []
            continue
        calls = {_dotted(c.func) for c in ast.walk(node) if isinstance(c, ast.Call)}
        assert calls <= ALLOWED_IMPORT_TIME_CALLS, calls


def test_importing_the_staged_runtime_executes_no_foreign_code_and_touches_nothing(staged_tree):
    report, _ = _audit(staged_tree)
    roots = (str(staged_tree), os.path.realpath(sys.base_prefix), os.path.realpath(sys.prefix))
    stdlib = os.path.dirname(os.__file__)
    generators = {os.path.join(stdlib, "dataclasses.py"), os.path.join(stdlib, "collections", "__init__.py")}
    kinds = set()
    for kind, value, *caller in report["events"]:
        kinds.add(kind)
        if kind.endswith("-generated"):
            # only the standard library's own code generators (dataclass methods, namedtuple)
            assert value == "<string>" and caller[0] in generators, (kind, value, caller)
            continue
        assert kind in ("exec", "compile"), (kind, value)              # no process, network, write or /in access
        assert value.startswith("<frozen ") or (os.path.isabs(value) and os.path.realpath(value).startswith(roots)
                                                and os.path.isfile(value)), (kind, value)
    assert kinds <= {"exec", "compile", "exec-generated", "compile-generated"}


def test_the_staged_entrypoint_refuses_without_arguments_and_starts_nothing(staged_tree):
    report, _ = _audit(staged_tree, "supervise-no-args")
    assert report["rc"] == EXPECT["no_args_exit_status"] == 78
    assert [e for e in report["events"] if e[0] not in ("exec", "compile", "exec-generated", "compile-generated")] \
        == []
    out = _run_python([str(staged_tree / "supervisor.py")], cwd=str(staged_tree.parent))
    assert (out.returncode, out.stdout) == (78, b"")                   # no frame
    assert out.stderr.decode() == EXPECT["no_args_message"] + "\n"


@pytest.mark.parametrize("argv", [["-c", "print('owned')"], ["/in/code.py"], ["--mode", "run"],
                                  ["--mode", "run", "--run-id", "x;id"]])
def test_the_staged_entrypoint_refuses_any_argv_outside_the_grammar(staged_tree, argv):
    out = _run_python([str(staged_tree / "supervisor.py"), *argv], cwd=str(staged_tree.parent))
    assert out.returncode == 78 and out.stdout == b"" and b"owned" not in out.stderr


@pytest.mark.parametrize("argv", [[], ["--prelude"], ["--prelude", "evil"], ["-c", "print('owned')"]])
def test_the_staged_runner_refuses_bad_arguments_without_reading_code(staged_tree, argv):
    out = _run_python([str(staged_tree / "runner.py"), *argv], cwd=str(staged_tree.parent))
    assert out.returncode == 78 and out.stdout == b"" and b"owned" not in out.stderr


def test_image_construction_runs_no_runtime_file():
    runs = [arg for s in _stages() for kw, arg in s["instr"] if kw == "RUN"]
    build_runs = [" ".join(arg.split()) for kw, arg in _stages()[0]["instr"] if kw == "RUN"]
    assert build_runs[1] == ("set -eu; find /stage/opt-ip -type d -exec chmod 0555 {} +; "
                             "find /stage/opt-ip -type f -exec chmod 0444 {} +")
    for run in runs:
        assert "/opt/ip/" not in run and "supervisor" not in run and "runner" not in run and "selftest" not in run
        pythons = re.findall(r"python3?\s+-I\s+-m\s+(\S+)", run)
        assert set(pythons) <= {"pip", "compileall"}, pythons
        assert "python" not in re.sub(r"python3?\s+-I\s+-m\s+(pip|compileall)", "", run).replace(
            "/usr/local/lib/python3.11", "").replace("python-config", "").replace("python3-config", "") \
            .replace("python3.11-config", ""), run
    compiled = re.findall(r"compileall .* (/\S+)$", build_runs[0])
    assert compiled == ["/stage/site-packages"]                         # the runtime tree is never compiled


# ── 6. Python 3.11 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("dest", sorted(PY_FILES))
def test_trusted_code_parses_and_compiles_for_python_3_11(dest):
    assert sys.version_info[:2] == (3, 11) and RECORD["python_version"].startswith("3.11.")
    src = _read(PY_FILES[dest])
    ast.parse(src, filename=dest, feature_version=(3, 11))
    compile(src, dest, "exec", dont_inherit=True)


# ── 7. reproducible dependency manifest ─────────────────────────────────────

def test_dependency_lock_renders_reproducibly_and_pillow_backs_the_only_third_party_import():
    inv = json.loads(_read(os.path.join(IMAGE, "dependency-inventory.json")))
    assert tool.render_lock(inv) == _read(os.path.join(IMAGE, "requirements.lock"))
    assert tool.render_inventory(inv) == _read(os.path.join(IMAGE, "dependency-inventory.json"))
    third_party = {r for r in _imports_by_file().values() for r in r} - set(sys.stdlib_module_names) - {"v2", "config"}
    assert third_party == {"PIL"} and inv["packages"]["pillow"]["version"] == "13.0.0"


# ── 9. V1, P4.1 evidence and the reviewed runtime sources are unchanged ─────

APPROVED_364A = {
    "v2/execution/image_layout.py", "v2/execution/image/Containerfile", "v2/execution/image/image-expectations.json",
    "v2/execution/container_runtime.py", "scripts/p3/image_tool.py", "scripts/p3/image_inspect.py",
    "scripts/p3/rootless.py", "tests/v2/p3/test_image_p35a.py", "tests/v2/p3/test_rootless_p35b.py",
    "tests/v2/p3/test_contract_purity.py", "tests/v2/p51/test_integration_controls_p51a.py",
    "tests/v2/p36/conftest.py", "tests/v2/p36/test_container_runtime_p36.py",
    "tests/v2/p36/test_image_integration_p364a.py",
    "docs/v2/p3/p35a_inert_reference/Containerfile", "docs/v2/p3/p35a_inert_reference/image-expectations.json",
    "docs/v2/p3/p35a_inert_reference/inert_entrypoint.py", "docs/v2/p3/p35a_inert_reference/README.md",
}


# P3.6-4B (approved): real-container validation suite, CI revision and stricter image verification
APPROVED_364B = {
    ".github/workflows/p3-ubuntu-rootless-probe.yml", "scripts/p3/ci_report.py",
    "tests/container/__init__.py", "tests/container/p364b/__init__.py",
    "tests/container/p364b/container_p364b.py", "tests/container/p364b/fixtures_p364b.py",
    "tests/v2/p36/test_container_suite_static_p364b.py",
    # sanitized CI evidence of runs #5/#6 and the wrapper that applies the reviewed P3.6-0 rules
    "docs/v2/p3/ci_p364b/sanitize_p364b.py", "docs/v2/p3/ci_p364b/run5_job_137515602649.sanitized.log",
    "docs/v2/p3/ci_p364b/run6_job_137517791540.sanitized.log", "docs/v2/p3/ci_p364b/run7_job_137529873779.sanitized.log",
    "docs/v2/p3/ci_p364b/run8_job_138265701550.sanitized.log",
    "docs/v2/p3/ci_p364b/run9_job_138276631114.sanitized.log",
    "docs/v2/p3/ci_p364b/run10_job_138283542551.sanitized.log",
    # D1 (approved): --dns=none removed from the run argv; regression test; fake podman models Podman 4.9.3
    "v2/execution/podman_argv.py", "tests/v2/p36/test_host_components_p36.py", "tests/v2/p36/fake_podman_main.py",
    "tests/v2/p36/test_podman_dns_d1_p364b.py",
    # D3 (approved): nr_inodes removed; RealOS real-time-signal fix; argument-compatibility diagnostic
    "tests/v2/p36/test_podman_tmpfs_d3_p364b.py", "tests/v2/p36/test_realos_smoke_p364b.py",
    "v2/execution/image/runtime/supervisor.py", "scripts/p3/argv_compat.py",
    # D2 (approved, Option A): exact writable-mount set; fake mount tables mirror rootless Podman 4.9.3
    "v2/execution/frame.py", "tests/v2/p36/fake_os.py", "tests/v2/p3/test_supervisor_helpers_p34.py",
    "tests/v2/p36/test_attestation_d2_p364b.py",
}


# P6 (approved): live-LLM integration and benchmarking in trusted-ops mode (no generated code runs)
APPROVED_P6 = {
    "multi_agent/op_engine.py", "multi_agent/live_llm.py", "multi_agent/team.py", "evaluation/live.py",
    "tests/v2/p6/__init__.py", "tests/v2/p6/test_live_p6.py",
    "tests/v2/p5/test_integration_p50.py", "tests/v2/p51/test_integration_controls_p51a.py",
    # P6 optimisation (approved): opt-in selective task recovery in the Supervisor, off by default
    "multi_agent/supervisor.py", "multi_agent/contracts.py", "tests/v2/p6/test_selective_recovery_p6.py",
    # P6 optimisation 2 (approved): bounded transient-provider retries, off by default
    "tests/v2/p6/test_provider_resilience_p6.py",
}


def test_only_approved_p364a_files_changed_outside_new_documentation():
    changed = set(_git("diff", "--name-only", P36_BASE).split())
    code_changes = {c for c in changed if not (c.startswith("docs/") and c.endswith(".md"))}
    # P6 live-benchmark evidence: data files only (no code), only under docs/v2/p6/<evidence folder>/.
    evidence = {c for c in code_changes if c.startswith("docs/v2/p6/") and c.count("/") == 4
                and c.rsplit("/", 1)[1] in ("live_benchmark.json", "runs.jsonl")}
    code_changes -= evidence
    approved = APPROVED_364A | APPROVED_364B | APPROVED_P6
    assert code_changes <= approved, sorted(code_changes - approved)


# P3.6-4B: the ONLY approved runtime changes since P3.6-3. Any other change to an installed file fails.
#   supervisor.py: real-time signal names resolved by number (found by tests/v2/p36/test_realos_smoke_p364b.py);
#   frame.py: D2 (approved) exact writable-mount set for rootless Podman 4.9.3.
APPROVED_RUNTIME_DIFF = {
    "v2/execution/image/runtime/supervisor.py": [
        "-            num = getattr(sig, name)",
        "+            # P3.6-4B: unnamed real-time signals are planned as \"SIGRT<number>\" (supervisor_helpers.signal_plan);",
        "+            # the signal module has no such attribute, so resolve them by number (host smoke test, 2026-11-24).",
        "+            num = int(name[5:]) if name.startswith(\"SIGRT\") and name[5:].isdigit() else getattr(sig, name)",
    ],
    "v2/execution/frame.py": [
        '+# P3.6-4B D2 (approved, Option A): rootless Podman 4.9.3 cannot create device nodes, so it bind-mounts the six',
        '+# standard host character devices into the read-only /dev, and it mounts a container-private devpts and the',
        "+# mqueue filesystem of the container's private IPC namespace. Each appears as an rw mount (CI run 45341414519).",
        '+# The set is EXACT: any additional or missing writable mount still fails closed. Residual risk (carried into',
        '+# P3.7): this runtime check matches mount-point PATHS only, not filesystem types or device numbers; types and',
        '+# device identities are verified by the real-container validation suite, not at run time.',
        '+STORAGE_WRITABLE_MOUNTS = ("/out", "/tmp")',
        '+ROOTLESS_DEV_WRITABLE_MOUNTS = ("/dev/full", "/dev/mqueue", "/dev/null", "/dev/pts", "/dev/random", "/dev/tty",',
        '+                                "/dev/urandom", "/dev/zero")',
        '+',
        '+',
        '-        "net_interfaces": ["lo"], "writable_mounts": ["/out", "/tmp"], "dev_writable": False,',
        '+        "net_interfaces": ["lo"], "dev_writable": False,',
        '+        "writable_mounts": sorted(STORAGE_WRITABLE_MOUNTS + ROOTLESS_DEV_WRITABLE_MOUNTS),',
    ],
}


def test_the_installed_runtime_is_byte_identical_to_the_reviewed_p36_3_sources():
    srcs = [src for _, src in layout.RUNTIME_FILES]
    assert sorted(_git("diff", "--name-only", P36_BASE, "--", *srcs).split()) == sorted(APPROVED_RUNTIME_DIFF)
    for path, approved in APPROVED_RUNTIME_DIFF.items():
        diff = _git("diff", "-U0", P36_BASE, "--", path)
        body = [ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        assert body == approved, (path, body)


def test_v1_application_and_p4_evidence_are_unchanged():
    assert _git("diff", "--name-only", EXAM_COMMIT, "--", "ui", "agent", "sandbox", "utils", "rules", "skills",
                "data", "lessons", "requirements.txt").strip() == ""
    assert _git("diff", "--name-only", P36_BASE, "--", "sandbox_v2", "docs/v2/p4", "docs/v2/p5",
                "requirements-v2.txt", "docs/v2/p3/ci_run4").strip() == ""
    # every existing evaluation file (the P4.1/P5 harness) is unchanged; P6 only ADDS evaluation/live.py
    assert _git("diff", "--name-only", "--diff-filter=MDRT", P36_BASE, "--", "evaluation").strip() == ""
    assert set(_git("diff", "--name-only", "--diff-filter=A", P36_BASE, "--", "evaluation").split()) <= \
        {"evaluation/live.py"}


def test_the_inert_p35a_reference_is_a_byte_copy_of_the_last_inert_definition():
    readme = _read(os.path.join(REFERENCE, "README.md"))
    for name, src in (("Containerfile", "v2/execution/image/Containerfile"),
                      ("image-expectations.json", "v2/execution/image/image-expectations.json"),
                      ("inert_entrypoint.py", "v2/execution/image/inert_entrypoint.py")):
        data = _read(os.path.join(REFERENCE, name), "rb")
        blob = subprocess.run(["git", "-C", ROOT, "show", f"{P36_BASE}:{src}"], capture_output=True, check=True,
                              timeout=60).stdout
        assert data == blob, name
        assert f"`{hashlib.sha256(data).hexdigest()}`" in readme, name
    assert _read(os.path.join(REFERENCE, "inert_entrypoint.py"), "rb") == \
        _read(os.path.join(IMAGE, "inert_entrypoint.py"), "rb")


# ── 10. production stays fail-closed ────────────────────────────────────────

def test_production_still_refuses_hardened_execution_with_the_p36_image_definition():
    from tests.v2.p36.test_container_runtime_p36 import HARD, OPTS
    from v2.execution import bootstrap
    bootstrap._reset_for_tests()
    try:
        for env in ({**HARD}, {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1"},
                    {**HARD, "IP_TEST_REQUIRE_CONTAINER": "1", "IP_FF_ORCHESTRATOR": "v2"}):
            bootstrap._reset_for_tests()
            with pytest.raises(bootstrap.BootstrapRefused, match="no validated container backend"):
                bootstrap.ensure_ready(env=env, options=OPTS)
    finally:
        bootstrap._reset_for_tests()
    referencing = set()
    for rel in _git("ls-files", "*.py").split():
        if rel.startswith(("tests/", "docs/")):
            continue
        data = _read(os.path.join(ROOT, rel), "rb")
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError):                 # deliberately malformed fixtures: plain byte search
            assert b"install_for_validation" not in data, rel
            continue
        if any(isinstance(n, ast.Attribute) and n.attr == "install_for_validation" or
               isinstance(n, ast.Name) and n.id == "install_for_validation" for n in ast.walk(tree)):
            referencing.add(rel)
    assert referencing == set()                       # defined in bootstrap; called by nothing outside tests


# ── security review (Task 4) ────────────────────────────────────────────────

NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib",
                   "asyncio", "xmlrpc", "socketserver", "multiprocessing", "webbrowser", "requests", "selectors"}


def _imports_by_file():
    out = {}
    for dest, path in PY_FILES.items():
        names = set()
        for n in ast.walk(ast.parse(_read(path))):
            if isinstance(n, ast.Import):
                names |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                assert n.level == 0, dest
                names.add(n.module.split(".")[0])
        out[dest] = names
    return out


def test_closure_imports_only_the_stdlib_the_closure_and_pinned_pillow():
    allowed = set(sys.stdlib_module_names) | {"v2", "config", "PIL"}
    for dest, names in _imports_by_file().items():
        assert names <= allowed, (dest, names - allowed)
        assert not names & NETWORK_MODULES, (dest, names & NETWORK_MODULES)
    closure = {_module_name(d) for d in PY_FILES}
    for dest, path in PY_FILES.items():
        for n in ast.walk(ast.parse(_read(path))):
            if isinstance(n, ast.ImportFrom) and n.module.split(".")[0] in ("v2", "config"):
                assert n.module in closure, (dest, n.module)
                for a in n.names:
                    sub = f"{n.module}.{a.name}"
                    if os.path.exists(os.path.join(ROOT, *sub.split(".")) + ".py"):
                        assert sub in closure, (dest, sub)


def test_process_creation_is_confined_to_the_reviewed_call_sites():
    uses = {}
    for dest, path in PY_FILES.items():
        tree = ast.parse(_read(path))
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Module))]:
            for c in ast.walk(fn):
                if isinstance(c, ast.Call):
                    name = _dotted(c.func)
                    if name.startswith(("os.", "self._subprocess.", "subprocess.", "ctypes.")) or name == "exec":
                        uses.setdefault(name, set()).add((dest, getattr(fn, "name", "<module>")))
    flat = {k: {f for f in v if f[1] != "<module>"} for k, v in uses.items()}
    for forbidden in ("os.system", "os.popen", "os.execv", "os.execve", "os.execvp", "os.spawnv", "os.posix_spawn",
                      "os.forkpty", "subprocess.run", "subprocess.call", "subprocess.check_output"):
        assert forbidden not in flat, forbidden
    assert flat["self._subprocess.Popen"] == {("supervisor.py", "spawn")}
    assert flat["os.fork"] == {("selftest.py", "pids")}
    assert flat["os.kill"] == {("supervisor.py", "kill_all")}
    assert flat["exec"] == {("runner.py", "main")}
    assert {f for k, v in flat.items() if k.startswith("ctypes.") for f in v} == {("supervisor.py", "set_not_dumpable")}
    modules_importing = {d for d, names in _imports_by_file().items() if names & {"subprocess", "ctypes"}}
    assert modules_importing == {"supervisor.py"}


def test_the_child_is_started_once_with_fixed_options_and_a_fixed_environment():
    tree = ast.parse(_read(SOURCES["supervisor.py"]))
    popen = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and _dotted(c.func).endswith(".Popen")]
    assert len(popen) == 1
    kw = {k.arg: ast.unparse(k.value) for k in popen[0].keywords}
    assert [ast.unparse(a) for a in popen[0].args] == ["argv"]
    assert kw == {"stdin": "self._subprocess.DEVNULL", "stdout": "self._subprocess.PIPE",
                  "stderr": "self._subprocess.PIPE", "env": "env", "close_fds": "True",
                  "start_new_session": "True", "cwd": "'/tmp'"}
    spawns = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and _dotted(c.func) == "os_api.spawn"]
    assert [ast.unparse(c) for c in spawns] == ["os_api.spawn(child_argv(mode, prelude, limits), CHILD_ENV)"]
    sys.path.insert(0, os.path.join(ROOT, "v2", "execution", "image", "runtime"))
    try:
        import supervisor
    finally:
        sys.path.pop(0)
    assert set(supervisor.CHILD_ENV) == {"MPLBACKEND", "MPLCONFIGDIR", "HOME", "LANG", "TZ", "OPENBLAS_NUM_THREADS",
                                         "OMP_NUM_THREADS", "MKL_NUM_THREADS", "PYTHONDONTWRITEBYTECODE",
                                         "PYTHONNOUSERSITE", "PATH"}
    for name, value in supervisor.CHILD_ENV.items():
        assert not inspector.SECRET_ENV.search(name) and not inspector.PROXY_ENV.search(name), name
        assert re.fullmatch(r"[A-Za-z0-9_./:-]+", value), (name, value)
    assert list(inspect.signature(supervisor.child_argv).parameters) == ["mode", "prelude", "limits"]
    for mode in ("run", "selftest-oom", "selftest-pids"):
        for prelude in supervisor.PRELUDES:
            for lim in (DEFAULT_LIMITS, Limits(memory_bytes=2 ** 31, pids=128)):
                for el in supervisor.child_argv(mode, prelude, lim):
                    assert re.fullmatch(r"[A-Za-z0-9_./=-]+", el), el     # enums and integers only; no code


def test_the_in_image_entry_and_contract_modules_read_no_environment():
    for dest, path in PY_FILES.items():
        tree = ast.parse(_read(path))
        reads = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv",
                                                                                           "putenv", "environb")]
        if dest == "config/feature_flags.py":
            top = [n for stmt in tree.body if not isinstance(stmt, (ast.FunctionDef, ast.ClassDef))
                   for n in ast.walk(stmt) if isinstance(n, ast.Attribute) and n.attr in ("environ", "getenv")]
            assert top == [], "feature flags may read the environment only inside functions (never at import)"
        else:
            assert reads == [], dest


def test_the_child_can_write_only_the_two_bounded_tmpfs_and_never_the_runtime():
    argv = pa.run_argv("/usr/bin/podman", "sha256:" + "a" * 64, instance_id="b" * 32, run_id="c" * 32,
                       inputs_dir="/var/lib/ip/runs/" + "c" * 32 + "/in")
    assert "--read-only" in argv and "--read-only-tmpfs=false" in argv
    tmpfs = [argv[i + 1].split(":", 1)[0] for i, a in enumerate(argv) if a == "--tmpfs"]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert tmpfs == ["/tmp", "/out"] and len(mounts) == 1 and mounts[0].endswith("destination=/in,readonly=true")
    assert not {a for a in argv if a in ("-v", "--volume")}
    for dest in SOURCES:
        assert not (layout.OPT_IP + "/" + dest).startswith(("/tmp/", "/out/", "/in/"))
    rt = EXPECT["runtime"]
    assert "/opt/ip" in rt["must_not_be_writable"]
    protected = {"/opt/ip/supervisor.py", "/opt/ip/runner.py", "/opt/ip/selftest.py", "/opt/ip/preludes.json",
                 "/opt/ip/v2/execution/frame.py"}
    assert protected <= set(rt["files_must_not_open_for_write"]) <= set(inspector.FILE_WRITE_PROBES)


# ── build driver: the runtime label is checked against what was staged ──────

def test_the_build_driver_refuses_when_the_staged_runtime_differs_from_the_label(monkeypatch):
    from tests.v2.p3.test_rootless_p35b import GOOD_FACTS, FakePodman, rl
    monkeypatch.setattr(rl.image_tool, "stage", lambda arch, cache, out: (["Containerfile"], "c" * 64))
    monkeypatch.setattr(rl.image_tool, "staged_runtime_digest", lambda out: "0" * 64)
    fake = FakePodman()
    with pytest.raises(rl.Refused) as info:
        rl.build("/cache", facts=GOOD_FACTS, runner=fake)
    assert any("runtime label" in p for p in info.value.problems)
    assert "build" not in fake.subcommands()                            # refused before podman build
    assert rl.image_tag("x86_64", "f" * 64).startswith("localhost/insightpilot-sandbox:p3.6-")
