"""
P0: V1 security baseline (characterisation, offline, benign payloads only).

Each test asserts the SECURE behaviour and is marked xfail(strict=True)
because the V1 execution path (all V2 flags off) does not provide it. These
tests characterise V1 only:
- they report "xfailed" (known gap, confirmed by a real run) and are expected
  to KEEP doing so after V2 fixes land, because V2 fixes are flag-gated and the
  V1 path stays available unchanged;
- a fix is proven only by SEPARATE tests for the hardened V2 path that pass;
  this file is never edited to show a fix;
- an unexpected XPASS means the V1 path itself changed: strict mode fails the
  run and the reviewer must investigate. Do not weaken, delete or re-mark
  these tests without reviewer approval (see docs/v2/BASELINE.md).

Payloads only print markers, read a temp file the test created, write inside
the test's temp dir or connect to a listener on 127.0.0.1 owned by the test.
No external network, no real secrets (a dummy value is used).
"""

import os
import socket
import threading

import pytest

from tests.v2.conftest import make_initial_state
from tests.v2.golden.reference import DATA_DIR

SALES_CSV = os.path.join(DATA_DIR, "sales_data.csv")
DUMMY_SECRET = "dummy-test-value-not-a-real-key"


def _run(code, csv_path=SALES_CSV, task_id="probe"):
    from sandbox.executor import execute_code
    return execute_code(code=code, csv_path=csv_path, task_id=task_id)


def _gap(ref, phase):
    # raises=AssertionError: the gap only counts as confirmed when the insecure
    # behaviour is observed; any other exception is a real test failure.
    return pytest.mark.xfail(strict=True, raises=AssertionError,
                             reason=f"Known V1 gap {ref}; to be fixed in {phase}")


# ── S2: static scanner is a denylist and can be bypassed ─────────────────────

BYPASSES = {
    "pandas_reexported_os": "import pandas as pd\nx = pd.io.common.os.environ",
    "getattr_builtin_eval": 'f = getattr(__builtins__, "ev" + "al")',
    "dunder_subclasses": "x = ().__class__.__base__.__subclasses__()",
    "pathlib_read": 'import pathlib\nx = pathlib.Path("/").read_text',
    "builtins_module": "import builtins\nx = builtins.open",
}


@_gap("S2", "P3 (with OS/container isolation, not AST alone)")
@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_static_scanner_flags_bypass(name):
    from agent.rules_engine import scan_code_safety
    assert scan_code_safety(BYPASSES[name]) is not None


# ── S1: generated code can read process secrets ─────────────────────────────

@_gap("S1", "P3")
def test_sandbox_cannot_read_parent_secrets(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", DUMMY_SECRET)
    ok, out, _, _ = _run('print("KEY=" + str(pd.io.common.os.environ.get("GOOGLE_API_KEY")))')
    assert ok, out
    assert DUMMY_SECRET not in out


# ── Filesystem: reads and writes outside the dataset / chart directory ───────

@_gap("FS-read", "P3")
def test_sandbox_cannot_read_files_outside_dataset(tmp_path):
    secret = tmp_path / "outside_secret.csv"
    secret.write_text("TOP_SECRET_MARKER\n")
    ok, out, _, _ = _run(f'print(pd.read_csv(r"{secret}", header=None).iloc[0, 0])')
    assert "TOP_SECRET_MARKER" not in out


@_gap("FS-write", "P3")
def test_sandbox_cannot_write_outside_chart_dir(tmp_path):
    target = tmp_path / "outside" / "written.csv"
    target.parent.mkdir()
    _run(f'pd.DataFrame({{"a": [1]}}).to_csv(r"{target}")')
    assert not target.exists()


# ── S4: LLM-controlled task id used in the chart path ────────────────────────

@_gap("S4", "P2/P3")
def test_task_id_cannot_escape_chart_dir(isolated_outputs):
    code = 'plt.plot([1, 2]); plt.savefig(CHART_PATH); plt.close("all")'
    _run(code, task_id="../escaped")
    assert not (isolated_outputs["charts"].parent / "escaped.png").exists()


# ── S3: dataset path interpolated into the unscanned wrapper ─────────────────

@_gap("S3", "P3")
def test_dataset_path_cannot_inject_code():
    crafted = '/tmp/" if 0 else (print("INJECTED_MARKER") or r"' + SALES_CSV + '"))#.csv'
    ok, out, _, _ = _run('print("rows", len(df))', csv_path=crafted)
    assert "INJECTED_MARKER" not in out


# ── Network: sandbox can open connections ────────────────────────────────────

@_gap("NET", "P3")
def test_sandbox_cannot_open_network_connections(monkeypatch):
    # The subprocess inherits os.environ; make sure a configured HTTP proxy
    # cannot intercept the loopback request and hide the result.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    received = threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(15)
    port = server.getsockname()[1]

    def accept():
        try:
            conn, _ = server.accept()
            received.set()
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        _run(f'print(pd.io.common.urlopen("http://127.0.0.1:{port}/").read())')
        t.join(timeout=15)
    finally:
        server.close()
    assert not received.is_set()


# ── Resources: no memory limit ───────────────────────────────────────────────

@_gap("S5", "P3")
def test_sandbox_memory_is_limited():
    ok, out, _, _ = _run('b = bytearray(300 * 1024 * 1024)\nprint("ALLOCATED", len(b))')
    assert "ALLOCATED" not in out


# ── S7: approval is enforced by the UI, not by the graph ─────────────────────

@_gap("S7", "P8")
def test_graph_does_not_generate_code_without_approval(use_scenario):
    from agent.graph import analyst_graph

    use_scenario("sales")
    nodes = []
    for step in analyst_graph.stream(make_initial_state("sales", safe_mode=True)):
        nodes.append(list(step.keys())[0])
        if len(nodes) == 4:
            break
    assert "generate_code" not in nodes


# ── Controls V1 DOES provide (must keep passing) ─────────────────────────────

@pytest.mark.parametrize("code", [
    "import os", "import subprocess", "import socket", "from shutil import rmtree",
    "eval('1')", "exec('x=1')", "open('x')", "__import__('os')",
])
def test_v1_scanner_blocks_direct_dangerous_constructs(code):
    from agent.rules_engine import scan_code_safety
    assert scan_code_safety(code) is not None


def test_v1_timeout_constant_unchanged():
    import sandbox.executor
    assert sandbox.executor.MAX_EXECUTION_TIME == 30
