"""
P3.3: demo-only DatasetAuthorizer.

Benign local fixtures only: copies of the demo datasets in temporary
directories (the real data/ directory is only ever read, and its hashes are
checked to be unchanged). No generated code is executed.
"""

import glob
import hashlib
import json
import os
import shutil
import socket
import stat

import pytest

from tests.v2.golden.reference import DATA_DIR
from v2.execution import datasets as ds
from v2.execution.errors import DatasetNotAuthorizedError, SandboxUnavailableError
from v2.execution.lease import InstanceLease
from v2.execution.limits import Limits
from v2.execution.workspace import JobWorkspace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
REAL_DATA = os.path.join(ROOT, "data")
NAMES = sorted(ds.DEMO_DATASETS)
IS_ROOT = os.geteuid() == 0


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture(scope="module", autouse=True)
def real_data_untouched():
    before = {n: _sha(os.path.join(REAL_DATA, n)) for n in os.listdir(REAL_DATA)}
    yield
    assert {n: _sha(os.path.join(REAL_DATA, n)) for n in os.listdir(REAL_DATA)} == before


@pytest.fixture
def lease(tmp_path):
    lease = InstanceLease.acquire(root=str(tmp_path / "v2root"))
    yield lease
    if lease.is_held():
        for run in lease.active_runs():
            pytest.fail(f"run {run} leaked")
        lease.release()


@pytest.fixture
def workspace(lease):
    ws = JobWorkspace.create(lease)
    yield ws
    ws.cleanup()


@pytest.fixture
def demo_dir(tmp_path):
    """A private copy of the four demo datasets (never the real data/)."""
    d = tmp_path / "demo_data"
    d.mkdir(mode=0o755)
    for name in NAMES:
        shutil.copyfile(os.path.join(REAL_DATA, name), d / name)
    os.chmod(d, 0o755)
    return d


def _refused(authorizer, path, workspace, match):
    with pytest.raises(DatasetNotAuthorizedError, match=match):
        authorizer.authorize_into(str(path), workspace)
    assert workspace.inputs() == [], "a partial copy was left behind"


# ── pins ─────────────────────────────────────────────────────────────────────

def test_pins_equal_the_golden_dataset_references():
    golden = {}
    for path in glob.glob(os.path.join(ROOT, "tests", "v2", "golden", "expected", "*.json")):
        with open(path, encoding="utf-8") as f:
            expected = json.load(f)
        golden[expected["file"]] = expected["sha256"]
    assert ds.DEMO_DATASETS == golden
    assert len(golden) == 4


def test_real_demo_files_match_the_pins():
    for name, digest in ds.DEMO_DATASETS.items():
        assert _sha(os.path.join(REAL_DATA, name)) == digest, name
    assert os.path.realpath(DATA_DIR) == os.path.realpath(ds.DEFAULT_DATA_DIR)


@pytest.mark.parametrize("approved", [
    {"../x.csv": "0" * 64}, {"Sales.csv": "0" * 64}, {"a.txt": "0" * 64},
    {"sales_data.csv": "0" * 63}, {"sales_data.csv": "G" * 64}, {"sales_data.csv": None},
])
def test_allowlist_itself_is_validated(approved):
    with pytest.raises(ValueError):
        ds.DatasetAuthorizer(approved=approved)


# ── the four approved datasets ───────────────────────────────────────────────

@pytest.mark.parametrize("name", NAMES)
def test_each_approved_dataset_is_copied_byte_for_byte(name, workspace):
    auth = ds.DatasetAuthorizer()                       # production configuration: the real data/
    result = auth.authorize_into(os.path.join(REAL_DATA, name), workspace)
    assert result.name == name and result.sha256 == ds.DEMO_DATASETS[name]
    assert result.workspace_name == "dataset.csv"
    assert workspace.inputs() == ["dataset.csv"]
    copy = os.path.join(workspace.input_dir, "dataset.csv")
    with open(copy, "rb") as a, open(os.path.join(REAL_DATA, name), "rb") as b:
        assert a.read() == b.read()
    assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o644
    assert result.size == os.path.getsize(copy)
    assert {f.name for f in __import__("dataclasses").fields(result)} == {"name", "sha256", "size",
                                                                          "workspace_name"}


def test_demo_directory_reached_through_a_symlink_is_accepted(tmp_path, workspace):
    link = tmp_path / "linked_data"
    os.symlink(REAL_DATA, link)
    result = ds.DatasetAuthorizer().authorize_into(str(link / "sales_data.csv"), workspace)
    assert result.name == "sales_data.csv"


def test_the_ui_and_test_paths_for_demo_datasets_are_accepted(workspace):
    # ui/app.py builds os.path.join(<root>/data, fname); the golden tests use DATA_DIR.
    auth = ds.DatasetAuthorizer()
    assert auth.check_path(os.path.join(ROOT, "data", "student_data.csv")) == "student_data.csv"
    assert auth.check_path(os.path.join(DATA_DIR, "attendance_data.csv")) == "attendance_data.csv"


# ── wrong content / wrong pin ────────────────────────────────────────────────

def test_modified_content_is_refused(demo_dir, workspace):
    target = demo_dir / "sales_data.csv"
    data = bytearray(target.read_bytes())
    data[100] ^= 1
    target.write_bytes(bytes(data))
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), target, workspace, "does not match the approved SHA-256")


def test_incorrect_pinned_hash_is_refused(demo_dir, workspace):
    auth = ds.DatasetAuthorizer(data_dir=str(demo_dir), approved={**ds.DEMO_DATASETS, "sales_data.csv": "0" * 64})
    _refused(auth, demo_dir / "sales_data.csv", workspace, "does not match the approved SHA-256")


# ── paths: uploads, outside files, traversal, unsafe names ──────────────────

def _bad_paths():
    good = os.path.join(REAL_DATA, "sales_data.csv")
    crafted_v1_s3 = '/tmp/" if 0 else (print("INJECTED_MARKER") or r"' + good + '"))#.csv'
    return {
        "relative": ("data/sales_data.csv", "absolute"),
        "dotdot_relative": ("../data/sales_data.csv", "absolute"),
        "dotdot_absolute": (os.path.join(REAL_DATA, "..", "data", "sales_data.csv"), "not normalised"),
        "dot_component": (os.path.join(REAL_DATA, ".", "sales_data.csv"), "not normalised"),
        "double_slash": (REAL_DATA + "//sales_data.csv", "not normalised"),
        "trailing_slash": (good + "/", "not normalised"),
        "nul": (good + "\x00", "control characters"),
        "newline": (REAL_DATA + "/sales\n_data.csv", "control characters"),
        "escape": (REAL_DATA + "/sales\x1b_data.csv", "control characters"),
        "line_separator": (REAL_DATA + "/sales\u2028data.csv", "control characters"),
        "upload_same_name": ("/tmp/sales_data.csv", "not in the approved"),
        "upload_other_name": ("/tmp/my_upload.csv", "only the four bundled"),
        "external": ("/etc/passwd", "only the four bundled"),
        "case_variant": (os.path.join(REAL_DATA, "Sales_Data.csv"), "only the four bundled"),
        "generator_script": (os.path.join(REAL_DATA, "generate_datasets.py"), "only the four bundled"),
        "v1_s3_crafted_path": (crafted_v1_s3, "only the four bundled"),
        "windows_style": ("C:\\data\\sales_data.csv", "absolute"),
        "empty": ("", "non-empty string"),
    }


@pytest.mark.parametrize("case", sorted(_bad_paths()))
def test_unsafe_or_unapproved_paths_are_refused(case, workspace):
    path, match = _bad_paths()[case]
    _refused(ds.DatasetAuthorizer(), path, workspace, match)


@pytest.mark.parametrize("value", [None, b"/x/sales_data.csv", 42])
def test_non_string_paths_are_refused(value, workspace):
    with pytest.raises(DatasetNotAuthorizedError, match="non-empty string"):
        ds.DatasetAuthorizer().authorize_into(value, workspace)


def test_pathlib_objects_are_refused(workspace):
    from pathlib import Path
    with pytest.raises(DatasetNotAuthorizedError):
        ds.DatasetAuthorizer().authorize_into(Path(REAL_DATA) / "sales_data.csv", workspace)


def test_same_name_copy_outside_the_approved_directory_is_refused(tmp_path, workspace):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    shutil.copyfile(os.path.join(REAL_DATA, "sales_data.csv"), outside / "sales_data.csv")
    _refused(ds.DatasetAuthorizer(), outside / "sales_data.csv", workspace, "not in the approved")


def test_unexpected_file_in_the_approved_directory_is_refused(demo_dir, workspace):
    (demo_dir / "extra.csv").write_text("a,b\n1,2\n")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "extra.csv", workspace, "only the four")


# ── file types and links ─────────────────────────────────────────────────────

def test_final_component_symlink_is_refused(demo_dir, tmp_path, workspace):
    elsewhere = tmp_path / "real_copy.csv"
    shutil.copyfile(demo_dir / "sales_data.csv", elsewhere)
    os.unlink(demo_dir / "sales_data.csv")
    os.symlink(elsewhere, demo_dir / "sales_data.csv")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "cannot be opened safely")


def test_symlink_to_a_device_is_refused(demo_dir, workspace):
    os.unlink(demo_dir / "sales_data.csv")
    os.symlink("/dev/zero", demo_dir / "sales_data.csv")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "cannot be opened safely")


def test_hardlink_is_refused(demo_dir, tmp_path, workspace):
    os.link(demo_dir / "sales_data.csv", tmp_path / "second_name.csv")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "hard links are not allowed")


def test_fifo_is_refused_without_blocking(demo_dir, workspace):
    os.unlink(demo_dir / "sales_data.csv")
    os.mkfifo(demo_dir / "sales_data.csv")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "not a regular file")


def test_directory_is_refused(demo_dir, workspace):
    os.unlink(demo_dir / "sales_data.csv")
    os.mkdir(demo_dir / "sales_data.csv")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "not a regular file")


def test_unix_socket_is_refused(tmp_path, workspace):
    d = tmp_path / "s"           # short path: AF_UNIX path length limit
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(d / "sales_data.csv"))
        _refused(ds.DatasetAuthorizer(data_dir=str(d)), d / "sales_data.csv", workspace, "cannot be opened safely")
    finally:
        sock.close()


@pytest.mark.skipif(not IS_ROOT, reason="creating a device node needs root (this Linux test environment is root)")
def test_character_device_is_refused(demo_dir, workspace):
    os.unlink(demo_dir / "sales_data.csv")
    os.mknod(demo_dir / "sales_data.csv", 0o644 | stat.S_IFCHR, os.makedev(1, 3))
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "not a regular file")


@pytest.mark.skipif(not IS_ROOT, reason="changing file ownership needs root (this Linux test environment is root)")
def test_foreign_owner_is_refused(demo_dir, workspace):
    os.chown(demo_dir / "sales_data.csv", 12345, -1)
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "not owned by this user")


@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_group_or_world_writable_data_directory_is_refused(demo_dir, workspace, mode):
    os.chmod(demo_dir, mode)
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace,
             "group/world-writable")


# ── sizes ────────────────────────────────────────────────────────────────────

def test_oversized_dataset_is_refused_before_copying(workspace):
    auth = ds.DatasetAuthorizer(limits=Limits(dataset_bytes=1000))
    _refused(auth, os.path.join(REAL_DATA, "sales_data.csv"), workspace, "outside 1..1000 bytes")


def test_empty_dataset_is_refused(demo_dir, workspace):
    (demo_dir / "sales_data.csv").write_bytes(b"")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir)), demo_dir / "sales_data.csv", workspace, "outside 1..")


# ── modification during the copy ─────────────────────────────────────────────

@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(ds, "COPY_CHUNK", 4096)


def test_file_growing_during_copy_is_refused(demo_dir, workspace, small_chunks):
    target = demo_dir / "sales_data.csv"

    def grow(copied):
        if copied == 4096:
            with open(target, "ab") as f:
                f.write(b"9,9,9\n")
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir), _after_chunk=grow), target, workspace,
             "changed while it was being copied")


def test_growth_beyond_the_size_limit_during_copy_is_refused(demo_dir, workspace, small_chunks):
    target = demo_dir / "sales_data.csv"
    size = target.stat().st_size

    def grow(copied):
        if copied == 4096:
            with open(target, "ab") as f:
                f.write(b"x" * 10000)
    auth = ds.DatasetAuthorizer(data_dir=str(demo_dir), limits=Limits(dataset_bytes=size + 10), _after_chunk=grow)
    _refused(auth, target, workspace, "grew beyond the size limit")


def test_in_place_rewrite_with_restored_mtime_is_refused(demo_dir, workspace, small_chunks):
    target = demo_dir / "sales_data.csv"
    original = target.stat()

    def rewrite(copied):
        if copied == 4096:
            with open(target, "r+b") as f:     # same size, later region, mtime restored
                f.seek(10000)
                f.write(b"#")
            os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir), _after_chunk=rewrite), target, workspace,
             "changed while it was being copied|does not match")


def test_replacement_by_rename_during_copy_is_refused(demo_dir, tmp_path, workspace, small_chunks):
    target = demo_dir / "sales_data.csv"
    replacement = tmp_path / "evil.csv"
    replacement.write_bytes(b"a,b\n" * 5000)

    def swap(copied):
        if copied == 4096:
            os.replace(replacement, target)
    # The open descriptor keeps reading the original inode, so the swap cannot
    # redirect the copy; unlinking that inode still changes its metadata
    # (link count, ctime), and the strict recheck refuses conservatively.
    _refused(ds.DatasetAuthorizer(data_dir=str(demo_dir), _after_chunk=swap), target, workspace,
             "changed while it was being copied")


# ── failures leave nothing behind ────────────────────────────────────────────

def test_io_error_during_copy_is_an_infrastructure_error_and_cleans_up(demo_dir, workspace, small_chunks):
    def disk_full(copied):
        raise OSError(28, "No space left on device")
    with pytest.raises(SandboxUnavailableError, match="dataset copy failed"):
        ds.DatasetAuthorizer(data_dir=str(demo_dir), _after_chunk=disk_full).authorize_into(
            str(demo_dir / "sales_data.csv"), workspace)
    assert workspace.inputs() == []


def test_a_planted_destination_is_not_followed_or_deleted(tmp_path, workspace):
    outside = tmp_path / "outside_target.txt"
    outside.write_text("must survive")
    os.symlink(outside, os.path.join(workspace.input_dir, "dataset.csv"))
    with pytest.raises(SandboxUnavailableError):
        ds.DatasetAuthorizer().authorize_into(os.path.join(REAL_DATA, "sales_data.csv"), workspace)
    assert outside.read_text() == "must survive"
    assert os.path.islink(os.path.join(workspace.input_dir, "dataset.csv"))   # not ours to delete
    workspace.cleanup()                                                        # removes the link only
    assert outside.read_text() == "must survive"


def test_a_second_copy_into_the_same_workspace_is_refused(workspace):
    auth = ds.DatasetAuthorizer()
    auth.authorize_into(os.path.join(REAL_DATA, "sales_data.csv"), workspace)
    with pytest.raises(SandboxUnavailableError):
        auth.authorize_into(os.path.join(REAL_DATA, "student_data.csv"), workspace)
    assert workspace.inputs() == ["dataset.csv"]
    with open(os.path.join(workspace.input_dir, "dataset.csv"), "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == ds.DEMO_DATASETS["sales_data.csv"]


# ── no interpolation ─────────────────────────────────────────────────────────

def test_dataset_path_never_reaches_the_generated_code(workspace):
    requested = os.path.join(REAL_DATA, "sales_data.csv")
    code = 'print("rows", len(df))\n'
    ds.DatasetAuthorizer().authorize_into(requested, workspace)
    workspace.write_code(code)
    with open(os.path.join(workspace.input_dir, "code.py"), "rb") as f:
        assert f.read() == code.encode()
    assert workspace.inputs() == ["code.py", "dataset.csv"]
    for name in workspace.inputs():
        with open(os.path.join(workspace.input_dir, name), "rb") as f:
            assert requested.encode() not in f.read()
