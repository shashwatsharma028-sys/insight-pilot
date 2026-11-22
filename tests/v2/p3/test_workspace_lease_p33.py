"""
P3.3: per-run JobWorkspace and InstanceLease.

Temporary roots only (pytest tmp_path); nothing is written to reports/,
lessons/, data/ or the default V2 root. Cross-process lock behaviour uses
short-lived Python subprocesses that only acquire a lease. No generated code
is executed.
"""

import ast
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from v2.execution import lease as lease_mod
from v2.execution.datasets import DatasetAuthorizer, DEMO_DATASETS
from v2.execution.errors import SandboxPolicyError, SandboxUnavailableError
from v2.execution.lease import InstanceLease, inspect_instances, reconcile_stale_instances
from v2.execution.limits import Limits
from v2.execution.results import is_valid_run_id
from v2.execution.workspace import JobWorkspace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REAL_DATA = os.path.join(ROOT, "data")
IS_ROOT = os.geteuid() == 0
NEW_MODULES = ["v2/execution/datasets.py", "v2/execution/workspace.py", "v2/execution/lease.py"]


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "v2root")


@pytest.fixture
def lease(root):
    held = InstanceLease.acquire(root=root)
    yield held
    if held.is_held():
        held.release()


# ── layout and permissions ───────────────────────────────────────────────────

def test_layout_and_permissions(lease, root):
    with JobWorkspace.create(lease) as ws:
        ws.write_code("print(1)\n")
        DatasetAuthorizer().authorize_into(os.path.join(REAL_DATA, "sales_data.csv"), ws)
        instance = os.path.join(root, "instances", lease.instance_id)
        assert _mode(root) == _mode(os.path.join(root, "instances")) == _mode(instance) == 0o700
        assert _mode(os.path.join(instance, "runs")) == _mode(ws.path) == 0o700
        assert _mode(ws.input_dir) == 0o755                  # readable by the future container's mapped user
        for name in ("code.py", "dataset.csv"):
            assert _mode(os.path.join(ws.input_dir, name)) == 0o644
        assert _mode(os.path.join(instance, "lease.lock")) == _mode(os.path.join(instance, "lease.json")) == 0o600
        for path in (root, instance, ws.path, ws.input_dir):
            assert os.lstat(path).st_uid == os.geteuid()
        assert sorted(os.listdir(ws.path)) == ["in"]         # no host-visible output directory


def test_default_root_is_per_user_under_the_temp_dir_and_outside_the_repo():
    import tempfile
    default = lease_mod.default_root()
    assert default == os.path.join(tempfile.gettempdir(), f"insightpilot-v2-{os.geteuid()}")
    assert not os.path.realpath(default).startswith(os.path.realpath(ROOT) + os.sep)


def test_new_modules_read_no_environment_and_are_not_wired_into_production():
    for rel in NEW_MODULES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | \
               {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not {"environ", "getenv", "putenv", "system", "popen", "Popen", "exec", "eval"} & used, rel
    # P5.1A (approved, narrow): the dispatcher may use DatasetAuthorizer.identify, a READ-ONLY
    # identity check, and nothing else from P3.3. Workspace and lease stay unwired everywhere.
    allowed_in = {"v2/execution/dispatch.py": ({"v2.execution.datasets"}, {"DatasetAuthorizer"})}
    for rel in ["v2/launch_app.py", "v2/execution/bootstrap.py", "v2/execution/binding.py",
                "v2/execution/dispatch.py", "v2/execution/runtime.py", "v2/execution/__init__.py"]:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        ok_modules, ok_names = allowed_in.get(rel, (set(), set()))
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules = {n.module for n in imports}
        assert not (modules - ok_modules) & {"v2.execution.datasets", "v2.execution.workspace",
                                             "v2.execution.lease"}, rel
        names = {a.name for n in imports for a in n.names}
        assert not names & {"datasets", "workspace", "lease"}, rel
        for n in imports:
            if n.module in ok_modules:
                assert {a.name for a in n.names} <= ok_names, rel
        if rel in allowed_in:
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            assert "identify" in attrs, rel
            assert not attrs & {"authorize_into", "create_input", "write_code", "acquire", "cleanup"}, rel


def test_execution_request_still_carries_no_dataset_path():
    """P5.1A (approved): the request may carry the dataset IDENTITY, never a host path."""
    import dataclasses
    from v2.execution.runtime import DatasetIdentity, ExecutionRequest
    assert [f.name for f in dataclasses.fields(ExecutionRequest)] == [
        "run_id", "task_id", "code", "limits", "dataset", "prelude"]
    assert [f.name for f in dataclasses.fields(DatasetIdentity)] == ["name", "sha256", "size"]
    for bad in ("/home/user/data/sales_data.csv", "../sales_data.csv", "data/sales_data.csv", "sales.CSV", ""):
        with pytest.raises(ValueError):
            DatasetIdentity(bad, "0" * 64, 1)


# ── run identifiers ──────────────────────────────────────────────────────────

def test_run_ids_are_unpredictable_128_bit_and_unique(lease):
    ids = []
    for _ in range(100):
        with JobWorkspace.create(lease) as ws:
            assert is_valid_run_id(ws.run_id)
            ids.append(ws.run_id)
    assert len(set(ids)) == 100
    assert lease.active_runs() == []


# ── code input ───────────────────────────────────────────────────────────────

def test_code_is_written_verbatim(lease):
    code = 'import pandas as pd\nprint("Ünïcödé", len(df))  # "quotes" \\ backslash\n'
    with JobWorkspace.create(lease) as ws:
        assert ws.write_code(code) == len(code.encode("utf-8"))
        with open(os.path.join(ws.input_dir, "code.py"), "rb") as f:
            assert f.read() == code.encode("utf-8")


def test_oversized_code_is_refused_before_anything_is_written(lease):
    with JobWorkspace.create(lease, limits=Limits(code_bytes=10)) as ws:
        with pytest.raises(SandboxPolicyError, match="limit"):
            ws.write_code("x = 12345678901")
        assert ws.inputs() == []
        assert ws.write_code("x = 1") == 5


def test_non_string_code_is_refused(lease):
    with JobWorkspace.create(lease) as ws:
        with pytest.raises(TypeError):
            ws.write_code(b"print(1)")


# ── traversal and symlink escape ─────────────────────────────────────────────

@pytest.mark.parametrize("name", ["../x", "in/../../x", "/etc/passwd", "code.py/x", "out.txt", "CODE.PY", "",
                                  "dataset.csv\x00"])
def test_only_the_two_fixed_input_names_exist(lease, name):
    with JobWorkspace.create(lease) as ws:
        with pytest.raises(ValueError):
            ws.create_input(name)
        with pytest.raises(ValueError):
            ws.remove_input(name)


def test_inputs_are_created_exclusively(lease):
    with JobWorkspace.create(lease) as ws:
        ws.write_code("a = 1")
        with pytest.raises(FileExistsError):
            ws.write_code("b = 2")
        with open(os.path.join(ws.input_dir, "code.py")) as f:
            assert f.read() == "a = 1"


def test_a_planted_symlink_input_is_never_followed(lease, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("original")
    with JobWorkspace.create(lease) as ws:
        os.symlink(outside, os.path.join(ws.input_dir, "code.py"))
        with pytest.raises(FileExistsError):
            ws.write_code("malicious = True")
    assert outside.read_text() == "original"


def test_cleanup_unlinks_planted_symlinks_without_following_them(lease, tmp_path):
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("keep")
    ws = JobWorkspace.create(lease)
    os.symlink(outside_dir, os.path.join(ws.path, "escape_dir"))
    os.symlink(outside_dir / "keep.txt", os.path.join(ws.input_dir, "dataset.csv"))
    ws.cleanup()
    assert (outside_dir / "keep.txt").read_text() == "keep"
    assert not os.path.lexists(ws.path)


def test_cleanup_of_a_replaced_run_directory_removes_only_the_entry(lease, tmp_path):
    outside_dir = tmp_path / "victim"
    outside_dir.mkdir()
    (outside_dir / "precious.txt").write_text("precious")
    ws = JobWorkspace.create(lease)
    real_run = ws.path
    os.rename(real_run, str(tmp_path / "moved_run"))         # simulate tampering
    os.symlink(outside_dir, real_run)
    with pytest.raises(SandboxUnavailableError, match="replaced"):
        ws.cleanup()
    assert (outside_dir / "precious.txt").read_text() == "precious"
    assert not os.path.lexists(real_run)


def test_unexpected_entries_inside_the_run_are_removed_within_the_run_only(lease, tmp_path):
    sibling = JobWorkspace.create(lease)
    sibling.write_code("sibling = 1")
    ws = JobWorkspace.create(lease)
    os.mkdir(os.path.join(ws.path, "junk"))
    with open(os.path.join(ws.path, "junk", "f"), "w") as f:
        f.write("x")
    ws.cleanup()
    assert not os.path.lexists(ws.path)
    assert sibling.inputs() == ["code.py"]
    sibling.cleanup()


# ── deterministic cleanup ────────────────────────────────────────────────────

def test_cleanup_on_success_rejection_and_exception(lease):
    with JobWorkspace.create(lease) as ws:
        ws.write_code("ok = 1")
        ok_path = ws.path
    assert not os.path.exists(ok_path)

    with pytest.raises(Exception):
        with JobWorkspace.create(lease) as ws:
            rejected_path = ws.path
            DatasetAuthorizer().authorize_into("/tmp/upload.csv", ws)
    assert not os.path.exists(rejected_path)

    with pytest.raises(ZeroDivisionError):
        with JobWorkspace.create(lease) as ws:
            boom_path = ws.path
            ws.write_code("x = 1")
            1 / 0
    assert not os.path.exists(boom_path)
    assert lease.active_runs() == []


def test_cleanup_is_idempotent_and_closes_the_workspace(lease):
    ws = JobWorkspace.create(lease)
    ws.cleanup()
    ws.cleanup()
    with pytest.raises(SandboxUnavailableError):
        ws.write_code("x = 1")


def test_a_workspace_needs_a_held_lease(root):
    held = InstanceLease.acquire(root=root)
    held.release()
    with pytest.raises(SandboxUnavailableError, match="not held"):
        JobWorkspace.create(held)


# ── simultaneous runs ────────────────────────────────────────────────────────

def test_simultaneous_runs_are_isolated(lease):
    results, errors, barrier = {}, [], threading.Barrier(16)
    names = sorted(DEMO_DATASETS)

    def worker(i):
        try:
            barrier.wait()
            with JobWorkspace.create(lease) as ws:
                ws.write_code(f"value = {i}\n")
                dataset = DatasetAuthorizer().authorize_into(os.path.join(REAL_DATA, names[i % 4]), ws)
                with open(os.path.join(ws.input_dir, "code.py")) as f:
                    code = f.read()
                results[i] = (ws.run_id, code, dataset.sha256, ws.inputs())
        except Exception as exc:          # pragma: no cover - reported below
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len({r[0] for r in results.values()}) == 16
    for i, (_, code, digest, inputs) in results.items():
        assert code == f"value = {i}\n"
        assert digest == DEMO_DATASETS[names[i % 4]]
        assert inputs == ["code.py", "dataset.csv"]
    assert lease.active_runs() == []


# ── private root safety ──────────────────────────────────────────────────────

def test_symlinked_root_is_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.symlink(target, tmp_path / "rootlink")
    with pytest.raises(SandboxUnavailableError):
        InstanceLease.acquire(root=str(tmp_path / "rootlink"))


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701])
def test_non_private_root_is_refused(tmp_path, mode):
    r = tmp_path / "shared"
    r.mkdir()
    os.chmod(r, mode)
    with pytest.raises(SandboxUnavailableError, match="private"):
        InstanceLease.acquire(root=str(r))


def test_root_that_is_a_file_is_refused(tmp_path):
    (tmp_path / "afile").write_text("x")
    with pytest.raises(SandboxUnavailableError):
        InstanceLease.acquire(root=str(tmp_path / "afile"))


@pytest.mark.skipif(not IS_ROOT, reason="changing ownership needs root (this Linux test environment is root)")
def test_root_owned_by_another_user_is_refused(tmp_path):
    r = tmp_path / "foreign"
    r.mkdir(mode=0o700)
    os.chown(r, 12345, 12345)
    with pytest.raises(SandboxUnavailableError, match="owned by this user"):
        InstanceLease.acquire(root=str(r))


def test_symlinked_instances_directory_is_refused(tmp_path):
    r = tmp_path / "r"
    r.mkdir(mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.symlink(elsewhere, r / "instances")
    with pytest.raises(SandboxUnavailableError):
        InstanceLease.acquire(root=str(r))
    assert os.listdir(elsewhere) == []


# ── lease metadata and ownership ─────────────────────────────────────────────

def test_lease_metadata_is_minimal_and_has_no_credentials(lease, root, monkeypatch):
    path = os.path.join(root, "instances", lease.instance_id, "lease.json")
    with open(path) as f:
        info = json.load(f)
    assert set(info) == {"schema", "instance_id", "pid", "boot_id", "created_unix"}
    assert info["instance_id"] == lease.instance_id and info["pid"] == os.getpid()
    raw = open(path).read()
    for secretish in ("KEY", "TOKEN", "SECRET", "PASSWORD", os.path.expanduser("~")):
        assert secretish not in raw


def test_two_leases_in_one_process_are_distinct_and_live(root):
    a = InstanceLease.acquire(root=root)
    b = InstanceLease.acquire(root=root)
    try:
        assert a.instance_id != b.instance_id
        states = {r["entry"]: r["state"] for r in inspect_instances(root=root, own_instance_id=a.instance_id)}
        assert states == {a.instance_id: "self", b.instance_id: "live"}
    finally:
        a.release()
        b.release()


def test_releasing_one_instance_never_touches_another_live_instance(root):
    a = InstanceLease.acquire(root=root)
    b = InstanceLease.acquire(root=root)
    ws_b = JobWorkspace.create(b)
    ws_b.write_code("b = 1")
    with JobWorkspace.create(a) as ws_a:
        ws_a.write_code("a = 1")
    assert a.release() == {"released": True, "removed": True, "remaining_runs": [], "error": None}
    assert ws_b.inputs() == ["code.py"]
    assert os.path.isdir(os.path.join(root, "instances", b.instance_id))
    states = {r["entry"]: r["state"] for r in inspect_instances(root=root)}
    assert states == {b.instance_id: "live"}
    ws_b.cleanup()
    b.release()


def test_release_with_active_runs_leaves_everything_in_place(root):
    held = InstanceLease.acquire(root=root)
    ws = JobWorkspace.create(held)
    ws.write_code("x = 1")
    report = held.release()
    assert report == {"released": True, "removed": False, "remaining_runs": [ws.run_id], "error": None}
    assert os.path.isfile(os.path.join(ws.input_dir, "code.py"))
    states = {r["entry"]: r["state"] for r in inspect_instances(root=root)}
    assert states == {held.instance_id: "stale"}      # reported, not deleted


_HOLDER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, {root_repo!r})
    from v2.execution.lease import InstanceLease
    lease = InstanceLease.acquire(root={root!r})
    print(lease.instance_id, flush=True)
    if {exit_now}:
        os._exit(0)          # exit without release: simulates a crash
    time.sleep(60)
""")


def _start_holder(root, exit_now=False):
    code = _HOLDER.format(root_repo=ROOT, root=root, exit_now=exit_now)
    env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
    proc = subprocess.Popen([sys.executable, "-I", "-B", "-c", code], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    instance_id = proc.stdout.readline().strip()
    assert is_valid_run_id(instance_id), proc.stderr.read()
    return proc, instance_id


def test_another_process_lease_is_live_then_stale_after_it_dies(root):
    proc, other = _start_holder(root)
    try:
        start = time.monotonic()
        states = {r["entry"]: r for r in inspect_instances(root=root)}
        assert time.monotonic() - start < 2               # never blocks on the lock
        assert states[other]["state"] == "live"
        assert states[other]["lease"]["pid"] == proc.pid
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait(30)
    states = {r["entry"]: r for r in inspect_instances(root=root)}
    assert states[other]["state"] == "stale"
    assert os.path.isdir(os.path.join(root, "instances", other))    # reported, never deleted


def test_a_crashed_process_leaves_a_stale_but_untouched_lease(root):
    proc, other = _start_holder(root, exit_now=True)
    proc.wait(30)
    report = {r["entry"]: r for r in inspect_instances(root=root)}[other]
    assert report["state"] == "stale" and "exited" in report["reason"]
    assert sorted(os.listdir(os.path.join(root, "instances", other))) == ["lease.json", "lease.lock", "runs"]


def test_pid_reuse_cannot_fake_liveness(root):
    proc, other = _start_holder(root, exit_now=True)
    proc.wait(30)
    info_path = os.path.join(root, "instances", other, "lease.json")
    info = json.load(open(info_path))
    info["pid"] = os.getpid()                          # a live PID, but not the lock holder
    with open(info_path, "w") as f:
        json.dump(info, f)
    report = {r["entry"]: r for r in inspect_instances(root=root)}[other]
    assert report["state"] == "stale"


def test_previous_boot_is_stale(root, monkeypatch):
    proc, other = _start_holder(root, exit_now=True)
    proc.wait(30)
    monkeypatch.setattr(lease_mod, "boot_id", lambda: "a-different-boot")
    report = {r["entry"]: r for r in inspect_instances(root=root)}[other]
    assert report["state"] == "stale" and "previous boot" in report["reason"]


def test_orphans_and_unexpected_entries_are_reported_not_deleted(root):
    held = InstanceLease.acquire(root=root)
    instances = os.path.join(root, "instances")
    try:
        no_lock = "a" * 32
        os.mkdir(os.path.join(instances, no_lock), 0o700)
        wrong_id = "b" * 32
        os.mkdir(os.path.join(instances, wrong_id), 0o700)
        open(os.path.join(instances, wrong_id, "lease.lock"), "w").close()
        with open(os.path.join(instances, wrong_id, "lease.json"), "w") as f:
            json.dump({"schema": 1, "instance_id": "c" * 32}, f)
        starting = "d" * 32
        os.mkdir(os.path.join(instances, starting), 0o700)
        open(os.path.join(instances, starting, "lease.lock"), "w").close()
        os.mkdir(os.path.join(instances, "not-an-instance"), 0o700)
        states = {r["entry"]: r["state"] for r in inspect_instances(root=root, own_instance_id=held.instance_id)}
        assert states == {held.instance_id: "self", no_lock: "orphan", wrong_id: "orphan",
                          starting: "indeterminate", "not-an-instance": "unexpected"}
        for entry in (no_lock, wrong_id, starting, "not-an-instance"):
            assert os.path.isdir(os.path.join(instances, entry))
    finally:
        held.release()


def test_destructive_reconciliation_is_disabled(root):
    with pytest.raises(RuntimeError, match="disabled until separately reviewed"):
        reconcile_stale_instances(root=root)


def test_lease_module_has_no_container_or_deletion_of_other_instances():
    with open(os.path.join(ROOT, "v2", "execution", "lease.py"), encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    calls = {getattr(n.func, "attr", getattr(n.func, "id", None)) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "rmtree" not in calls and "subprocess" not in src and "podman" not in src and "docker" not in src
    # The only deletions are of this instance's own lease files (release via
    # _remove_own_directory, and a failed acquire).
    fn_with_deletes = {fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                       for n in ast.walk(fn) if isinstance(n, ast.Call)
                       and getattr(n.func, "attr", None) in ("unlink", "rmdir", "remove")}
    assert fn_with_deletes == {"_remove_own_directory", "_remove_own_instance_dir"}


def test_inspection_with_no_instances_directory_is_empty(root):
    # A fresh root has no instances/ yet; inspect creates the private root but nothing else.
    assert inspect_instances(root=root) == []
    assert os.listdir(root) == []
