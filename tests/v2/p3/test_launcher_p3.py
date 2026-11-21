"""
P3.2a: the V2 launcher's flags-off compatibility boundary (v2/launch_app.py).

Offline. The scripted LLM from tests/v2/mock_llm.py is installed; no paid or
network calls are possible (tests/v2/conftest.py offline guard).

Covers:
- flag checks (defaults run the app; anything else stops before ui/app.py);
- execution semantics of the runpy approach compared with Streamlit's own
  main-script execution (probe scripts);
- import resolution identical to `streamlit run ui/app.py`;
- full AppTest equivalence between `ui/app.py` and the launcher for demo
  selection, planning, approval, retries, reports, every results page,
  follow-up chat, plan edit, discard and the safe-mode-off (graph) path;
- committed golden snapshot parity through the launcher (never regenerated);
- unchanged execution-function identities; no V2 imports from V1;
- rerun stability.

Golden snapshots are compared with IP_UPDATE_GOLDEN forcibly unset, so this
file can never rewrite them.
"""

import ast
import json
import os
import re
import subprocess
import sys
import textwrap
import warnings

import pytest

from tests.v2.golden import reference as ref
from tests.v2.golden import scenarios as sc
from tests.v2.golden.compare import check_snapshot, mismatches, normalize_run, parse_golden

streamlit_testing = pytest.importorskip("streamlit.testing.v1")
AppTest = streamlit_testing.AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
APP = os.path.join(ROOT, "ui", "app.py")
LAUNCHER = os.path.join(ROOT, "v2", "launch_app.py")
ENTRY_POINTS = {"direct": APP, "launcher": LAUNCHER}
NAV = ["Overview", "Insights", "Data Quality", "Agent Activity",
       "Execution Log", "Follow-up Chat", "Downloads"]
SESSION_KEYS = ["agent_state", "analysis_complete", "awaiting_approval", "uploaded_path",
                "dataset_name", "chat_input", "run_count", "theme", "nav", "confirm_reset"]
TIMEOUT = 240


@pytest.fixture(autouse=True)
def _never_regenerate_golden_and_restore_sys_path(monkeypatch):
    monkeypatch.delenv("IP_UPDATE_GOLDEN", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))   # launcher/app edits are undone


@pytest.fixture
def launcher_module():
    import v2.launch_app as launcher
    return launcher


# ─────────────────────────────────────────────────────────────────────────────
# 1. Flag checks (pure)
# ─────────────────────────────────────────────────────────────────────────────

def test_default_flags_are_accepted(launcher_module):
    from config.feature_flags import FeatureFlags
    assert launcher_module.check_flags({}) == FeatureFlags()


def test_explicit_default_values_are_accepted(launcher_module):
    env = {"IP_DEPLOYMENT_PROFILE": "exam", "IP_FF_SANDBOX_HARDENED": "0",
           "IP_SANDBOX_BACKEND": "subprocess", "IP_FF_TRACING": "off",
           "IP_FF_GUARDRAILS": "off", "IP_LIVE_LLM_BUDGET_USD": "0",
           "PATH": "/usr/bin", "IPYTHONDIR": "/x"}
    assert launcher_module.check_flags(env).is_v1_default


UNSUPPORTED = {
    "hardened_container": ({"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"},
                           ["IP_FF_SANDBOX_HARDENED", "IP_SANDBOX_BACKEND"]),
    "secure_profile": ({"IP_DEPLOYMENT_PROFILE": "secure", "IP_FF_SANDBOX_HARDENED": "1",
                        "IP_SANDBOX_BACKEND": "container", "IP_FF_GUARDRAILS": "enforce"},
                       ["IP_DEPLOYMENT_PROFILE", "IP_FF_GUARDRAILS", "IP_FF_SANDBOX_HARDENED",
                        "IP_SANDBOX_BACKEND"]),
    "guardrails_monitor": ({"IP_FF_GUARDRAILS": "monitor"}, ["IP_FF_GUARDRAILS"]),
    "tracing": ({"IP_FF_TRACING": "1"}, ["IP_FF_TRACING"]),
    "verifier": ({"IP_FF_VERIFIER": "shadow"}, ["IP_FF_VERIFIER"]),
    "hitl_v2": ({"IP_FF_HITL_V2": "1"}, ["IP_FF_HITL_V2"]),
    "orchestrator": ({"IP_FF_ORCHESTRATOR": "v2"}, ["IP_FF_ORCHESTRATOR"]),
    "live_budget": ({"IP_LIVE_LLM_BUDGET_USD": "5"}, ["IP_LIVE_LLM_BUDGET_USD"]),
}


@pytest.mark.parametrize("case", sorted(UNSUPPORTED))
def test_unsupported_v2_features_are_refused(launcher_module, case):
    env, expected_names = UNSUPPORTED[case]
    with pytest.raises(launcher_module.LaunchRefused) as info:
        launcher_module.check_flags(env)
    message = str(info.value)
    listed = message.split("cannot provide yet: ")[1].split(".")[0].split(", ")
    assert listed == expected_names
    assert "never used as a fallback" in message


INVALID = {
    "hardened_subprocess": {"IP_FF_SANDBOX_HARDENED": "1"},
    "bad_bool": {"IP_FF_TRACING": "maybe"},
    "typo": {"IP_FF_VERIFER": "enforce"},
    "container_without_hardened": {"IP_SANDBOX_BACKEND": "container"},
    "out_of_range": {"IP_FF_MAX_PARALLEL_TASKS": "99"},
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_flags_are_refused_strictly(launcher_module, case):
    with pytest.raises(launcher_module.LaunchRefused, match="Invalid V2 feature-flag configuration"):
        launcher_module.check_flags(INVALID[case])


def test_importing_the_launcher_module_does_not_run_the_app(launcher_module, monkeypatch):
    import runpy
    calls = []
    monkeypatch.setattr(runpy, "run_path", lambda *a, **k: calls.append(a))
    import importlib
    importlib.reload(launcher_module)
    assert calls == []


def test_launcher_source_is_minimal():
    """No rebinding, no processes, no secrets, fixed target path."""
    with open(LAUNCHER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"eval", "exec", "compile", "__import__", "open", "setattr"}
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:   # never assigns attributes of other modules
                assert not isinstance(t, ast.Attribute), ast.unparse(t)
    assert imported == {"os", "runpy", "sys", "config.feature_flags", "streamlit"}
    assert "environ[" not in ast.unparse(tree) and "dotenv" not in ast.unparse(tree)


def test_launcher_targets_exactly_ui_app(launcher_module):
    assert launcher_module.APP_PATH == APP
    assert launcher_module.UI_DIR == os.path.join(ROOT, "ui")
    assert launcher_module.LAUNCHER_DIR == os.path.join(ROOT, "v2")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Import resolution: identical to `streamlit run ui/app.py`
# ─────────────────────────────────────────────────────────────────────────────

def _streamlit_fixed(path_list, main_script):
    """What streamlit.web.bootstrap._fix_sys_path does at server start."""
    return [os.path.dirname(main_script)] + list(path_list)


def _app_own_insert(path_list):
    """What ui/app.py line 19 does on every run."""
    return [ROOT] + list(path_list)


def test_import_resolution_matches_direct_launch(launcher_module):
    from importlib.machinery import PathFinder
    base = [p for p in sys.path if p not in (ROOT, launcher_module.UI_DIR, launcher_module.LAUNCHER_DIR)]

    direct = _app_own_insert(_streamlit_fixed(base, APP))

    via = _streamlit_fixed(base, LAUNCHER)
    launcher_module.prepare_sys_path(via)
    via = _app_own_insert(via)

    def origin(name, path):
        spec = PathFinder.find_spec(name, path)
        return None if spec is None else (spec.origin, tuple(spec.submodule_search_locations or ()))

    for name in ["agent", "sandbox", "utils", "config", "app", "streamlit", "pandas",
                 "execution", "launch_app", "v2", "tests"]:
        assert origin(name, via) == origin(name, direct), name
    assert origin("execution", via) is None and origin("launch_app", via) is None
    # Same directories, same order (duplicates of ROOT are harmless).
    dedup = lambda seq: [p for i, p in enumerate(seq) if p not in seq[:i]]
    assert dedup(via) == dedup(direct)


def test_prepare_sys_path_is_idempotent(launcher_module):
    path = _streamlit_fixed(["/site"], LAUNCHER)
    launcher_module.prepare_sys_path(path)
    first = list(path)
    for _ in range(10):
        launcher_module.prepare_sys_path(path)
    assert path == first == [ROOT, launcher_module.UI_DIR, "/site"]


def test_no_pages_directories_change_navigation():
    # `streamlit run X` auto-discovers X's sibling pages/ folder.
    assert not os.path.exists(os.path.join(ROOT, "ui", "pages"))
    assert not os.path.exists(os.path.join(ROOT, "v2", "pages"))


def _without_magic_import(tree):
    tree.body = [n for n in tree.body
                 if not (isinstance(n, ast.Import)
                         and [a.asname for a in n.names] == ["__streamlitmagic__"])]
    return tree


@pytest.mark.parametrize("script", [APP, LAUNCHER])
def test_streamlit_magic_does_not_change_the_script(script):
    """
    Streamlit applies 'magic' only to the main script, so under the launcher
    ui/app.py runs without it. That is equivalent only while magic rewrites
    nothing except its own unconditional, unused import.
    """
    from streamlit.runtime.scriptrunner import magic
    with open(script, encoding="utf-8") as f:
        src = f.read()
    transformed = magic.add_magic(src, script)
    assert "__streamlitmagic__." not in ast.unparse(transformed)
    assert ast.dump(_without_magic_import(transformed)) == ast.dump(ast.parse(src))


# ─────────────────────────────────────────────────────────────────────────────
# 3. Execution semantics: runpy vs Streamlit's own main-script execution
# ─────────────────────────────────────────────────────────────────────────────

PROBE = '''
import os, sys
import streamlit as st
g = globals()
st.session_state.setdefault("runs", 0)
st.session_state["runs"] += 1
st.session_state["probe"] = {
    "name": __name__,
    "file": __file__,
    "is_sys_main": sys.modules["__main__"].__dict__ is g,
    "argv0": sys.argv[0] if sys.argv else None,
    "cwd": os.getcwd(),
    "has_builtins": "__builtins__" in g,
    "doc": __doc__,
}
st.markdown("probe body")
if st.button("rerun", key="again"):
    st.rerun()
if st.session_state["runs"] >= 3:
    st.stop()
st.markdown("after stop point")
'''


def _probe_paths(tmp_path):
    probe = tmp_path / "probe_app.py"
    probe.write_text(PROBE)
    wrapper = tmp_path / "probe_launcher.py"
    wrapper.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {ROOT!r})
        from v2.launch_app import run_app
        run_app({str(probe)!r})
    """))
    return probe, wrapper


def _probe_runs(script):
    at = AppTest.from_file(str(script), default_timeout=60)
    seen = []
    at.run()
    seen.append((dict(at.session_state["probe"]), [m.value for m in at.markdown]))
    for _ in range(3):
        at.button(key="again").click().run()
        seen.append((dict(at.session_state["probe"]), [m.value for m in at.markdown]))
    assert not at.exception, [e.value for e in at.exception]
    return seen, at.session_state["runs"]


def test_runpy_execution_matches_streamlit_main_script_semantics(tmp_path):
    probe, wrapper = _probe_paths(tmp_path)
    native, native_runs = _probe_runs(probe)
    via, via_runs = _probe_runs(wrapper)
    assert native_runs == via_runs
    for (n_probe, n_md), (v_probe, v_md) in zip(native, via):
        assert v_md == n_md                     # st.stop / st.rerun behave the same
        for key in ("name", "file", "is_sys_main", "cwd", "has_builtins", "doc"):
            assert v_probe[key] == n_probe[key], key
        assert v_probe["name"] == "__main__" and v_probe["file"] == str(probe)
        assert v_probe["is_sys_main"] is True
    # runpy also points sys.argv[0] at the app, as `streamlit run ui/app.py` does.
    assert via[0][0]["argv0"] == str(probe)


def test_sys_main_is_restored_after_the_app_runs(launcher_module, tmp_path):
    probe = tmp_path / "noop.py"
    probe.write_text("x = 1\n")
    before = sys.modules["__main__"]
    argv0 = sys.argv[0]
    launcher_module.run_app(str(probe))
    assert sys.modules["__main__"] is before and sys.argv[0] == argv0


# ─────────────────────────────────────────────────────────────────────────────
# 4. Refusal before ui/app.py executes (AppTest)
# ─────────────────────────────────────────────────────────────────────────────

REFUSAL_CASES = {**{k: v[0] for k, v in UNSUPPORTED.items()}, **INVALID}


@pytest.mark.parametrize("case", sorted(REFUSAL_CASES))
def test_refusal_stops_before_ui_app_executes(case, monkeypatch, use_scenario):
    import runpy
    controller = use_scenario("sales")
    ran = []
    monkeypatch.setattr(runpy, "run_path", lambda *a, **k: ran.append(a))
    for var, value in REFUSAL_CASES[case].items():
        monkeypatch.setenv(var, value)
    at = AppTest.from_file(LAUNCHER, default_timeout=60)
    at.run()
    assert ran == []                                  # ui/app.py never executed
    assert not at.exception
    assert len(at.error) == 1 and at.error[0].value.startswith("InsightPilot did not start.")
    assert len(at.button) == 0 and len(at.markdown) == 0
    assert "uploaded_path" not in at.session_state    # no app state initialised
    assert controller.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. AppTest equivalence: ui/app.py vs launcher
# ─────────────────────────────────────────────────────────────────────────────

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?")
# Report downloads embed wall-clock timestamps (and notebook cell ids are
# random), so their media hashes differ between ANY two runs. Only these three
# URLs are masked; the files themselves are compared in _compare_reports.
# Chart image URLs (content hashes of the PNGs) stay strict.
_REPORT_MEDIA = re.compile(r"^/mock/media/[0-9a-f]+\.(md|pdf|json)$")
_REPORT_DOWNLOADS = {"Markdown report": "md", "PDF report": "pdf", "Jupyter notebook": "json"}
_CLOCK = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")


def _norm_text(value, out_root):
    if isinstance(value, str):
        value = value.replace(str(out_root), "<OUT>")
        value = _TS.sub("<TIMESTAMP>", value)
        return _CLOCK.sub("<CLOCK>", value)
    if isinstance(value, dict):
        return {k: _norm_text(v, out_root) for k, v in value.items() if k not in ("id", "formId")}
    if isinstance(value, list):
        return [_norm_text(v, out_root) for v in value]
    return value


def _tree(node, out_root, masked=None):
    from google.protobuf.json_format import MessageToDict
    kids = getattr(node, "children", None)
    entry = {"type": getattr(node, "type", type(node).__name__)}
    proto = getattr(node, "proto", None)
    if proto is not None and not isinstance(kids, dict):
        data = _norm_text(MessageToDict(proto), out_root)
        url = data.get("url")
        match = _REPORT_MEDIA.match(url) if isinstance(url, str) else None
        if match and _REPORT_DOWNLOADS.get(data.get("label")) == match.group(1):
            data["url"] = f"<REPORT-MEDIA>.{match.group(1)}"
            if masked is not None:
                masked.append(match.group(1))
        entry["proto"] = data
    if isinstance(kids, dict):
        entry["children"] = [_tree(kids[k], out_root, masked) for k in sorted(kids)]
    return entry


def _observe(at, out_root, controller):
    from agent.token_monitor import monitor
    masked = []
    obs = {"tree": _tree(at._tree, out_root, masked),
           "masked_report_media": sorted(masked),
           "exceptions": [e.value for e in at.exception],
           "llm_calls": controller.calls_by_skill()}
    session = {}
    for key in SESSION_KEYS:
        if key not in at.session_state:
            continue
        value = at.session_state[key]
        if key == "agent_state" and value:
            value = normalize_run(value, controller, monitor.summary(),
                                  safe_mode=bool(value.get("safe_mode")))
        session[key] = _norm_text(json.loads(json.dumps(value, default=str)), out_root)
    obs["session"] = session
    return obs


def _continue_session(at, script):
    """Same Streamlit 1.59 AppTest stale-widget workaround as the P0 UI tests."""
    try:
        at.run()
        return at
    except KeyError as e:
        if "$$ID-" not in str(e):
            raise
        warnings.warn("Streamlit AppTest stale-widget defect hit; restarting harness "
                      "with identical session state (see docs/v2/BASELINE.md)")
        fresh = AppTest.from_file(script, default_timeout=TIMEOUT)
        for key in SESSION_KEYS:
            if key in at.session_state:
                fresh.session_state[key] = at.session_state[key]
        fresh.run()
        return fresh


def _button(at, label):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"button {label!r} not rendered; have {[b.label for b in at.button]}"
    return matches[0]


def _labelled(widgets, label):
    matches = [w for w in widgets if w.label == label]
    assert len(matches) == 1, [w.label for w in widgets]
    return matches[0]


def _isolate(monkeypatch, root):
    """Fresh, separate output locations, lesson store and token counters per run."""
    import sandbox.executor
    import agent.nodes.report_generator as report_generator
    from agent.token_monitor import monitor
    (root / "reports" / "charts").mkdir(parents=True)
    (root / "lessons").mkdir()
    monkeypatch.setattr(sandbox.executor, "CHARTS_DIR", str(root / "reports" / "charts"))
    monkeypatch.setattr(report_generator, "REPORTS_DIR", str(root / "reports"))
    monkeypatch.setenv("LESSONS_DIR", str(root / "lessons"))
    monitor.reset()


def _flow_load(at, script):
    at.run()
    yield "load", at


def _flow_to_approval(at, script):
    yield from _flow_load(at, script)
    at.button(key="ds_sales_data.csv").click().run()
    yield "demo_selected", at
    _button(at, "Run autonomous analysis").click().run()
    yield "awaiting_approval", at


def _flow_approve_pages_chat(at, script):
    for step in _flow_to_approval(at, script):
        yield step
    _button(at, "Approve and run").click().run()
    yield "approved_and_run", at
    at = _continue_session(at, script)
    yield "results", at
    for label in NAV:
        at.button(key=f"nav_{label}").click().run()
        yield f"page_{label}", at
    at.button(key="nav_Follow-up Chat").click().run()
    at.text_input(key="chat_full").input("Which region leads?")
    at.button(key="send_full").click().run()
    yield "followup_chat", at


def _flow_plan_edit(at, script):
    for step in _flow_to_approval(at, script):
        yield step
    at.text_area[0].input("Skip the filesystem inventory task")
    _button(at, "Approve and run").click().run()
    yield "edited_and_run", at


def _flow_discard(at, script):
    for step in _flow_to_approval(at, script):
        yield step
    _button(at, "Discard plan").click().run()
    yield "discarded", at


def _flow_safe_mode_off(at, script):
    for step in _flow_load(at, script):
        yield step
    at.button(key="ds_sales_data.csv").click().run()
    # Same retry limit as the committed graph snapshot (make_initial_state: 2).
    _labelled(at.slider, "Max retries per task").set_value(2).run()
    _labelled(at.toggle, "Review plan before running").set_value(False).run()
    yield "safe_mode_off", at
    _button(at, "Run autonomous analysis").click().run()
    yield "graph_run_complete", at
    at = _continue_session(at, script)
    yield "results", at


FLOWS = {
    "approve_pages_chat": _flow_approve_pages_chat,
    "plan_edit": _flow_plan_edit,
    "discard": _flow_discard,
    "safe_mode_off_graph_path": _flow_safe_mode_off,
}


def _run_flow(flow, script, tmp_path, monkeypatch, controller, label):
    """
    One independent app session. The scripted replies depend only on the
    prompt, so one installed controller serves both runs; its call log is
    cleared so each run is observed on its own.
    """
    root = tmp_path / label
    _isolate(monkeypatch, root)
    controller.calls.clear()
    at = AppTest.from_file(script, default_timeout=TIMEOUT)
    observations, last = [], None
    for name, at_now in flow(at, script):
        observations.append((name, _observe(at_now, root, controller)))
        last = at_now
    return observations, last, controller


def _first_difference(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: {type(a).__name__} != {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}: missing on one side"
            d = _first_difference(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _first_difference(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {str(a)[:200]!r} != {str(b)[:200]!r}"


@pytest.mark.parametrize("flow_name", sorted(FLOWS))
def test_launcher_is_equivalent_to_ui_app(flow_name, tmp_path, monkeypatch, use_scenario):
    flow = FLOWS[flow_name]
    controller = use_scenario("sales")
    direct, _, _ = _run_flow(flow, APP, tmp_path, monkeypatch, controller, "direct")
    via, last, _ = _run_flow(flow, LAUNCHER, tmp_path, monkeypatch, controller, "launcher")
    assert [n for n, _ in via] == [n for n, _ in direct]
    for (name, d_obs), (_, v_obs) in zip(direct, via):
        assert not v_obs["exceptions"], (name, v_obs["exceptions"])
        diff = _first_difference(d_obs, v_obs)
        assert diff is None, f"step {name}: {diff}"
    assert not last.error, [e.value for e in last.error]
    if flow_name == "approve_pages_chat":
        downloads = dict(via)["page_Downloads"]
        assert downloads["masked_report_media"] == ["json", "md", "pdf"]
    _compare_reports(tmp_path / "direct" / "reports", tmp_path / "launcher" / "reports",
                     tmp_path / "direct", tmp_path / "launcher")


def _compare_reports(direct_dir, via_dir, direct_root, via_root):
    """The report files offered for download are equivalent between the runs."""
    names = sorted(p.name for p in direct_dir.iterdir() if p.is_file())
    assert names == sorted(p.name for p in via_dir.iterdir() if p.is_file())
    for name in names:
        d, v = direct_dir / name, via_dir / name
        if name.endswith(".md"):
            assert _norm_text(d.read_text(), direct_root) == _norm_text(v.read_text(), via_root), name
        elif name.endswith(".ipynb"):
            def cells(path, root):
                nb = json.loads(path.read_text())
                for cell in nb["cells"]:
                    cell.pop("id", None)          # nbformat assigns random cell ids
                return _norm_text(nb, root)
            assert cells(d, direct_root) == cells(v, via_root), name
        elif name.endswith(".pdf"):
            # Compressed content streams carry the timestamp, so bytes cannot be
            # normalised: compare validity and page structure only.
            db, vb = d.read_bytes(), v.read_bytes()
            assert db[:5] == vb[:5] == b"%PDF-", name
            pages = lambda b: len(re.findall(rb"/Type\s*/Page[^s]", b))
            assert pages(db) == pages(vb) > 0, name
        else:
            pytest.fail(f"unexpected report file {name}")
    direct_charts = sorted(p.name for p in (direct_dir / "charts").iterdir())
    assert direct_charts == sorted(p.name for p in (via_dir / "charts").iterdir())
    for name in direct_charts:     # charts are byte-identical
        assert (direct_dir / "charts" / name).read_bytes() == (via_dir / "charts" / name).read_bytes()


def test_launcher_matches_committed_ui_golden_snapshot(tmp_path, monkeypatch, use_scenario):
    """Golden parity: the launcher approve flow reproduces the committed V1 UI snapshot."""
    from agent.token_monitor import monitor
    _, at, controller = _run_flow(_flow_to_approval, LAUNCHER, tmp_path, monkeypatch,
                                  use_scenario("sales"), "golden")
    assert controller.calls_by_skill() == {"planner": 1}       # planning only, before approval
    _button(at, "Approve and run").click().run()
    assert not at.exception and not at.error
    state = at.session_state["agent_state"]
    statuses = {t["id"]: getattr(t["status"], "value", t["status"]) for t in state["analysis_plan"]}
    assert statuses == sc.SALES["expected_status"]
    # Retry behaviour: sales_t1 recovers after one retry; sales_t4 is blocked by
    # the V1 scanner on every attempt and fails after the retry limit.
    plan = {t["id"]: t for t in state["analysis_plan"]}
    assert plan["sales_t1"]["retry_count"] == 1
    assert "SANDBOX SAFETY VIOLATION" in plan["sales_t4"]["error_message"]
    printed = {}
    for t in state["completed_tasks"]:
        printed.update(parse_golden(t.get("execution_result")))
    assert mismatches(printed, ref.load_expected("sales")["answers"]) == {}
    for key in ("final_report_md", "final_report_pdf", "notebook_path"):
        assert state[key] and os.path.exists(state[key]), key
    with open(state["final_report_pdf"], "rb") as f:
        assert f.read(5) == b"%PDF-"
    check_snapshot("sales_ui_approve_safe_mode_on",
                   normalize_run(state, controller, monitor.summary(), safe_mode=True))


def test_safe_mode_off_through_launcher_matches_committed_graph_snapshot(
        tmp_path, monkeypatch, use_scenario):
    """The UI's graph path (Path 1) via the launcher reproduces the committed V1 graph snapshot."""
    from agent.token_monitor import monitor
    _, at, controller = _run_flow(_flow_safe_mode_off, LAUNCHER, tmp_path, monkeypatch,
                                  use_scenario("sales"), "graph")
    state = at.session_state["agent_state"]
    assert at.session_state["analysis_complete"] is True
    check_snapshot("sales_graph_safe_mode_off",
                   normalize_run(state, controller, monitor.summary(), safe_mode=False))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Identities, imports, reruns
# ─────────────────────────────────────────────────────────────────────────────

def test_execution_function_identities_unchanged(tmp_path, monkeypatch, use_scenario):
    import runpy
    import streamlit
    import sandbox.executor
    import agent.nodes.executor
    before = {
        "sandbox.execute_code": sandbox.executor.execute_code,
        "sandbox.execute_code.code": sandbox.executor.execute_code.__code__,
        "node.execute_code": agent.nodes.executor.execute_code,
        "node.executor_node": agent.nodes.executor.executor_node,
        "streamlit.file_uploader": streamlit.file_uploader,
        "runpy.run_path": runpy.run_path,
    }
    assert before["node.execute_code"] is before["sandbox.execute_code"]
    _, at, _ = _run_flow(_flow_to_approval, LAUNCHER, tmp_path, monkeypatch,
                         use_scenario("sales"), "ids")
    _button(at, "Approve and run").click().run()
    assert at.session_state["analysis_complete"] is True      # code really executed
    after = {
        "sandbox.execute_code": sandbox.executor.execute_code,
        "sandbox.execute_code.code": sandbox.executor.execute_code.__code__,
        "node.execute_code": agent.nodes.executor.execute_code,
        "node.executor_node": agent.nodes.executor.executor_node,
        "streamlit.file_uploader": streamlit.file_uploader,
        "runpy.run_path": runpy.run_path,
    }
    assert all(after[k] is before[k] for k in before), {k for k in before if after[k] is not before[k]}
    assert sandbox.executor.execute_code.__module__ == "sandbox.executor"


_FRESH_PROBE = r"""
import json, sys
sys.path.insert(0, {root!r})
from streamlit.testing.v1 import AppTest
at = AppTest.from_file({script!r}, default_timeout=120)
at.run()
mods = sorted(sys.modules)
print(json.dumps({{
    "v2": [m for m in mods if m == "v2" or m.startswith("v2.")],
    "flags": "config.feature_flags" in sys.modules,
    "app_ran": "agent.graph" in sys.modules,
    "errors": [e.value for e in at.error],
    "exceptions": [e.value for e in at.exception],
}}))
"""


def _fresh_process(script, extra_env=None):
    env = {k: v for k, v in os.environ.items()
           if k != "GOOGLE_API_KEY" and not k.startswith(("IP_FF_", "IP_SANDBOX_", "IP_HITL_",
                                                          "IP_DEPLOYMENT_", "IP_LIVE_"))}
    env.update(extra_env or {})
    out = subprocess.run([sys.executable, "-B", "-c", _FRESH_PROBE.format(root=ROOT, script=script)],
                         capture_output=True, text=True, env=env, timeout=240, cwd=ROOT)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_direct_ui_app_imports_no_v2_and_no_flags():
    result = _fresh_process(APP)
    assert result["app_ran"] and not result["errors"] and not result["exceptions"]
    assert result["v2"] == [] and result["flags"] is False


def test_launcher_defaults_run_the_app_and_load_no_v2_execution_modules():
    result = _fresh_process(LAUNCHER)
    assert result["app_ran"] and not result["errors"] and not result["exceptions"]
    assert result["flags"] is True
    assert result["v2"] == []          # the launcher runs as __main__; v2.execution is not imported


def test_refusal_in_a_fresh_process_never_imports_the_app():
    result = _fresh_process(LAUNCHER, {"IP_FF_SANDBOX_HARDENED": "1", "IP_SANDBOX_BACKEND": "container"})
    assert result["app_ran"] is False
    assert result["v2"] == [] and result["exceptions"] == []
    assert len(result["errors"]) == 1 and "IP_FF_SANDBOX_HARDENED" in result["errors"][0]


def test_reruns_are_stable_and_flags_are_checked_every_run(tmp_path, monkeypatch, use_scenario):
    root = tmp_path / "reruns"
    _isolate(monkeypatch, root)
    controller = use_scenario("sales")
    import v2.launch_app as launcher

    at = AppTest.from_file(LAUNCHER, default_timeout=TIMEOUT)
    at.run()
    at.button(key="ds_sales_data.csv").click().run()
    reference_tree = _tree(at._tree, root)
    root_count = sys.path.count(ROOT)
    for i in range(10):
        at.run()
        assert not at.exception and not at.error
        assert _tree(at._tree, root) == reference_tree, f"rerun {i} changed the page"
        assert at.session_state["uploaded_path"].endswith("sales_data.csv")
    # The launcher adds nothing per rerun; ui/app.py itself inserts ROOT once per
    # run (unchanged V1 behaviour), exactly as under `streamlit run ui/app.py`.
    assert sys.path.count(ROOT) == root_count + 10
    assert sys.path.count(launcher.UI_DIR) == 1
    assert launcher.LAUNCHER_DIR not in sys.path
    for label in NAV[:1]:
        at.button(key=f"nav_{label}").click().run()
        assert not at.exception

    # A V2 request appearing mid-session stops the very next rerun (fail closed).
    monkeypatch.setenv("IP_FF_SANDBOX_HARDENED", "1")
    monkeypatch.setenv("IP_SANDBOX_BACKEND", "container")
    at.run()
    assert len(at.error) == 1 and at.error[0].value.startswith("InsightPilot did not start.")
    assert len(at.button) == 0
    assert controller.calls == []
