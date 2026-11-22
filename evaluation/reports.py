"""
Benchmark report writers: JSON (full), CSV (runs, tasks, LLM calls, sandbox executions) and Markdown.

Every number in the Markdown comes from the result dict. Token figures carry
their provenance class, and mock estimates are labelled as such.
"""

import csv
import json
import os

RUN_COLUMNS = ["variant", "dataset", "repetition", "error", "wall_seconds", "answers_correct", "answers_expected",
               "quality_correct", "quality_expected", "tasks_planned", "tasks_completed", "tasks_failed",
               "retries", "charts_valid", "charts_expected", "reports_valid", "reports_expected", "llm_calls",
               "sandbox_executions", "behavior_matches_v1_snapshot"]
TASK_COLUMNS = ["variant", "dataset", "repetition", "task_id", "title", "status", "expected_status",
                "status_as_expected", "retries", "expected_retries", "error_type", "has_chart", "expects_chart",
                "has_interpretation"]
CALL_COLUMNS = ["variant", "dataset", "repetition", "index", "skill", "call_source", "token_source",
                "input_tokens", "output_tokens", "anomaly", "seconds"]
EXEC_COLUMNS = ["variant", "dataset", "repetition", "index", "task_id", "seconds", "success",
                "blocked_by_safety_scan", "error_type", "chart"]


def run_rows(result):
    for r in result["runs"]:
        yield {"variant": r["variant"], "dataset": r["dataset"], "repetition": r["repetition"],
               "error": r["error"] or "", "wall_seconds": f"{r['wall_seconds']:.6f}",
               "answers_correct": r["answers"]["correct"], "answers_expected": r["answers"]["expected"],
               "quality_correct": r["quality"]["correct"], "quality_expected": r["quality"]["expected"],
               "tasks_planned": r["task_summary"]["planned"], "tasks_completed": r["task_summary"]["completed"],
               "tasks_failed": r["task_summary"]["failed"], "retries": r["task_summary"]["retries"],
               "charts_valid": r["charts"]["valid"], "charts_expected": r["charts"]["expected"],
               "reports_valid": r["reports"]["valid"], "reports_expected": r["reports"]["expected"],
               "llm_calls": len(r["llm_calls"]), "sandbox_executions": len(r["executions"]),
               "behavior_matches_v1_snapshot": r["behavior_matches_v1_snapshot"]}


def task_rows(result):
    for r in result["runs"]:
        for t in r["tasks"]:
            yield {"variant": r["variant"], "dataset": r["dataset"], "repetition": r["repetition"],
                   **{k: ("" if t.get(k) is None else t[k]) for k in TASK_COLUMNS[3:]}}


def call_rows(result):
    for r in result["runs"]:
        for i, c in enumerate(r["llm_calls"]):
            yield {"variant": r["variant"], "dataset": r["dataset"], "repetition": r["repetition"], "index": i,
                   **{k: ("" if c.get(k) is None else c[k]) for k in CALL_COLUMNS[4:]}}


def exec_rows(result):
    for r in result["runs"]:
        for i, e in enumerate(r["executions"]):
            yield {"variant": r["variant"], "dataset": r["dataset"], "repetition": r["repetition"], "index": i,
                   **{k: ("" if e.get(k) is None else e[k]) for k in EXEC_COLUMNS[4:]}}


def write_csv(path, columns, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="raise", quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def _ms(stats):
    if not stats or not stats.get("n"):
        return "n/a"
    return f"n={stats['n']}, median {stats['median_ms']} ms, p95 {stats['p95_ms']} ms " \
           f"(min {stats['min_ms']}, max {stats['max_ms']})"


def markdown(result) -> str:
    s, env = result["summary"], result["environment"]
    t = s["tasks"]
    lines = [
        f"# InsightPilot benchmark: variant `{result['variant']}`", "",
        f"- harness {result['harness']}, started {result['started_utc']}, "
        f"{s['datasets']} datasets x {s['repetitions']} repetitions = {s['runs']} runs",
        f"- workload fingerprint `{result['workload']['sha256']}`",
        f"- environment: Python {env['python']} on {env['platform']} ({env['cpu_count']} CPUs), commit "
        f"`{env['git_commit']}`{' (dirty tree)' if env['git_dirty'] else ''}",
        f"- LLM: {result['llm']}", "",
        "## Correctness (first repetition; every repetition must be identical)", "",
        "| Metric | Result |", "|---|---|",
        f"| Golden numerical answers | {s['answers']['correct']}/{s['answers']['expected']} correct "
        f"({s['answers']['incorrect']} incorrect, {s['answers']['missing']} missing) |",
        f"| Data-quality reference values | {s['quality']['correct']}/{s['quality']['expected']} |",
        f"| Tasks completed (of those expected to complete) | {t['completed_of_expected']}/{t['expected_completed']} |",
        f"| Expected failures observed (adversarial task blocked) | {t['expected_failures_observed']}/{t['expected_failures']} |",
        f"| Task status as expected | {t['status_as_expected']}/{t['planned']} |",
        f"| Retries (total; as scripted) | {t['retries']} ({t['retries_as_expected']}/{t['planned']} as expected) |",
        f"| Sandbox executions (blocked by safety scan; failed) | {s['sandbox_executions']['total']} "
        f"({s['sandbox_executions']['blocked_by_safety_scan']}; {s['sandbox_executions']['failed']}) |",
        f"| Valid charts | {s['charts']['valid']}/{s['charts']['expected']} |",
        f"| Valid reports (Markdown, PDF, notebook) | {s['reports']['valid']}/{s['reports']['expected']} |",
        f"| Behaviour equals committed V1 snapshot | {s['behavior_matches_v1_snapshot']}/{s['datasets']} datasets |",
        f"| Deterministic across repetitions | {sum(s['deterministic_across_repetitions'].values())}/{s['datasets']} datasets |",
        f"| Run errors | {len(s['errors'])} |", "",
        "## LLM calls and tokens (per benchmark pass)", "",
        f"- calls: {s['llm_calls_per_benchmark_pass']} ({', '.join(f'{k} {v}' for k, v in s['llm_calls_by_skill'].items())})",
    ]
    for cls, b in s["tokens_per_benchmark_pass"]["classes"].items():
        lines.append(f"- `{cls}`: {b['calls']} calls, {b['input_tokens']} input / {b['output_tokens']} output "
                     f"tokens; cost: {b['cost_usd_estimate']} ({b['cost_note']})")
    lines += ["", "## Latency (all repetitions; machine-dependent)", "",
              f"- pipeline wall time per run: {_ms(s['latency']['pipeline_wall_all_runs'])}",
              f"- sandbox execution (subprocess) per call: {_ms(s['latency']['sandbox_execution_all'])}",
              f"- scripted LLM call: {_ms(s['latency']['llm_call_mock_all'])}", "",
              "| Dataset | Pipeline wall | Sandbox execution |", "|---|---|---|"]
    for ds, b in s["latency"]["by_dataset"].items():
        lines.append(f"| {ds} | {_ms(b['wall'])} | {_ms(b['sandbox_executions'])} |")
    lines += ["", "Limitations: scripted LLM (no model quality, provider latency or provider tokens); one machine; "
              "latency includes Python start-up of every sandbox subprocess.", ""]
    return "\n".join(lines)


def write_all(result, out_dir) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    paths = {"json": os.path.join(out_dir, "benchmark.json"), "runs_csv": os.path.join(out_dir, "runs.csv"),
             "tasks_csv": os.path.join(out_dir, "tasks.csv"), "llm_calls_csv": os.path.join(out_dir, "llm_calls.csv"),
             "executions_csv": os.path.join(out_dir, "executions.csv"), "markdown": os.path.join(out_dir, "summary.md")}
    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, sort_keys=True, default=str)
        f.write("\n")
    write_csv(paths["runs_csv"], RUN_COLUMNS, run_rows(result))
    write_csv(paths["tasks_csv"], TASK_COLUMNS, task_rows(result))
    write_csv(paths["llm_calls_csv"], CALL_COLUMNS, call_rows(result))
    write_csv(paths["executions_csv"], EXEC_COLUMNS, exec_rows(result))
    with open(paths["markdown"], "w", encoding="utf-8") as f:
        f.write(markdown(result))
    return paths
