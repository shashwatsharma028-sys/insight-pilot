"""
P4.0 integration: the harness drives the UNMODIFIED V1 pipeline offline.

The scripted LLM and the real V1 subprocess sandbox are used. The tests
check that the measurements are correct, deterministic, isolated and
complete.
"""

import csv
import json
import os

import pytest

from evaluation import harness, reports

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _tree(path):
    out = {}
    for d, _, files in os.walk(path):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            out[os.path.relpath(p, path)] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(scope="module")
def sales_two_reps(tmp_path_factory):
    out = tmp_path_factory.mktemp("bench_sales")
    before = {d: _tree(os.path.join(ROOT, d)) for d in ("reports", "lessons", "data")}
    result = harness.run_benchmark(str(out), datasets=["sales"], repetitions=2)
    after = {d: _tree(os.path.join(ROOT, d)) for d in ("reports", "lessons", "data")}
    return result, out, before, after


def test_sales_measurements_are_complete_and_correct(sales_two_reps):
    result, out, _, _ = sales_two_reps
    s = result["summary"]
    assert s["errors"] == [] and s["runs"] == 2
    assert s["answers"] == {"correct": 7, "incorrect": 0, "missing": 0, "expected": 7}
    assert s["quality"] == {"correct": 5, "expected": 5}
    assert s["tasks"]["completed_of_expected"] == 3 and s["tasks"]["expected_failures_observed"] == 1
    assert s["tasks"]["status_as_expected"] == 4 and s["tasks"]["retries_as_expected"] == 4
    assert s["sandbox_executions"]["blocked_by_safety_scan"] >= 1               # adversarial task never runs
    assert s["charts"] == {"valid": 2, "expected": 2} and s["reports"] == {"valid": 3, "expected": 3}
    assert s["behavior_matches_v1_snapshot"] == 1
    assert s["deterministic_across_repetitions"] == {"sales": True}


def test_token_provenance_reconciles_with_the_v1_monitor(sales_two_reps):
    result, _, _, _ = sales_two_reps
    for run in result["runs"]:
        classes = run["tokens"]["classes"]
        assert list(classes) == ["mock_scripted/estimated_chars_div_4"]
        mock = classes["mock_scripted/estimated_chars_div_4"]
        assert mock["cost_usd_estimate"] is None and run["tokens"]["provider_calls"] == 0
        assert (mock["calls"], mock["input_tokens"], mock["output_tokens"]) == (
            run["monitor"]["calls"], run["monitor"]["input_tokens"], run["monitor"]["output_tokens"])
        assert run["monitor"]["estimated_calls"] == run["monitor"]["calls"] == run["scripted_calls_seen_by_mock"]
        assert run["llm_calls_by_skill"] == {"business_insights": 3, "code_generator": 7, "planner": 1}


def test_latencies_are_measured_not_invented(sales_two_reps):
    result, _, _, _ = sales_two_reps
    for run in result["runs"]:
        assert run["wall_seconds"] > 0
        ran = [e for e in run["executions"] if not e["blocked_by_safety_scan"]]
        assert ran and all(e["seconds"] > 0 for e in ran)
        assert sum(e["seconds"] for e in run["executions"]) <= run["wall_seconds"]
    lat = result["summary"]["latency"]
    assert lat["pipeline_wall_all_runs"]["n"] == 2 and lat["sandbox_execution_all"]["n"] >= 8


def test_outputs_are_isolated_from_the_repository(sales_two_reps):
    result, out, before, after = sales_two_reps
    assert before == after                                       # reports/, lessons/, data/ untouched
    for run in result["runs"]:
        for t in run["charts"]["per_task"].values():
            assert t["path_inside_output_dir"]
    assert os.path.isdir(os.path.join(str(out), "runs", "v1", "sales", "rep1", "reports", "charts"))


def test_reports_are_written_in_three_formats_and_round_trip(sales_two_reps, tmp_path):
    result, _, _, _ = sales_two_reps
    paths = reports.write_all(result, str(tmp_path))
    with open(paths["json"], encoding="utf-8") as f:
        assert json.load(f)["summary"] == json.loads(json.dumps(result["summary"], default=str))
    def rows(p):
        with open(p, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    assert len(rows(paths["runs_csv"])) == 2
    assert len(rows(paths["tasks_csv"])) == 8
    assert len(rows(paths["llm_calls_csv"])) == 22
    assert {r["call_source"] for r in rows(paths["llm_calls_csv"])} == {"mock_scripted"}
    assert len(rows(paths["executions_csv"])) == sum(len(r["executions"]) for r in result["runs"])
    with open(paths["markdown"], encoding="utf-8") as f:
        md = f.read()
    assert "7/7 correct" in md and "never cost or savings" in md and result["workload"]["sha256"] in md


def test_variant_hook_is_applied_around_every_run(tmp_path):
    entered = []

    from contextlib import contextmanager

    @contextmanager
    def apply(ctx):
        entered.append((ctx["dataset"], ctx["repetition"]))
        yield
    with harness.temporary_variant(harness.Variant("p40-probe", "records hook calls", apply)):
        result = harness.run_benchmark(str(tmp_path), datasets=["attendance"], repetitions=1, variant="p40-probe")
    assert entered == [("attendance", 0)] and result["variant"] == "p40-probe"
    assert result["summary"]["answers"]["correct"] == 7


def test_all_four_datasets_meet_every_reference(tmp_path):
    result = harness.run_benchmark(str(tmp_path), repetitions=1)
    s = result["summary"]
    assert s["errors"] == []
    assert s["answers"] == {"correct": 28, "incorrect": 0, "missing": 0, "expected": 28}
    assert s["quality"] == {"correct": 20, "expected": 20}
    assert s["tasks"]["completed_of_expected"] == s["tasks"]["expected_completed"] == 12
    assert s["charts"] == {"valid": 7, "expected": 7} and s["reports"] == {"valid": 12, "expected": 12}
    assert s["behavior_matches_v1_snapshot"] == 4
    assert result["workload"]["sha256"] == harness.workload_fingerprint(["attendance", "ecommerce", "sales",
                                                                          "student"])["sha256"]
