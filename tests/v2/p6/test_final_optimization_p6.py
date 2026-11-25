"""
P6 final optimisation phase (offline only): evidence reproduction, latency decomposition, the compact guidance
candidate and the combined-guidance experiment definition. No network, no key, no live call.
"""

import json
import math
import os
import subprocess
import sys

import pytest

from evaluation import live
from multi_agent import claims, prompts
from tests.v2.p6.test_live_p6 import _hidden_needles

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "scripts", "p6"))
import compact_guidance as cg  # noqa: E402
import evidence_package as ev  # noqa: E402


# ── 1. the published selective-recovery numbers reproduce from the raw records ──────────────────────────

def test_selective_recovery_metrics_reproduce_exactly_from_raw_records():
    r = ev.recovery_evidence()
    c, o = r["control"], r["selective_recovery"]
    assert r["records"] == 37 and r["matched_pairs"] == 18 and len(r["unmatched_runs"]) == 1
    assert (c["hidden_correct"], c["hidden_total"], o["hidden_correct"], o["hidden_total"]) == (47, 104, 77, 104)
    assert (c["correct_all"], c["answered"], o["correct_all"], o["answered"]) == (58, 61, 96, 105)
    assert (c["planner_failures"], o["planner_failures"], c["runs"]) == (9, 2, 18)
    assert (c["required_tasks_completed"], o["required_tasks_completed"], c["required_tasks"]) == (26, 43, 54)
    assert (c["input_tokens"], c["output_tokens"], o["input_tokens"], o["output_tokens"]) == (28241, 16161, 31726, 16315)
    assert round(c["tokens_per_correct"]) == 945 and round(o["tokens_per_correct"]) == 624
    assert (c["cost_successful_usd"], o["cost_successful_usd"]) == (0.048875, 0.050305)
    assert round(c["latency_p95_nearest_rank_s"], 1) == 163.1 and round(o["latency_p95_nearest_rank_s"], 1) == 147.4
    ch = r["changes"]
    assert round(ch["hidden_accuracy"]["abs_pp"], 1) == 28.8 and round(ch["hidden_accuracy"]["relative_pct"], 1) == 63.8
    assert round(ch["planner_failure_rate"]["abs_pp"], 1) == -38.9
    assert round(ch["cost_per_correct_usd"]["relative_pct"], 1) == -37.2
    p = r["paired_hidden_answers"]
    assert (p["wins"], p["losses"], p["ties"]) == (9, 2, 7) and round(p["sign_test_p"], 4) == 0.0654
    assert p["ci95"][0] > 0                                                       # CI excludes zero


def test_evidence_script_agrees_with_the_harness_on_accuracy():
    """Independent recomputation vs the harness's own summary (same data, separate code)."""
    runs = ev.load(ev.RECOVERY)
    s = live.summarize_variants(runs)
    assert s["paired_hidden_correct"] == {"control": 47, "selective_recovery": 77, "of": 104}


# ── 2. latency decomposition accounts for the whole wall time ───────────────────────────────────────────

@pytest.mark.parametrize("which", ["recovery", "baseline"])
def test_latency_decomposition_sums_to_wall_time_and_deterministic_work_is_small(which):
    runs = ev.load(ev.RECOVERY) if which == "recovery" else [r for r in ev.load(ev.BASELINE) if r["arch"] == "multi_agent"]
    b = ev.latency_breakdown(runs)
    t = b["totals_s"]
    parts = t["wait_s"] + t["llm_success_s"] + t["llm_failed_s"] + t["other_s"]
    assert math.isclose(parts, t["wall_s"], rel_tol=1e-6)
    assert b["share_of_wall"]["other_s"] < 0.01                                   # < 1% of wall time
    assert b["share_of_wall"]["wait_s"] + b["share_of_wall"]["llm_success_s"] > 0.8


# ── 3. the compact guidance candidate loses nothing and is wired to nothing ─────────────────────────────

def test_compact_guidance_keeps_every_op_argument_kind_and_result():
    c = cg.op_guidance_compact()
    for op, spec in claims.OPS.items():
        line = next(l for l in c.splitlines() if l.startswith(op + "("))
        assert claims.OP_RESULTS[op] in line
        for name, rule in spec.items():
            arg = name.rstrip("?")
            assert f"{arg}" in line
            if rule == "str":
                assert f"{arg}:cell-text" in line
            elif rule == "prefix":
                assert f"{arg}?:int 1-{claims.MAX_PREFIX}" in line
            elif rule == claims.ANY:
                assert f"{arg}:any" in line
            else:
                assert f"{arg}:{'|'.join(rule)}" in line


@pytest.mark.parametrize("fact", [
    "JSON object", "({} when an op has none)", "exactly key, op and args", "exact column names",
    "never a JSON boolean, number or null", '"True" or "False"', "result shape", "*_where_equals, not group_*",
    "a mapping only when", "first by_prefix characters", "ties go to the first group in sorted order",
    "YYYY-MM-DD", "by_prefix 7 groups by calendar month (YYYY-MM)", "4 by calendar year (YYYY)",
    "merges it across years"])
def test_compact_guidance_keeps_every_rule(fact):
    assert fact in cg.op_guidance_compact()


def test_compact_guidance_is_a_third_smaller_and_leaks_nothing():
    c = cg.op_guidance_compact()
    full = prompts.op_schema_section() + prompts.op_semantics_section()
    assert len(c) < 0.70 * len(full)                                              # measured: 2,464 vs 3,725 chars
    for ds in live.DATASETS:
        for needle in _hidden_needles(ds):
            assert needle not in c
        assert ds not in c.lower()


@pytest.mark.parametrize("schema,semantic", [(False, False), (True, False), (False, True), (True, True)])
def test_compact_guidance_is_not_used_by_any_prompt(schema, semantic):
    prof = live.dsmod.profile(live.dsmod.load(live.dataset_path("sales")))
    req = {"attempt": 1, "goal": "g", "profile": prof, "max_tasks": 8, "feedback": [],
           "op_schema": schema, "op_semantics": semantic}
    assert "CLAIM OPS (op(args)" not in prompts.planning(req)
    assert "CLAIM OPS (op(args)" not in live.single_claims_prompt(prof, live.tasks_for("sales")[0], 1, [], schema, semantic)


# ── 4. the combined-guidance experiment isolates exactly the two sections ───────────────────────────────

def test_the_combined_experiment_differs_only_in_both_guidance_sections(tmp_path):
    rc = live.main(["--provider", "fake", "--mode", "full", "--experiment", "combined_guidance",
                    "--transient-retries", "2", "--out", str(tmp_path)])
    exp = json.load(open(tmp_path / "combined_guidance_experiment.json"))
    assert rc == 0 and exp["summary"]["matched_pairs"] == 20 and exp["common_run"] == {}
    base = [[d, r] for a, d, r in live.schedule(live.DATASETS, 5, 20261124) if a == "multi_agent"]
    assert exp["slots"] == base                                                   # the baseline's workload and order
    for r in exp["results"]:
        sch, sem = r["prompt_audit"]["planner"], r["semantics_audit"]["planner"]
        if r["variant"] == "optimized":
            assert sch["without_guidance"] == 0 and sem["without_semantics"] == 0
        else:
            assert sch["with_guidance"] == 0 and sem["with_semantics"] == 0
        assert "plan_recovery" in r["run"]                                        # selective recovery in BOTH arms
    assert {r["transient_retries"] for r in exp["results"]} == {0}                # mock never fails; policy shared


def test_the_combined_experiment_refuses_to_hold_a_guidance_section_constant():
    with pytest.raises(ValueError):
        live.run_matched_experiment(lambda *a: None, "combined_guidance", common_run={"schema_aware": True})


def test_the_evidence_script_runs_standalone():
    out = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "scripts", "p6", "evidence_package.py"), "--markdown"],
                         capture_output=True, text=True, check=True, cwd=ROOT,
                         env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}).stdout
    assert "47/104 = 45.2%" in out and "77/104 = 74.0%" in out
