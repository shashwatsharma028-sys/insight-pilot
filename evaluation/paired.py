"""
Fair paired comparison of two benchmark results (A = baseline, B = candidate).

Refuses to compare (NotComparable) unless:
- both results describe the same workload (identical fingerprint: datasets, scenarios, expected values,
  harness version, retry budget);
- they ran the same datasets with the same number of repetitions;
- their token-provenance classes are identical (an estimate is never compared with a provider count).

Then:
- Correctness is compared metric by metric (answers, quality, tasks, failures blocked, charts, reports,
  V1-snapshot behaviour). Any decrease is a REGRESSION, and no improvement may then be claimed.
- Latency is paired: run wall time by (dataset, repetition), and sandbox execution by
  (dataset, task, attempt, repetition). For each metric:
  - n pairs and the medians of A and B;
  - the median paired difference (B - A);
  - a deterministic bootstrap 95% CI of that median (seeded);
  - an exact two-sided sign test.
  Verdict: "faster" or "slower" only if the CI excludes 0 AND the sign test p < alpha AND there are at least
  min_pairs pairs; otherwise "no significant difference" or "insufficient pairs".
- LLM calls and tokens are compared per provenance class; mock token differences are labelled as
  estimates and never as cost.
"""

import math
import random

from evaluation import metrics


class NotComparable(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _classes(result):
    return sorted(result["summary"]["tokens_per_benchmark_pass"]["classes"])


def comparability(a, b):
    p = []
    if a["workload"]["sha256"] != b["workload"]["sha256"]:
        p.append("different workload fingerprints: the results do not measure the same work")
    if sorted({r["dataset"] for r in a["runs"]}) != sorted({r["dataset"] for r in b["runs"]}):
        p.append("different dataset sets")
    if a["summary"]["repetitions"] != b["summary"]["repetitions"]:
        p.append(f"different repetition counts ({a['summary']['repetitions']} vs {b['summary']['repetitions']})")
    if _classes(a) != _classes(b):
        p.append(f"different token provenance classes ({_classes(a)} vs {_classes(b)}): not comparable")
    return p


CORRECTNESS = [
    ("answers_correct", lambda s: s["answers"]["correct"]),
    ("quality_correct", lambda s: s["quality"]["correct"]),
    ("tasks_completed_of_expected", lambda s: s["tasks"]["completed_of_expected"]),
    ("expected_failures_observed", lambda s: s["tasks"]["expected_failures_observed"]),
    ("task_status_as_expected", lambda s: s["tasks"]["status_as_expected"]),
    ("charts_valid", lambda s: s["charts"]["valid"]),
    ("reports_valid", lambda s: s["reports"]["valid"]),
    ("deterministic_datasets", lambda s: sum(s["deterministic_across_repetitions"].values())),
    ("run_errors_absent", lambda s: -len(s["errors"])),
]


def sign_test(diffs):
    """Exact two-sided sign test on non-zero differences; p = 1.0 when there are none."""
    nz = [d for d in diffs if d != 0]
    n = len(nz)
    if n == 0:
        return 1.0
    k = min(sum(d > 0 for d in nz), sum(d < 0 for d in nz))
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _median(xs):
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def bootstrap_ci(diffs, resamples=10000, seed=20261122, level=0.95):
    rng = random.Random(seed)
    n = len(diffs)
    meds = sorted(_median([diffs[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
    lo = meds[int(math.floor((1 - level) / 2 * resamples))]
    hi = meds[min(resamples - 1, int(math.ceil((1 + level) / 2 * resamples)) - 1)]
    return lo, hi


def paired_latency(pairs, *, min_pairs=5, alpha=0.05, resamples=10000, seed=20261122):
    if not pairs:
        return {"n": 0, "verdict": "insufficient pairs"}
    a = [x for x, _ in pairs]
    b = [y for _, y in pairs]
    diffs = [y - x for x, y in pairs]
    out = {"n": len(pairs), "median_a_ms": round(_median(a) * 1000, 2), "median_b_ms": round(_median(b) * 1000, 2),
           "median_diff_ms": round(_median(diffs) * 1000, 2),
           "relative_change_of_medians": round((_median(b) - _median(a)) / _median(a), 4) if _median(a) else None}
    if len(pairs) < min_pairs:
        out["verdict"] = "insufficient pairs"
        return out
    lo, hi = bootstrap_ci(diffs, resamples=resamples, seed=seed)
    p = sign_test(diffs)
    out.update(ci95_median_diff_ms=[round(lo * 1000, 2), round(hi * 1000, 2)], sign_test_p=round(p, 6))
    if hi < 0 and p < alpha:
        out["verdict"] = "faster"
    elif lo > 0 and p < alpha:
        out["verdict"] = "slower"
    else:
        out["verdict"] = "no significant difference"
    return out


def _run_pairs(a, b):
    key = lambda r: (r["dataset"], r["repetition"])  # noqa: E731
    ia, ib = {key(r): r for r in a["runs"]}, {key(r): r for r in b["runs"]}
    return [(ia[k]["wall_seconds"], ib[k]["wall_seconds"]) for k in sorted(set(ia) & set(ib))]


def _exec_pairs(a, b):
    def index(result):
        out = {}
        for r in result["runs"]:
            seen = {}
            for e in r["executions"]:
                n = seen[e["task_id"]] = seen.get(e["task_id"], -1) + 1
                if not e["blocked_by_safety_scan"]:
                    out[(r["dataset"], e["task_id"], n, r["repetition"])] = e["seconds"]
        return out
    ia, ib = index(a), index(b)
    return [(ia[k], ib[k]) for k in sorted(set(ia) & set(ib))], sorted(set(ia) ^ set(ib))


def compare(a, b, **stats_kwargs):
    problems = comparability(a, b)
    if problems:
        raise NotComparable(problems)
    sa, sb = a["summary"], b["summary"]
    correctness = {}
    for name, get in CORRECTNESS:
        va, vb = get(sa), get(sb)
        correctness[name] = {"a": va, "b": vb, "delta": vb - va, "regression": vb < va}
    regressions = sorted(n for n, c in correctness.items() if c["regression"])
    exec_pairs, unpaired = _exec_pairs(a, b)
    latency = {"pipeline_wall": paired_latency(_run_pairs(a, b), **stats_kwargs),
               "sandbox_execution": paired_latency(exec_pairs, **stats_kwargs),
               "unpaired_executions": len(unpaired)}
    calls = {"a": sa["llm_calls_per_benchmark_pass"], "b": sb["llm_calls_per_benchmark_pass"],
             "delta": sb["llm_calls_per_benchmark_pass"] - sa["llm_calls_per_benchmark_pass"],
             "by_skill": {s: {"a": sa["llm_calls_by_skill"].get(s, 0), "b": sb["llm_calls_by_skill"].get(s, 0)}
                          for s in sorted(set(sa["llm_calls_by_skill"]) | set(sb["llm_calls_by_skill"]))}}
    tokens = {}
    for cls in _classes(a):
        ta, tb = sa["tokens_per_benchmark_pass"]["classes"][cls], sb["tokens_per_benchmark_pass"]["classes"][cls]
        tokens[cls] = {"input_delta": tb["input_tokens"] - ta["input_tokens"],
                       "output_delta": tb["output_tokens"] - ta["output_tokens"],
                       "a": {"input": ta["input_tokens"], "output": ta["output_tokens"]},
                       "b": {"input": tb["input_tokens"], "output": tb["output_tokens"]},
                       "note": ("character estimates of scripted prompts: a change in PROMPT SIZE, not cost"
                                if cls.startswith("mock") else "provider-reported" if "provider_reported" in cls
                                else "estimate")}
    claims = []
    if not regressions:
        for metric, res in latency.items():
            if isinstance(res, dict) and res.get("verdict") == "faster":
                claims.append(f"{metric}: median {res['median_diff_ms']} ms per pair "
                              f"(95% CI {res['ci95_median_diff_ms']}), sign test p={res['sign_test_p']}")
    return {"schema": "insightpilot.paired-comparison/1", "a": a["variant"], "b": b["variant"],
            "workload": a["workload"]["sha256"],
            "environments_identical": metrics.canonical(a["environment"] | {"git_commit": None, "git_dirty": None})
            == metrics.canonical(b["environment"] | {"git_commit": None, "git_dirty": None}),
            "correctness": correctness, "regressions": regressions, "latency": latency, "llm_calls": calls,
            "tokens": tokens,
            "verdict": "REGRESSION" if regressions else ("improvement claims: " + "; ".join(claims) if claims
                                                         else "no significant improvement"),
            "claims": claims}


def markdown(cmp) -> str:
    lines = [f"# Paired comparison: `{cmp['a']}` (A) vs `{cmp['b']}` (B)", "",
             f"- workload `{cmp['workload']}` (identical for both)",
             f"- environments identical (ignoring commit): {cmp['environments_identical']}",
             f"- **verdict: {cmp['verdict']}**", "", "| Correctness metric | A | B | Regression |", "|---|---|---|---|"]
    for name, c in cmp["correctness"].items():
        lines.append(f"| {name} | {c['a']} | {c['b']} | {'YES' if c['regression'] else 'no'} |")
    lines += ["", "| Latency (paired) | n | median A ms | median B ms | median diff ms | 95% CI | sign p | verdict |",
              "|---|---|---|---|---|---|---|---|"]
    for name in ("pipeline_wall", "sandbox_execution"):
        x = cmp["latency"][name]
        lines.append(f"| {name} | {x['n']} | {x.get('median_a_ms')} | {x.get('median_b_ms')} | "
                     f"{x.get('median_diff_ms')} | {x.get('ci95_median_diff_ms')} | {x.get('sign_test_p')} | "
                     f"{x['verdict']} |")
    lines += ["", f"- LLM calls: A {cmp['llm_calls']['a']}, B {cmp['llm_calls']['b']}"]
    for cls, t in cmp["tokens"].items():
        lines.append(f"- tokens `{cls}`: input {t['a']['input']} -> {t['b']['input']}, output {t['a']['output']} -> "
                     f"{t['b']['output']} ({t['note']})")
    return "\n".join(lines) + "\n"
