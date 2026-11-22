"""
P4.0 unit and edge-case tests for the evaluation package (no pipeline run).
"""

import ast
import copy
import csv
import io
import json
import math
import os
import socket
import struct
import types
import zlib

import pytest

from evaluation import harness, metrics, paired, provenance, reports
from evaluation.isolation import OfflineViolation, Patcher, offline_run

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _src(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── token provenance ─────────────────────────────────────────────────────────

def resp(content="ok", usage=None):
    r = types.SimpleNamespace(content=content)
    if usage is not None:
        r.usage_metadata = usage
    return r


@pytest.mark.parametrize("usage,expected", [
    (None, (provenance.ESTIMATED, None, None, None)),
    ({"output_tokens": 3}, (provenance.ESTIMATED, None, None, None)),                # no input_tokens key
    ({"input_tokens": 10, "output_tokens": 4}, (provenance.REPORTED, 10, 4, None)),
    ({"input_tokens": 10}, (provenance.REPORTED, 10, 0, None)),
    ({"input_tokens": -1, "output_tokens": 4}, (provenance.ESTIMATED, None, None, "invalid_usage_metadata")),
    ({"input_tokens": 1.5, "output_tokens": 4}, (provenance.ESTIMATED, None, None, "invalid_usage_metadata")),
    ({"input_tokens": True, "output_tokens": 4}, (provenance.ESTIMATED, None, None, "invalid_usage_metadata")),
    ({"input_tokens": "10", "output_tokens": 4}, (provenance.ESTIMATED, None, None, "invalid_usage_metadata")),
    ("not a dict", (provenance.ESTIMATED, None, None, None)),
])
def test_usage_metadata_is_classified_strictly(usage, expected):
    assert provenance.classify_usage(resp(usage=usage)) == expected


def test_estimates_reproduce_the_v1_token_monitor_exactly():
    from agent.token_monitor import TokenMonitor, CHARS_PER_TOKEN_ESTIMATE
    assert CHARS_PER_TOKEN_ESTIMATE == provenance.CHARS_PER_TOKEN_ESTIMATE
    args = (["system prompt text", "user prompt é ünïcode " * 37],)
    r = resp("response content " * 11)
    m = TokenMonitor()
    m.record_from_response("planner", r, args)
    rec = provenance.call_record("planner", provenance.MOCK, args, r, 0.01)
    assert (rec["input_tokens"], rec["output_tokens"]) == (m.total_input_tokens, m.total_output_tokens)
    assert rec["token_source"] == provenance.ESTIMATED and m.estimated_calls == 1


def test_token_classes_are_never_summed_and_mock_tokens_never_have_a_cost():
    calls = [provenance.call_record("planner", provenance.MOCK, ("p" * 400,), resp("x" * 40), 0),
             provenance.call_record("planner", provenance.PROVIDER, ("p",), resp(usage={"input_tokens": 100,
                                                                                       "output_tokens": 20}), 0),
             provenance.call_record("code", provenance.PROVIDER, ("p" * 80,), resp("y" * 8), 0)]
    agg = provenance.aggregate(calls, input_price_per_m=0.10, output_price_per_m=0.40)
    assert set(agg["classes"]) == {"mock_scripted/estimated_chars_div_4", "provider/provider_reported",
                                   "provider/estimated_chars_div_4"}
    assert "total_input_tokens" not in agg and agg["total_calls"] == 3
    assert agg["mock_calls"] == 1 and agg["provider_calls"] == 2
    mock = agg["classes"]["mock_scripted/estimated_chars_div_4"]
    assert mock["cost_usd_estimate"] is None and "never cost" in mock["cost_note"]
    rep = agg["classes"]["provider/provider_reported"]
    assert rep["cost_usd_estimate"] == round(100 / 1e6 * 0.10 + 20 / 1e6 * 0.40, 6)
    assert agg["classes"]["provider/estimated_chars_div_4"]["cost_usd_estimate"] is None
    assert provenance.aggregate(calls)["classes"]["provider/provider_reported"]["cost_usd_estimate"] is None


def test_anomalies_are_counted():
    calls = [provenance.call_record("s", provenance.PROVIDER, ("p",), resp(usage={"input_tokens": -5}), 0)]
    assert provenance.aggregate(calls)["classes"]["provider/estimated_chars_div_4"]["anomalies"] == 1


# ── numerical answers and data quality ───────────────────────────────────────

def test_answer_scoring_distinguishes_correct_incorrect_missing_and_unexpected():
    expected = {"a": 1.0, "b": {"x": 2.0, "y": 3.0}, "c": "north", "d": 5, "e": 0.0}
    printed = {"a": 1.0 + 1e-12, "b": {"x": 2.0, "y": 3.1}, "c": "North", "z": 9}
    s = metrics.score_answers(printed, expected)
    assert s["per_answer"] == {"a": "correct", "b": "incorrect", "c": "incorrect", "d": "missing", "e": "missing"}
    assert (s["correct"], s["incorrect"], s["missing"], s["expected"]) == (1, 2, 2, 5)
    assert s["unexpected"] == ["z"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "1.0", None, [1.0], {"a": 1}])
def test_malformed_answer_values_are_incorrect_not_errors(value):
    assert metrics.score_answers({"a": value}, {"a": 1.0})["per_answer"]["a"] == "incorrect"


def test_printed_answers_detect_duplicates_across_tasks():
    state = {"completed_tasks": [{"execution_result": 'GOLDEN a=1\nGOLDEN b=2'},
                                 {"execution_result": 'noise\nGOLDEN a=1'}, {"execution_result": None}]}
    printed, dup = metrics.printed_answers(state)
    assert printed == {"a": 1, "b": 2} and dup == ["a"]


def test_quality_scoring_covers_all_five_values_and_dict_equality():
    expected = {"total_rows": 10, "total_columns": 3, "duplicate_rows": 1, "missing_values": {"x": 2},
                "quality_score": 97.5}
    assert metrics.score_quality(dict(expected), expected)["correct"] == 5
    bad = dict(expected, missing_values={"x": 2, "y": 1}, quality_score=97.6)
    s = metrics.score_quality(bad, expected)
    assert s["correct"] == 3 and s["per_value"]["missing_values"] == "incorrect"
    assert metrics.score_quality({"total_rows": 10}, expected)["per_value"]["quality_score"] == "missing"
    assert metrics.score_quality(None, expected)["correct"] == 0
    assert metrics.score_quality("garbage", expected)["correct"] == 0


# ── tasks ────────────────────────────────────────────────────────────────────

SCEN = {"expected_status": {"t1": "completed", "t2": "failed", "t3": "completed"},
        "expected_retries": {"t1": 1}, "expected_charts": {"t1"}}


def test_task_scoring_and_summary():
    state = {"analysis_plan": [
        {"id": "t1", "title": "A", "status": "completed", "retry_count": 1, "chart_path": "/x.png",
         "interpretation": "i"},
        {"id": "t2", "title": "B", "status": "failed", "retry_count": 2, "error_type": "runtime_error"},
        {"id": "t3", "title": "C", "status": "failed", "retry_count": 2, "error_type": "data_error"}]}
    rows = metrics.score_tasks(state, SCEN)
    s = metrics.task_summary(rows)
    assert s["completed_of_expected"] == 1 and s["expected_completed"] == 2
    assert s["expected_failures_observed"] == 1 and s["status_as_expected"] == 2
    assert s["retries"] == 5 and s["error_types"] == ["data_error", "runtime_error"]
    assert metrics.task_summary([])["planned"] == 0


# ── charts and reports ───────────────────────────────────────────────────────

def _png(w=4, h=3):
    raw = b"".join(b"\x00" + b"\x7f" * (w * 3) for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (metrics.PNG_MAGIC + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_png_checks(tmp_path):
    good = tmp_path / "good.png"
    good.write_bytes(_png())
    c = metrics.check_png(str(good), str(tmp_path))
    assert c["exists"] and c["png_signature"] and c["decodes"] and (c["width"], c["height"]) == (4, 3)
    assert c["path_inside_output_dir"]
    assert not metrics.check_png(str(good), str(tmp_path / "elsewhere"))["path_inside_output_dir"]
    (tmp_path / "fake.png").write_bytes(b"not a png")
    f = metrics.check_png(str(tmp_path / "fake.png"), str(tmp_path))
    assert not f["png_signature"] and not f["decodes"]
    (tmp_path / "trunc.png").write_bytes(_png()[:40])
    assert not metrics.check_png(str(tmp_path / "trunc.png"), str(tmp_path))["decodes"]
    assert not metrics.check_png(None, str(tmp_path))["exists"]
    assert not metrics.check_png(str(tmp_path / "missing.png"), str(tmp_path))["exists"]


def test_chart_validity_requires_expected_valid_and_inside_output_dir(tmp_path):
    good = tmp_path / "t1.png"
    good.write_bytes(_png())
    stray = tmp_path / "t3.png"
    stray.write_bytes(_png())
    state = {"analysis_plan": [{"id": "t1", "chart_path": str(good)}, {"id": "t2", "chart_path": None},
                               {"id": "t3", "chart_path": str(stray)}]}
    v = metrics.chart_validity(state, SCEN, str(tmp_path))
    assert v["valid"] == 1 and v["expected"] == 1 and v["unexpected"] == ["t3"]
    state["analysis_plan"][0]["chart_path"] = None
    assert metrics.chart_validity(state, SCEN, str(tmp_path))["valid"] == 0


def _reports(tmp_path, md=None, pdf=b"%PDF-1.4\n...\n%%EOF\n", nb=None, titles=("Overview",)):
    import nbformat
    md = md if md is not None else "# Report\n## Results\n" + "".join(f"### {i}. {t}\n" for i, t in enumerate(titles, 1))
    (tmp_path / "r.md").write_text(md, encoding="utf-8")
    (tmp_path / "r.pdf").write_bytes(pdf)
    if nb is None:
        node = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(f"x{i}") for i in range(1 + len(titles))])
        nb = nbformat.writes(node)
    (tmp_path / "r.ipynb").write_text(nb, encoding="utf-8")
    return {"final_report_md": str(tmp_path / "r.md"), "final_report_pdf": str(tmp_path / "r.pdf"),
            "notebook_path": str(tmp_path / "r.ipynb"), "completed_tasks": [{"title": t} for t in titles]}


def test_report_validity_accepts_well_formed_outputs(tmp_path):
    v = metrics.report_validity(_reports(tmp_path), str(tmp_path))
    assert v["valid"] == 3 and v["notebook"]["code_cells"] == 2


@pytest.mark.parametrize("kind,kwargs", [
    ("markdown", {"md": "no headings\n"}),
    ("markdown", {"md": "# Report\n### 1. Something else\n"}),
    ("markdown", {"md": "## Not a title\n### 1. Overview\n"}),
    ("pdf", {"pdf": b"%PDF-1.4 truncated"}),
    ("pdf", {"pdf": b"GIF89a %%EOF"}),
    ("notebook", {"nb": "{not json"}),
    ("notebook", {"nb": json.dumps({"cells": [], "nbformat": 4})}),
])
def test_report_validity_rejects_malformed_outputs(tmp_path, kind, kwargs):
    v = metrics.report_validity(_reports(tmp_path, **kwargs), str(tmp_path))
    assert v[kind]["valid"] is False and v["valid"] == 2


def test_reports_outside_the_output_directory_are_invalid(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    v = metrics.report_validity(_reports(tmp_path), str(other))
    assert v["valid"] == 0
    assert metrics.report_validity({}, str(tmp_path))["valid"] == 0


# ── statistics ───────────────────────────────────────────────────────────────

def test_latency_statistics_edge_cases():
    assert metrics.latency_stats([])["n"] == 0
    assert metrics.latency_stats([float("nan"), float("inf"), None, "x"])["n"] == 0
    assert metrics.latency_stats([0.5]) == {"n": 1, "median_ms": 500.0, "p95_ms": 500.0, "min_ms": 500.0,
                                            "max_ms": 500.0}
    s = metrics.latency_stats([0.1, 0.2, 0.3, 0.4])
    assert s["median_ms"] == 250.0 and s["p95_ms"] == 400.0
    assert metrics.percentile(list(range(1, 21)), 95) == 19
    with pytest.raises(ValueError):
        metrics.percentile([], 50)


def test_sign_test_exact_values():
    assert paired.sign_test([1] * 10) == pytest.approx(2 / 1024)
    assert paired.sign_test([0, 0, 0]) == 1.0
    assert paired.sign_test([1, -1]) == 1.0
    assert paired.sign_test([1, 1, 1, -1]) == pytest.approx(0.625)


def test_bootstrap_is_deterministic_and_brackets_the_median():
    diffs = [0.01 * i for i in range(-3, 9)]
    assert paired.bootstrap_ci(diffs, resamples=2000) == paired.bootstrap_ci(diffs, resamples=2000)
    lo, hi = paired.bootstrap_ci(diffs, resamples=2000)
    assert lo <= paired._median(diffs) <= hi


# ── paired comparison on synthetic results ───────────────────────────────────

def synthetic(variant="a", wall=1.0, exec_s=0.5, reps=6, correct=28, fingerprint="f" * 64, cls=None,
              jitter=0.0, seed=0):
    import random
    rng = random.Random(seed)
    runs = []
    for rep in range(reps):
        for ds in ("sales", "student"):
            runs.append({"dataset": ds, "repetition": rep, "variant": variant,
                         "wall_seconds": wall + rng.uniform(-jitter, jitter),
                         "executions": [{"task_id": "t1", "seconds": exec_s + rng.uniform(-jitter, jitter),
                                         "blocked_by_safety_scan": False},
                                        {"task_id": "t9", "seconds": 0.0, "blocked_by_safety_scan": True}]})
    classes = cls or {"mock_scripted/estimated_chars_div_4": {"calls": 30, "input_tokens": 1000,
                                                              "output_tokens": 100}}
    summary = {"repetitions": reps, "answers": {"correct": correct}, "quality": {"correct": 20},
               "tasks": {"completed_of_expected": 12, "expected_failures_observed": 1, "status_as_expected": 13},
               "charts": {"valid": 7}, "reports": {"valid": 12},
               "deterministic_across_repetitions": {"sales": True, "student": True}, "errors": [],
               "llm_calls_per_benchmark_pass": 30, "llm_calls_by_skill": {"planner": 4},
               "tokens_per_benchmark_pass": {"classes": classes}}
    return {"variant": variant, "workload": {"sha256": fingerprint}, "environment": {"python": "3.11"},
            "summary": summary, "runs": runs}


def test_a_versus_a_finds_no_difference():
    a, b = synthetic("a", jitter=0.05, seed=1), synthetic("b", jitter=0.05, seed=2)
    c = paired.compare(a, b, resamples=2000)
    assert c["regressions"] == [] and c["claims"] == []
    assert c["latency"]["pipeline_wall"]["verdict"] == "no significant difference"
    assert c["verdict"] == "no significant improvement"


def test_a_consistent_slowdown_is_detected():
    c = paired.compare(synthetic("a"), synthetic("b", wall=1.05, exec_s=0.55), resamples=2000)
    assert c["latency"]["pipeline_wall"]["verdict"] == "slower"
    assert c["latency"]["sandbox_execution"]["verdict"] == "slower"
    assert c["latency"]["pipeline_wall"]["ci95_median_diff_ms"][0] > 0


def test_a_consistent_speedup_is_claimed_only_without_regressions():
    c = paired.compare(synthetic("a"), synthetic("b", wall=0.9, exec_s=0.4), resamples=2000)
    assert c["latency"]["pipeline_wall"]["verdict"] == "faster" and len(c["claims"]) == 2
    bad = paired.compare(synthetic("a"), synthetic("b", wall=0.9, exec_s=0.4, correct=27), resamples=2000)
    assert bad["regressions"] == ["answers_correct"] and bad["claims"] == [] and bad["verdict"] == "REGRESSION"


def test_blocked_executions_are_not_latency_samples():
    c = paired.compare(synthetic("a"), synthetic("b"), resamples=500)
    assert c["latency"]["sandbox_execution"]["n"] == 12                       # t9 (blocked) excluded


def test_insufficient_pairs():
    c = paired.compare(synthetic("a", reps=2), synthetic("b", reps=2, wall=0.5), resamples=500)
    assert c["latency"]["pipeline_wall"]["verdict"] == "insufficient pairs" and c["claims"] == []


@pytest.mark.parametrize("change,problem", [
    (dict(fingerprint="0" * 64), "workload fingerprints"),
    (dict(reps=5), "repetition counts"),
    (dict(cls={"provider/provider_reported": {"calls": 30, "input_tokens": 1, "output_tokens": 1}}), "provenance"),
])
def test_non_comparable_results_are_refused(change, problem):
    with pytest.raises(paired.NotComparable) as info:
        paired.compare(synthetic("a"), synthetic("b", **change))
    assert any(problem in p for p in info.value.problems)


def test_different_dataset_sets_are_refused():
    b = synthetic("b")
    b["runs"] = [r for r in b["runs"] if r["dataset"] == "sales"]
    with pytest.raises(paired.NotComparable):
        paired.compare(synthetic("a"), b)


def test_mock_token_deltas_are_labelled_prompt_size_not_cost():
    cls_b = {"mock_scripted/estimated_chars_div_4": {"calls": 30, "input_tokens": 800, "output_tokens": 100}}
    c = paired.compare(synthetic("a"), synthetic("b", cls=cls_b), resamples=500)
    t = c["tokens"]["mock_scripted/estimated_chars_div_4"]
    assert t["input_delta"] == -200 and "not cost" in t["note"]
    assert "Paired comparison" in paired.markdown(c)


# ── reports ──────────────────────────────────────────────────────────────────

def test_csv_escapes_commas_quotes_and_newlines(tmp_path):
    rows = [{"variant": "v1", "dataset": "d", "repetition": 0, "task_id": "t", "title": 'a, "b"\nc',
             "status": "completed", "expected_status": "completed", "status_as_expected": True, "retries": 0,
             "expected_retries": 0, "error_type": "", "has_chart": False, "expects_chart": False,
             "has_interpretation": True}]
    p = tmp_path / "t.csv"
    reports.write_csv(p, reports.TASK_COLUMNS, rows)
    back = list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8"))))
    assert back[0]["title"] == 'a, "b"\nc' and len(back) == 1
    with pytest.raises(ValueError):
        reports.write_csv(p, reports.TASK_COLUMNS, [dict(rows[0], unexpected="x")])


# ── isolation ────────────────────────────────────────────────────────────────

def test_patcher_restores_in_reverse_order_and_env_state():
    ns = types.SimpleNamespace(x=1)
    p = Patcher()
    p.setattr(ns, "x", 2)
    p.setattr(ns, "x", 3)
    os.environ.pop("P40_TEST_VAR", None)
    p.setenv("P40_TEST_VAR", "1")
    p.delenv("P40_NEVER_SET")
    p.undo()
    assert ns.x == 1 and "P40_TEST_VAR" not in os.environ


def test_offline_run_blocks_network_and_real_llm_and_restores_everything(tmp_path, monkeypatch):
    import agent.nodes.executor as ex
    import agent.nodes.planner as planner
    import sandbox.executor as sx
    import utils.llm
    from tests.v2.golden import scenarios as sc
    monkeypatch.setenv("GOOGLE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("LESSONS_DIR", "/original/lessons")
    before = (sx.CHARTS_DIR, ex.execute_code, planner.get_llm, utils.llm.ChatGoogleGenerativeAI,
              socket.socket.connect)
    with pytest.raises(RuntimeError, match="boom"):
        with offline_run(sc.SCENARIOS["sales"], str(tmp_path / "w")):
            assert "GOOGLE_API_KEY" not in os.environ
            assert sx.CHARTS_DIR.startswith(str(tmp_path)) and os.environ["LESSONS_DIR"].startswith(str(tmp_path))
            with pytest.raises(OfflineViolation):
                utils.llm.ChatGoogleGenerativeAI(model="x")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(OfflineViolation):
                    s.connect(("192.0.2.1", 443))
            finally:
                s.close()
            assert planner.get_llm is not before[2]
            raise RuntimeError("boom")
    assert (sx.CHARTS_DIR, ex.execute_code, planner.get_llm, utils.llm.ChatGoogleGenerativeAI,
            socket.socket.connect) == before
    assert os.environ["GOOGLE_API_KEY"] == "not-a-real-key" and os.environ["LESSONS_DIR"] == "/original/lessons"


# ── workload fingerprint, variants, CLI, V1 independence ─────────────────────

def test_workload_fingerprint_is_stable_and_sensitive(monkeypatch):
    from tests.v2.golden import scenarios as sc
    f1 = harness.workload_fingerprint(["sales", "student"])
    assert f1 == harness.workload_fingerprint(["student", "sales"])
    changed = copy.deepcopy(sc.SCENARIOS["sales"])
    changed["code"]["Sales Overview"][-1] += "\n# changed\n"
    monkeypatch.setitem(sc.SCENARIOS, "sales", changed)
    assert harness.workload_fingerprint(["sales", "student"])["sha256"] != f1["sha256"]
    monkeypatch.setattr(harness, "MAX_RETRIES", 3)
    assert harness.workload_fingerprint(["student"])["sha256"] != f1["sha256"]


def test_variant_registry_and_argument_validation(tmp_path):
    with harness.temporary_variant(harness.Variant("p40-test", "test only")):
        assert "p40-test" in harness.VARIANTS
        with pytest.raises(ValueError):
            harness.register_variant(harness.Variant("p40-test", "dup"))
    assert "p40-test" not in harness.VARIANTS and "v1" in harness.VARIANTS
    for kwargs in (dict(repetitions=0), dict(datasets=["nope"]), dict(variant="nope")):
        with pytest.raises(ValueError):
            harness.run_benchmark(str(tmp_path), **kwargs)


@pytest.mark.parametrize("folder", ["reports", "lessons", "data"])
def test_cli_refuses_to_write_into_protected_repository_folders(folder):
    from evaluation.__main__ import main
    assert main(["run", "--out", os.path.join(ROOT, folder, "bench"), "--reps", "1"]) == 2


def test_cli_compare_refuses_non_comparable_results(tmp_path):
    from evaluation.__main__ import main
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(synthetic("a")))
    b.write_text(json.dumps(synthetic("b", fingerprint="0" * 64)))
    assert main(["compare", str(a), str(b), "--out", str(tmp_path / "cmp")]) == 2
    b.write_text(json.dumps(synthetic("b", correct=20)))
    assert main(["compare", str(a), str(b), "--out", str(tmp_path / "cmp")]) == 1      # regression
    assert (tmp_path / "cmp" / "comparison.json").exists()


def test_no_v1_module_imports_the_evaluation_package():
    offenders = []
    for folder in ("agent", "sandbox", "utils", "ui"):
        for dirpath, _, files in os.walk(os.path.join(ROOT, folder)):
            for name in files:
                if name.endswith(".py"):
                    tree = ast.parse(_src(os.path.join(dirpath, name)))
                    for node in ast.walk(tree):
                        mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                            [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                        if any(m.split(".")[0] == "evaluation" for m in mods):
                            offenders.append(os.path.join(dirpath, name))
    assert offenders == []


def test_evaluation_never_calls_a_provider_or_network_api():
    forbidden = {"ChatGoogleGenerativeAI", "requests", "urllib", "httpx", "http.client"}
    for name in os.listdir(os.path.join(ROOT, "evaluation")):
        if name.endswith(".py"):
            src = _src(os.path.join(ROOT, "evaluation", name))
            tree = ast.parse(src)
            imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
                n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            assert not imported & forbidden, (name, imported & forbidden)
