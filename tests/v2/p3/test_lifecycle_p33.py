"""
P3.3 security corrections: workspace and lease lifecycle.

FIX 1  A run-id collision (or any failure before this call created the run
       directory) never deletes an existing run.
FIX 2  Cleanup is retryable: `cleaned` is set only after deletion is
       verified; once cleanup starts, no input can be written.
FIX 3  Lease release is exception-safe: the flock is dropped and every owned
       descriptor closed on all paths; failed removal is reported, never as
       success; the lease stays inspectable; run creation and release are
       serialised.

Benign fixtures in temporary roots only. Failures are injected by wrapping
os functions for one exact name; nothing else is affected. No generated code
is executed.
"""

import errno
import fcntl
import os
import threading

import pytest

from v2.execution import lease as lease_mod
from v2.execution import workspace as ws_mod
from v2.execution.datasets import DatasetAuthorizer, DEMO_DATASETS
from v2.execution.errors import SandboxUnavailableError
from v2.execution.lease import LEASE_INFO, LEASE_LOCK, RUNS, InstanceLease, inspect_instances
from v2.execution.workspace import JobWorkspace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SALES = os.path.join(ROOT, "data", "sales_data.csv")
SENTINEL = "sentinel = 'run A must survive'\n"


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "v2root")


@pytest.fixture
def lease(root):
    held = InstanceLease.acquire(root=root)
    yield held
    if held._lock_fd is not None:
        held.release()


def _code_bytes(ws):
    with open(os.path.join(ws.input_dir, "code.py"), "rb") as f:
        return f.read()


def _fail_once(monkeypatch, func_name, target, exc):
    """Make os.<func_name> raise `exc` the first time it is called for `target`."""
    real = getattr(os, func_name)
    state = {"armed": True, "hits": 0}

    def wrapper(path, *args, **kwargs):
        if state["armed"] and os.fsdecode(path) == target:
            state["armed"] = False
            state["hits"] += 1
            raise exc
        return real(path, *args, **kwargs)
    monkeypatch.setattr(os, func_name, wrapper)
    return state


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: failed creation removes only what this call created
# ─────────────────────────────────────────────────────────────────────────────

def test_run_id_collision_never_deletes_the_existing_run(lease, monkeypatch):
    a = JobWorkspace.create(lease)
    a.write_code(SENTINEL)
    before_fds = open_fds()
    monkeypatch.setattr(ws_mod, "_new_run_id", lambda: a.run_id)       # B collides with A

    with pytest.raises(SandboxUnavailableError, match="collision; the existing run was left untouched"):
        JobWorkspace.create(lease)

    assert open_fds() == before_fds                                     # B left no descriptor open
    assert os.path.isdir(a.path)
    assert _code_bytes(a) == SENTINEL.encode()
    assert lease.active_runs() == [a.run_id]
    monkeypatch.undo()
    # A is still fully usable, then cleans up normally.
    DatasetAuthorizer().authorize_into(SALES, a)
    assert a.inputs() == ["code.py", "dataset.csv"]
    a.cleanup()
    assert a.cleaned and not os.path.lexists(a.path) and lease.active_runs() == []


def test_mkdir_failure_before_creation_touches_nothing(lease, monkeypatch):
    a = JobWorkspace.create(lease)
    a.write_code(SENTINEL)
    victim_id = "f" * 32
    monkeypatch.setattr(ws_mod, "_new_run_id", lambda: victim_id)
    state = _fail_once(monkeypatch, "mkdir", victim_id, PermissionError(errno.EACCES, "Permission denied"))
    before_fds = open_fds()
    with pytest.raises(SandboxUnavailableError, match="could not be created"):
        JobWorkspace.create(lease)
    assert state["hits"] == 1 and open_fds() == before_fds
    assert lease.active_runs() == [a.run_id] and _code_bytes(a) == SENTINEL.encode()
    a.cleanup()


def test_failure_after_creation_removes_only_its_own_new_run(lease, monkeypatch):
    a = JobWorkspace.create(lease)
    a.write_code(SENTINEL)
    state = _fail_once(monkeypatch, "mkdir", "in", OSError(errno.ENOSPC, "No space left on device"))
    with pytest.raises(SandboxUnavailableError, match="could not be created"):
        JobWorkspace.create(lease)
    assert state["hits"] == 1
    assert lease.active_runs() == [a.run_id]                            # B's own run removed, A kept
    assert _code_bytes(a) == SENTINEL.encode()
    a.cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: retryable cleanup
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_deletion_is_reported_never_marked_clean_and_is_retryable(lease, monkeypatch):
    sibling = JobWorkspace.create(lease)
    sibling.write_code(SENTINEL)
    ws = JobWorkspace.create(lease)
    ws.write_code("x = 1\n")
    DatasetAuthorizer().authorize_into(SALES, ws)
    baseline = open_fds()
    state = _fail_once(monkeypatch, "rmdir", ws.run_id, PermissionError(errno.EACCES, "Permission denied"))

    with pytest.raises(SandboxUnavailableError, match="workspace cleanup failed .*call cleanup\\(\\) again"):
        ws.cleanup()
    assert state["hits"] == 1
    assert ws.cleaned is False and ws.closing is True                   # never considered clean
    assert os.path.isdir(ws.path)
    # Cleanup has started: input can never be written again.
    with pytest.raises(SandboxUnavailableError, match="closed"):
        ws.write_code("y = 2\n")
    with pytest.raises(SandboxUnavailableError, match="closed"):
        ws.create_input("dataset.csv")
    with pytest.raises(SandboxUnavailableError, match="closed"):
        DatasetAuthorizer().authorize_into(SALES, ws)
    ws.remove_input("code.py")                                          # harmless no-op
    assert _code_bytes(sibling) == SENTINEL.encode()                    # another run untouched

    ws.cleanup()                                                        # retry finishes safely
    assert ws.cleaned is True and not os.path.lexists(ws.path)
    ws.cleanup()                                                        # repeated success is harmless
    assert open_fds() == baseline - 3      # input/, run/ and the owned runs/ duplicate are closed
    assert lease.active_runs() == [sibling.run_id]
    assert _code_bytes(sibling) == SENTINEL.encode()
    sibling.cleanup()


def test_context_manager_reports_a_failed_cleanup_and_allows_a_retry(lease, monkeypatch):
    with pytest.raises(SandboxUnavailableError, match="cleanup failed") as info:
        with JobWorkspace.create(lease) as ws:
            ws.write_code("x = 1\n")
            _fail_once(monkeypatch, "rmdir", ws.run_id, OSError(errno.EBUSY, "Device or resource busy"))
            raise ZeroDivisionError("body failed")
    chain, exc = [], info.value
    while exc is not None:
        chain.append(type(exc))
        exc = exc.__context__
    assert chain == [SandboxUnavailableError, OSError, ZeroDivisionError]   # the body's error is kept
    assert ws.cleaned is False
    ws.cleanup()
    assert ws.cleaned is True and lease.active_runs() == []


def test_cleanup_never_resolves_relative_to_the_current_directory(lease, tmp_path, monkeypatch):
    ws = JobWorkspace.create(lease)
    decoy = tmp_path / "cwd"
    decoy.mkdir()
    (decoy / ws.run_id).mkdir()                                         # same name in the CWD
    (decoy / ws.run_id / "keep").write_text("keep")
    monkeypatch.chdir(decoy)
    lease.release()                                                     # lease descriptors closed
    ws.cleanup()                                                        # uses its own runs/ descriptor
    assert ws.cleaned and not os.path.lexists(ws.path)
    assert (decoy / ws.run_id / "keep").read_text() == "keep"
    with pytest.raises(SandboxUnavailableError, match="descriptor"):
        ws_mod._remove_run_dir(None, ws.run_id)


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: exception-safe lease release
# ─────────────────────────────────────────────────────────────────────────────

def _second_description_can_lock(path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_normal_release_closes_every_descriptor(root):
    baseline = open_fds()
    held = InstanceLease.acquire(root=root)
    assert open_fds() == baseline + 5                                   # root, instances, instance, lock, runs
    report = held.release()
    assert report == {"released": True, "removed": True, "remaining_runs": [], "error": None}
    assert open_fds() == baseline
    assert held.release() == {"released": False, "removed": False, "remaining_runs": [], "error": None}


@pytest.mark.parametrize("func,step", [("rmdir", RUNS), ("unlink", LEASE_LOCK),
                                       ("unlink", LEASE_INFO), ("rmdir", "<instance>")])
def test_failed_removal_steps_are_reported_and_release_still_completes(root, monkeypatch, func, step):
    baseline = open_fds()
    held = InstanceLease.acquire(root=root)
    instance_dir = held.instance_path
    target = held.instance_id if step == "<instance>" else step
    state = _fail_once(monkeypatch, func, target, OSError(errno.EIO, "Input/output error"))

    report = held.release()

    assert state["hits"] == 1
    assert report["released"] is True and report["removed"] is False   # never reported as success
    assert report["error"] == "OSError: Input/output error"
    assert open_fds() == baseline                                       # every owned descriptor closed
    assert not held.is_held() and held.runs_fd is None
    with pytest.raises(SandboxUnavailableError, match="not held"):
        JobWorkspace.create(held)
    assert os.path.isdir(instance_dir)                                  # left for inspection
    lock_path = os.path.join(instance_dir, LEASE_LOCK)
    if os.path.exists(lock_path):
        assert _second_description_can_lock(lock_path)                  # the flock was released
    monkeypatch.undo()
    report = {r["entry"]: r for r in inspect_instances(root=root)}[held.instance_id]
    expected = {RUNS: "stale", LEASE_LOCK: "stale", LEASE_INFO: "orphan", "<instance>": "orphan"}[step]
    assert report["state"] == expected
    if step in (RUNS, LEASE_LOCK, LEASE_INFO):                          # metadata kept until the last file
        assert report["lease"]["instance_id"] == held.instance_id


def test_flock_unlock_error_still_closes_everything_and_releases_the_lock(root, monkeypatch):
    baseline = open_fds()
    held = InstanceLease.acquire(root=root)
    other = InstanceLease.acquire(root=root)
    lock_path = os.path.join(held.instance_path, LEASE_LOCK)
    real_flock = fcntl.flock

    def failing_unlock(fd, op):
        if op == fcntl.LOCK_UN:
            raise OSError(errno.EIO, "Input/output error")
        return real_flock(fd, op)
    monkeypatch.setattr(fcntl, "flock", failing_unlock)
    _fail_once(monkeypatch, "rmdir", RUNS, OSError(errno.EIO, "Input/output error"))
    report = held.release()
    monkeypatch.undo()
    assert report["removed"] is False and report["error"]
    assert _second_description_can_lock(lock_path)                      # closing released it
    assert open_fds() == baseline + 5                                   # only `other`'s descriptors remain
    assert {r["entry"]: r["state"] for r in inspect_instances(root=root)}[other.instance_id] == "live"
    other.release()
    assert open_fds() == baseline


def test_a_failed_release_never_touches_another_instance(root, monkeypatch):
    a = InstanceLease.acquire(root=root)
    b = InstanceLease.acquire(root=root)
    ws_b = JobWorkspace.create(b)
    ws_b.write_code(SENTINEL)
    _fail_once(monkeypatch, "unlink", LEASE_INFO, OSError(errno.EIO, "Input/output error"))
    assert a.release()["removed"] is False
    monkeypatch.undo()
    assert _code_bytes(ws_b) == SENTINEL.encode() and b.is_held()
    assert {r["entry"]: r["state"] for r in inspect_instances(root=root, own_instance_id=b.instance_id)} == {
        a.instance_id: "orphan", b.instance_id: "self"}
    ws_b.cleanup()
    assert b.release()["removed"] is True


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: run creation vs release
# ─────────────────────────────────────────────────────────────────────────────

def test_creation_after_release_starts_is_refused(lease):
    lease.release()
    with pytest.raises(SandboxUnavailableError, match="released or releasing"):
        JobWorkspace.create(lease)


def test_release_waits_for_an_in_flight_creation_and_keeps_that_run(lease, monkeypatch):
    in_create, go = threading.Event(), threading.Event()
    real_mkdir = os.mkdir

    def slow_mkdir(path, *args, **kwargs):
        if os.fsdecode(path) not in ("in",) and len(os.fsdecode(path)) == 32:
            in_create.set()
            assert go.wait(10), "test timed out"
        return real_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(os, "mkdir", slow_mkdir)

    created, reports = [], []
    creator = threading.Thread(target=lambda: created.append(JobWorkspace.create(lease)))
    creator.start()
    assert in_create.wait(10)
    releaser = threading.Thread(target=lambda: reports.append(lease.release()))
    releaser.start()
    releaser.join(0.5)
    assert releaser.is_alive(), "release must wait while a run is being created"
    go.set()
    creator.join(10)
    releaser.join(10)
    monkeypatch.undo()

    ws = created[0]
    assert reports == [{"released": True, "removed": False, "remaining_runs": [ws.run_id], "error": None}]
    assert os.path.isdir(ws.path)                                       # the new run was not deleted
    ws.write_code("still usable = True\n")
    ws.cleanup()                                                        # works after the lease is gone
    assert ws.cleaned and not os.path.lexists(ws.path)


def test_concurrent_creation_and_release_stay_consistent(lease):
    created, refused, unexpected = [], [], []
    start = threading.Barrier(5)

    def creator():
        start.wait()
        for _ in range(50):
            try:
                created.append(JobWorkspace.create(lease))
            except SandboxUnavailableError as exc:
                refused.append(str(exc))
            except Exception as exc:          # pragma: no cover - reported below
                unexpected.append(repr(exc))
    threads = [threading.Thread(target=creator) for _ in range(4)]
    for t in threads:
        t.start()
    start.wait()
    report = lease.release()
    for t in threads:
        t.join(30)
    assert unexpected == []
    assert all("released or releasing" in r for r in refused)
    assert sorted(ws.run_id for ws in created) == report["remaining_runs"]  # every created run reported, kept
    for ws in created:
        assert os.path.isdir(ws.path)
        ws.cleanup()
    assert all(ws.cleaned for ws in created)
