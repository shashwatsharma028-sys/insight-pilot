"""
Benchmark harness: run the unmodified V1 pipeline offline and measure it.

    result = run_benchmark(out_dir, datasets=None, repetitions=3, variant="v1")

A *variant* is a named context manager applied around each run (for
example, a future V2 executor). Only "v1", the unchanged pipeline, is
registered in P4.0; evaluation.paired compares two results.

Deterministic metrics (answers, quality, tasks, retries, charts, reports,
LLM calls, token estimates) must be identical across repetitions, and the
result says whether they were. Latency is measured with time.perf_counter
and reported as statistics. It depends on the machine and is labelled with
the environment.
"""

import hashlib
import importlib.metadata as md
import json
import os
import platform
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from itertools import permutations
from typing import Callable, Optional

from evaluation import HARNESS_VERSION, metrics, provenance
from evaluation.isolation import offline_run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MAX_RETRIES = 2
SAFE_MODE = False


@dataclass(frozen=True)
class Variant:
    """
    name/description; apply(ctx): a context manager entered around each run;
    execute_factory(): returns the sandbox execute_code to use (None = the V1 implementation).
    """
    name: str
    description: str
    apply: Callable = field(default=lambda ctx: nullcontext())
    execute_factory: Optional[Callable] = None


def _lazy_seaborn_executor():
    from sandbox_v2.executor import execute_code
    return execute_code


VARIANTS = {
    "v1": Variant("v1", "unchanged V1 LangGraph pipeline and V1 subprocess sandbox"),
    "v1-control": Variant("v1-control", "identical to v1 under another name: the A/A control in interleaved A/B runs"),
    "v2-lazy-seaborn": Variant("v2-lazy-seaborn", "V1 pipeline with the P4.1 V2 sandbox executor (lazy-seaborn "
                               "prelude, sandbox_v2/); everything else unchanged",
                               execute_factory=_lazy_seaborn_executor),
}


def register_variant(variant: Variant):
    if variant.name in VARIANTS:
        raise ValueError(f"variant {variant.name!r} already registered")
    VARIANTS[variant.name] = variant


@contextmanager
def temporary_variant(variant: Variant):
    register_variant(variant)
    try:
        yield variant
    finally:
        VARIANTS.pop(variant.name, None)


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def workload_fingerprint(datasets) -> dict:
    """Everything that defines the workload. Two results are comparable only if this is identical."""
    from tests.v2.golden import reference as ref
    from tests.v2.golden import scenarios as sc
    parts = {}
    for name in sorted(datasets):
        s = sc.SCENARIOS[name]
        parts[name] = {
            "dataset_sha256": _sha256_file(os.path.join(ref.DATA_DIR, s["file"])),
            "scenario_sha256": hashlib.sha256(metrics.canonical(
                {k: (sorted(v) if isinstance(v, set) else v) for k, v in s.items()}).encode()).hexdigest(),
            "expected_sha256": hashlib.sha256(metrics.canonical(ref.load_expected(name)).encode()).hexdigest(),
        }
    body = {"harness": HARNESS_VERSION, "max_retries": MAX_RETRIES, "safe_mode": SAFE_MODE, "datasets": parts}
    return {"sha256": hashlib.sha256(metrics.canonical(body).encode()).hexdigest(), **body}


def environment() -> dict:
    def ver(dist):
        try:
            return md.version(dist)
        except md.PackageNotFoundError:
            return None
    try:
        commit = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=10).stdout.strip() or None
        dirty = bool(subprocess.run(["git", "-C", ROOT, "status", "--porcelain"], capture_output=True, text=True,
                                    timeout=10).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(), "cpu_count": os.cpu_count(),
            "git_commit": commit, "git_dirty": dirty,
            "packages": {d: ver(d) for d in ("langgraph", "langchain-core", "pandas", "numpy", "matplotlib",
                                             "seaborn", "pillow", "reportlab", "nbformat", "streamlit")}}


def _snapshot_behavior(dataset):
    from tests.v2.golden.compare import snapshot_path
    path = snapshot_path(f"{dataset}_graph_safe_mode_off")
    with open(path, encoding="utf-8") as f:
        return json.load(f)["behavior"]


def run_once(dataset, repetition, out_dir, variant="v1", clock=time.perf_counter) -> dict:
    from agent.token_monitor import monitor
    from tests.v2.conftest import make_initial_state
    from tests.v2.golden import reference as ref
    from tests.v2.golden import scenarios as sc
    from tests.v2.golden.compare import normalize_run

    v = VARIANTS[variant]
    scenario = sc.SCENARIOS[dataset]
    workdir = os.path.join(out_dir, "runs", v.name, dataset, f"rep{repetition}")
    error = None
    execute_impl = v.execute_factory() if v.execute_factory else None
    with offline_run(scenario, workdir, clock=clock, execute_impl=execute_impl) as (controller, executions, dirs):
        with v.apply({"dataset": dataset, "repetition": repetition, "dirs": dirs}):
            from agent.graph import analyst_graph
            t0 = clock()
            try:
                final = analyst_graph.invoke(make_initial_state(dataset, safe_mode=SAFE_MODE,
                                                                max_retries=MAX_RETRIES))
            except Exception as exc:                     # recorded, never hidden
                final, error = {}, f"{type(exc).__name__}: {exc}"
            wall = clock() - t0
        monitor_summary = monitor.summary()
        calls = list(controller.calls)
        mock_log = list(controller.mock_calls)
        behavior = normalize_run(final, controller, monitor_summary, safe_mode=SAFE_MODE)["behavior"] \
            if final else None
        task_rows = metrics.score_tasks(final, scenario) if final else []
        printed, duplicates = metrics.printed_answers(final) if final else ({}, [])
        expected = ref.load_expected(dataset)
        result = {
            "dataset": dataset, "repetition": repetition, "variant": v.name, "error": error,
            "wall_seconds": wall,
            "answers": metrics.score_answers(printed, expected["answers"]),
            "duplicate_answers": duplicates,
            "quality": metrics.score_quality((final or {}).get("data_quality_report"), expected["quality"]),
            "tasks": task_rows, "task_summary": metrics.task_summary(task_rows),
            "executions": executions,
            "charts": metrics.chart_validity(final, scenario, dirs["charts"]) if final else
            {"expected": len(scenario["expected_charts"]), "valid": 0, "unexpected": [], "per_task": {}},
            "reports": metrics.report_validity(final, dirs["reports"]) if final else {"valid": 0, "expected": 3},
            "llm_calls": calls, "llm_calls_by_skill": controller.calls_by_skill(),
            "tokens": provenance.aggregate(calls),
            "monitor": {k: monitor_summary.get(k) for k in ("calls", "input_tokens", "output_tokens")}
            | {"estimated_calls": monitor.estimated_calls},
            "scripted_calls_seen_by_mock": len(mock_log),
            "behavior_matches_v1_snapshot": behavior == _snapshot_behavior(dataset) if behavior else False,
        }
    return result


def deterministic_view(run) -> dict:
    """The parts of a run that must not vary between repetitions."""
    return {k: run[k] for k in ("error", "answers", "duplicate_answers", "quality", "tasks", "task_summary",
                                "llm_calls_by_skill", "tokens", "monitor", "behavior_matches_v1_snapshot")} | {
        "executions": [{k: e[k] for k in e if k != "seconds"} for e in run["executions"]],
        "charts": {"expected": run["charts"]["expected"], "valid": run["charts"]["valid"],
                   "sizes": {t: (c["width"], c["height"]) for t, c in run["charts"]["per_task"].items()},
                   "pixels": {t: c.get("pixels_sha256") for t, c in run["charts"]["per_task"].items()}},
        "reports": {k: (run["reports"][k] if not isinstance(run["reports"].get(k), dict)
                        else run["reports"][k].get("valid")) for k in run["reports"]},
    }


def summarize(runs, datasets, repetitions) -> dict:
    first = [r for r in runs if r["repetition"] == 0]
    det = {}
    for ds in datasets:
        views = {metrics.canonical(deterministic_view(r)) for r in runs if r["dataset"] == ds}
        det[ds] = len(views) == 1
    total = lambda key, sub: sum(r[key][sub] for r in first)  # noqa: E731
    by_ds = {ds: {"wall": metrics.latency_stats([r["wall_seconds"] for r in runs if r["dataset"] == ds]),
                  "sandbox_executions": metrics.latency_stats(
                      [e["seconds"] for r in runs if r["dataset"] == ds for e in r["executions"]])}
             for ds in datasets}
    calls = [c for r in first for c in r["llm_calls"]]
    return {
        "datasets": len(datasets), "repetitions": repetitions, "runs": len(runs),
        "errors": sorted({r["error"] for r in runs if r["error"]}),
        "deterministic_across_repetitions": det,
        "answers": {"correct": total("answers", "correct"), "incorrect": total("answers", "incorrect"),
                    "missing": total("answers", "missing"), "expected": total("answers", "expected")},
        "quality": {"correct": total("quality", "correct"), "expected": total("quality", "expected")},
        "tasks": {k: sum(r["task_summary"][k] for r in first) for k in
                  ("planned", "completed", "failed", "expected_completed", "completed_of_expected",
                   "expected_failures", "expected_failures_observed", "status_as_expected", "retries",
                   "retries_as_expected")},
        "sandbox_executions": {"total": sum(len(r["executions"]) for r in first),
                               "blocked_by_safety_scan": sum(e["blocked_by_safety_scan"] for r in first
                                                             for e in r["executions"]),
                               "failed": sum(not e["success"] for r in first for e in r["executions"])},
        "charts": {"valid": total("charts", "valid"), "expected": total("charts", "expected")},
        "reports": {"valid": sum(r["reports"]["valid"] for r in first),
                    "expected": sum(r["reports"]["expected"] for r in first)},
        "behavior_matches_v1_snapshot": sum(r["behavior_matches_v1_snapshot"] for r in first),
        "llm_calls_per_benchmark_pass": len(calls),
        "llm_calls_by_skill": {s: sum(1 for c in calls if c["skill"] == s) for s in sorted({c["skill"] for c in calls})},
        "tokens_per_benchmark_pass": provenance.aggregate(calls),
        "latency": {"pipeline_wall_all_runs": metrics.latency_stats([r["wall_seconds"] for r in runs]),
                    "sandbox_execution_all": metrics.latency_stats([e["seconds"] for r in runs
                                                                    for e in r["executions"]]),
                    "llm_call_mock_all": metrics.latency_stats([c["seconds"] for r in runs
                                                                for c in r["llm_calls"]]),
                    "by_dataset": by_ds},
    }


def ab_schedule(variants, datasets, repetitions):
    """
    Dataset-level interleaving: for every (repetition, dataset) cell, all variants run back to back, in an
    order that cycles through every permutation, so each variant takes each position equally often when
    the number of cells is a multiple of len(variants)!.
    """
    perms = list(permutations(variants))
    out, cell = [], 0
    for rep in range(repetitions):
        for ds in datasets:
            for pos, v in enumerate(perms[cell % len(perms)]):
                out.append({"repetition": rep, "dataset": ds, "variant": v, "position": pos, "cell": cell})
            cell += 1
    return out


def schedule_balance(schedule) -> dict:
    counts = {}
    for s in schedule:
        counts.setdefault(s["variant"], {}).setdefault(s["position"], 0)
        counts[s["variant"]][s["position"]] += 1
    return counts


def run_ab_benchmark(out_dir, variants=("v1", "v2-lazy-seaborn", "v1-control"), datasets=None, repetitions=6,
                     clock=time.perf_counter) -> dict:
    """Run several variants interleaved and counterbalanced in ONE session; return {variant: result}."""
    from tests.v2.golden import scenarios as sc
    datasets = sorted(datasets or sc.SCENARIOS)
    variants = list(variants)
    if len(variants) < 2 or len(set(variants)) != len(variants):
        raise ValueError("need at least two distinct variants")
    for v in variants:
        if v not in VARIANTS:
            raise ValueError(f"unknown variant {v!r}")
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    schedule = ab_schedule(variants, datasets, repetitions)
    runs = {v: [] for v in variants}
    for i, s in enumerate(schedule):
        r = run_once(s["dataset"], s["repetition"], out_dir, variant=s["variant"], clock=clock)
        r["schedule_index"], r["schedule_position"] = i, s["position"]
        runs[s["variant"]].append(r)
    workload, env = workload_fingerprint(datasets), environment()
    session = {"variants": variants, "cells": len(schedule) // len(variants), "runs": len(schedule),
               "design": "dataset-level interleaving; variant order cycles through all permutations",
               "position_balance": schedule_balance(schedule)}
    return {v: {"schema": "insightpilot.benchmark/1", "harness": HARNESS_VERSION, "variant": v,
                "variant_description": VARIANTS[v].description, "started_utc": started, "workload": workload,
                "environment": env, "llm": "scripted mock (tests/v2/mock_llm.py); no provider calls are possible",
                "ab_session": session, "summary": summarize(runs[v], datasets, repetitions), "runs": runs[v]}
            for v in variants}


def run_benchmark(out_dir, datasets=None, repetitions=3, variant="v1", clock=time.perf_counter) -> dict:
    from tests.v2.golden import scenarios as sc
    datasets = sorted(datasets or sc.SCENARIOS)
    if repetitions < 1:
        raise ValueError("repetitions must be >= 1")
    unknown = sorted(set(datasets) - set(sc.SCENARIOS))
    if unknown:
        raise ValueError(f"unknown datasets: {unknown}")
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; registered: {sorted(VARIANTS)}")
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    runs = []
    for rep in range(repetitions):                   # interleave datasets per repetition
        for ds in datasets:
            runs.append(run_once(ds, rep, out_dir, variant=variant, clock=clock))
    return {"schema": "insightpilot.benchmark/1", "harness": HARNESS_VERSION, "variant": variant,
            "variant_description": VARIANTS[variant].description, "started_utc": started,
            "workload": workload_fingerprint(datasets), "environment": environment(),
            "llm": "scripted mock (tests/v2/mock_llm.py); no provider calls are possible",
            "summary": summarize(runs, datasets, repetitions), "runs": runs}
