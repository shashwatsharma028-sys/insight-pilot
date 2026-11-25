"""
P6 evidence package: recompute every reported metric DIRECTLY from the committed raw run records
(docs/v2/p6/*/runs.jsonl), without the harness's summary functions, so the published numbers can be checked
independently. Offline only; reads files; makes no model call.

    python -B scripts/p6/evidence_package.py            # prints JSON
    python -B scripts/p6/evidence_package.py --markdown # prints a Markdown table

Definitions (identical for every arm):
- hidden accuracy      = correct hidden answers / hidden questions (grade.primary_keys per run)
- claim precision      = correct answers / answered (reported) answers, over all graded keys
- planner failure rate = runs whose status is plan_invalid / runs
- required-task completion = required tasks with every question answered / required tasks (every run counts)
- tokens               = provider-reported input/output tokens of SUCCESSFUL calls only
- cost (estimate)      = successful-call cost; failed-attempt worst-case charges are reported separately
- latency              = wall-clock seconds per run; "excl. waits" subtracts pacing + rate-limit waits (wait_s).
  p95 is reported two ways: linear interpolation, and nearest-rank (the harness's evaluation.metrics method,
  used in the earlier records). With n = 18 runs per arm, the nearest-rank p95 IS the slowest run.
"""

import json
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RECOVERY = os.path.join(ROOT, "docs", "v2", "p6", "recovery_experiment", "runs.jsonl")
BASELINE = os.path.join(ROOT, "docs", "v2", "p6", "baseline_attempt3", "runs.jsonl")


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def required_tasks(dataset):
    """Required tasks and their question keys (the benchmark's task definitions)."""
    sys.path.insert(0, ROOT)
    from evaluation.live import tasks_for
    return [list(t["questions"]) for t in tasks_for(dataset)]


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def nearest_rank(xs, q):
    xs = sorted(xs)
    return xs[max(1, math.ceil(q * len(xs))) - 1] if xs else None


def arm_metrics(runs):
    n = len(runs)
    hidden_ok = hidden_n = ok_all = answered = 0
    plan_invalid = req_done = req_n = 0
    tin = tout = calls = ok_calls = 0
    cost_ok = cost_fail = 0.0
    walls, walls_x, waits = [], [], []
    for r in runs:
        per = r["grade"]["per_key"]
        prim = r["grade"]["primary_keys"]
        hidden_n += len(prim)
        hidden_ok += sum(per[k] == "correct" for k in prim)
        ok_all += sum(v == "correct" for v in per.values())
        answered += sum(v != "missing" for v in per.values())
        plan_invalid += r["run"].get("status") == "plan_invalid"
        for keys in required_tasks(r["dataset"]):
            req_n += 1
            req_done += all(per.get(k, "missing") != "missing" for k in keys)
        for c in r["llm_records"]:
            calls += 1
            if c.get("failed"):
                cost_fail += c["cost_usd"]
            else:
                ok_calls += 1
                tin += c["input_tokens"]
                tout += c["output_tokens"]
                cost_ok += c["cost_usd"]
        walls.append(r["wall_s"])
        waits.append(r.get("wait_s", 0.0))
        walls_x.append(max(0.0, r["wall_s"] - r.get("wait_s", 0.0)))
    return {
        "runs": n, "hidden_correct": hidden_ok, "hidden_total": hidden_n,
        "hidden_accuracy": hidden_ok / hidden_n,
        "claim_precision": ok_all / answered if answered else None, "answered": answered, "correct_all": ok_all,
        "planner_failures": plan_invalid, "planner_failure_rate": plan_invalid / n,
        "required_tasks_completed": req_done, "required_tasks": req_n, "required_task_completion": req_done / req_n,
        "provider_attempts": calls, "successful_calls": ok_calls,
        "input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout,
        "tokens_per_correct": (tin + tout) / hidden_ok if hidden_ok else None,
        "cost_successful_usd": round(cost_ok, 6), "cost_failed_worst_case_usd": round(cost_fail, 6),
        "cost_per_correct_usd": cost_ok / hidden_ok if hidden_ok else None,
        "latency_p50_s": pct(walls, 0.5), "latency_p95_s": pct(walls, 0.95),
        "latency_excl_wait_p50_s": pct(walls_x, 0.5), "latency_excl_wait_p95_s": pct(walls_x, 0.95),
        "latency_p95_nearest_rank_s": nearest_rank(walls, 0.95),
        "latency_excl_wait_p95_nearest_rank_s": nearest_rank(walls_x, 0.95),
        "provider_wait_total_s": sum(waits), "provider_wait_mean_s": sum(waits) / n,
    }


def matched_pairs(runs, a="selective_recovery", b="control"):
    by = {}
    for r in runs:
        by.setdefault(r["slot"], {})[r["variant"]] = r
    pairs = [p for _, p in sorted(by.items()) if a in p and b in p]
    unmatched = [r for p in by.values() if not (a in p and b in p) for r in p.values()]
    return pairs, unmatched


def paired_effect(diffs, seed=1, n_boot=20000):
    rng = random.Random(seed)
    boots = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n_boot))
    n, k = sum(d != 0 for d in diffs), sum(d > 0 for d in diffs)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n) if n else 1.0
    return {"pairs": len(diffs), "mean_diff": sum(diffs) / len(diffs),
            "ci95": [boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot) - 1]],
            "wins": k, "losses": n - k, "ties": len(diffs) - n, "sign_test_p": p}


def changes(opt, ctl):
    """Absolute (percentage points for rates) and relative change, optimised vs control."""
    out = {}
    for key in ("hidden_accuracy", "claim_precision", "planner_failure_rate", "required_task_completion"):
        out[key] = {"control": ctl[key], "optimised": opt[key], "abs_pp": 100 * (opt[key] - ctl[key]),
                    "relative_pct": 100 * (opt[key] - ctl[key]) / ctl[key] if ctl[key] else None}
    for key in ("input_tokens", "output_tokens", "total_tokens", "tokens_per_correct", "cost_successful_usd",
                "cost_per_correct_usd", "latency_p50_s", "latency_p95_s", "latency_excl_wait_p50_s",
                "latency_excl_wait_p95_s", "provider_wait_total_s"):
        out[key] = {"control": ctl[key], "optimised": opt[key], "abs": opt[key] - ctl[key],
                    "relative_pct": 100 * (opt[key] - ctl[key]) / ctl[key] if ctl[key] else None}
    return out


def recovery_evidence():
    runs = load(RECOVERY)
    pairs, unmatched = matched_pairs(runs)
    opt = arm_metrics([p["selective_recovery"] for p in pairs])
    ctl = arm_metrics([p["control"] for p in pairs])
    diffs = [p["selective_recovery"]["grade"]["correct_hidden"] - p["control"]["grade"]["correct_hidden"] for p in pairs]
    return {"source": os.path.relpath(RECOVERY, ROOT), "records": len(runs), "matched_pairs": len(pairs),
            "unmatched_runs": [{"variant": r["variant"], "dataset": r["dataset"], "rep": r["rep"],
                                "hidden": f'{r["grade"]["correct_hidden"]}/{r["grade"]["total_hidden"]}'}
                               for r in unmatched],
            "control": ctl, "selective_recovery": opt, "changes": changes(opt, ctl),
            "paired_hidden_answers": paired_effect(diffs)}


def baseline_evidence():
    runs = load(BASELINE)
    return {"source": os.path.relpath(BASELINE, ROOT),
            "single_agent": arm_metrics([r for r in runs if r["arch"] == "single_agent"]),
            "multi_agent": arm_metrics([r for r in runs if r["arch"] == "multi_agent"])}


def latency_breakdown(runs):
    """Per run: wall = waits (pacing + rate limit) + provider call time (successful + failed) + everything else
    (graph, prompt building, trusted computation, verification, grading overhead). From recorded traces only."""
    tot = {"wall_s": 0.0, "wait_s": 0.0, "llm_success_s": 0.0, "llm_failed_s": 0.0, "other_s": 0.0}
    by_skill = {}
    for r in runs:
        ok = sum(c.get("seconds", 0.0) for c in r["llm_records"] if not c.get("failed"))
        bad = sum(c.get("seconds", 0.0) for c in r["llm_records"] if c.get("failed"))
        wait = r.get("wait_s", 0.0)
        tot["wall_s"] += r["wall_s"]
        tot["wait_s"] += wait
        tot["llm_success_s"] += ok
        tot["llm_failed_s"] += bad
        tot["other_s"] += max(0.0, r["wall_s"] - wait - ok - bad)
        for c in r["llm_records"]:
            if not c.get("failed"):
                by_skill.setdefault(c["skill"], []).append(c.get("seconds", 0.0))
    share = {k: v / tot["wall_s"] for k, v in tot.items() if k != "wall_s"}
    return {"runs": len(runs), "totals_s": tot, "share_of_wall": share,
            "successful_call_seconds_by_skill": {k: {"calls": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95)}
                                                 for k, v in sorted(by_skill.items())}}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    rec = load(RECOVERY)
    out = {"recovery_experiment": recovery_evidence(), "baseline_attempt3": baseline_evidence(),
           "latency_recovery_experiment": latency_breakdown(rec),
           "latency_baseline_multi_agent": latency_breakdown([r for r in load(BASELINE) if r["arch"] == "multi_agent"])}
    if "--markdown" in argv:
        print(markdown(out))
    else:
        print(json.dumps(out, indent=1, sort_keys=True, default=str))
    return 0


def markdown(out) -> str:
    r = out["recovery_experiment"]
    c, o = r["control"], r["selective_recovery"]
    rows = [("Hidden-answer accuracy", f'{c["hidden_correct"]}/{c["hidden_total"]} = {c["hidden_accuracy"]:.1%}',
             f'{o["hidden_correct"]}/{o["hidden_total"]} = {o["hidden_accuracy"]:.1%}'),
            ("Claim precision", f'{c["correct_all"]}/{c["answered"]} = {c["claim_precision"]:.1%}',
             f'{o["correct_all"]}/{o["answered"]} = {o["claim_precision"]:.1%}'),
            ("Planner failure rate", f'{c["planner_failures"]}/{c["runs"]} = {c["planner_failure_rate"]:.1%}',
             f'{o["planner_failures"]}/{o["runs"]} = {o["planner_failure_rate"]:.1%}'),
            ("Required-task completion", f'{c["required_tasks_completed"]}/{c["required_tasks"]} = {c["required_task_completion"]:.1%}',
             f'{o["required_tasks_completed"]}/{o["required_tasks"]} = {o["required_task_completion"]:.1%}'),
            ("Tokens in / out (successful calls)", f'{c["input_tokens"]:,} / {c["output_tokens"]:,}', f'{o["input_tokens"]:,} / {o["output_tokens"]:,}'),
            ("Tokens per correct answer", f'{c["tokens_per_correct"]:.0f}', f'{o["tokens_per_correct"]:.0f}'),
            ("Estimated cost per correct answer", f'${c["cost_per_correct_usd"]:.6f}', f'${o["cost_per_correct_usd"]:.6f}'),
            ("Total estimated cost (successful calls)", f'${c["cost_successful_usd"]:.6f}', f'${o["cost_successful_usd"]:.6f}'),
            ("Latency p50 / p95 interpolated / p95 nearest-rank (s)",
             f'{c["latency_p50_s"]:.1f} / {c["latency_p95_s"]:.1f} / {c["latency_p95_nearest_rank_s"]:.1f}',
             f'{o["latency_p50_s"]:.1f} / {o["latency_p95_s"]:.1f} / {o["latency_p95_nearest_rank_s"]:.1f}'),
            ("Latency excl. waits p50 / p95 (s)", f'{c["latency_excl_wait_p50_s"]:.1f} / {c["latency_excl_wait_p95_s"]:.1f}',
             f'{o["latency_excl_wait_p50_s"]:.1f} / {o["latency_excl_wait_p95_s"]:.1f}'),
            ("Provider waiting (total s)", f'{c["provider_wait_total_s"]:.1f}', f'{o["provider_wait_total_s"]:.1f}')]
    lines = ["| Metric | Control | Selective recovery |", "|---|---|---|"] + [f"| {a} | {b} | {d} |" for a, b, d in rows]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
