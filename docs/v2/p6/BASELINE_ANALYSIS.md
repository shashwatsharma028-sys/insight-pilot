# P6 live baseline (attempt 3): analysis and optimisation priority

**Source:** `baseline_attempt3/` (40 of 40 runs, unchanged), plus `plan_diagnostic_sample/` (12 separate diagnostic runs).
**Model:** gemini-3.5-flash-lite through the OpenAI-compatible endpoint; temperature 0; 2048 max output tokens.
**Design:** 4 datasets × 5 reps × 2 architectures, randomised order with seed 20261124.
**Run settings:** 7 s pacing; HTTP 429 retried (up to 6, Retry-After honoured, charged $0 provisionally); every other failed
attempt charged the worst case. Both architectures use the same model, settings, profile, questions and trusted op engine.
**Cost:** "Cost" means estimated cost: provider-reported tokens × confirmed list prices. It is **not billing**.

Both architectures run in trusted-ops mode: models choose operations from a fixed catalogue, and no model-written code runs.
`single_agent` is the V1-style single agent in trusted-ops mode, not the original unrestricted V1 code-generation pipeline.

## 1. Headline results (all 40 runs, nothing excluded)

| Metric | single_agent | multi_agent |
|---|---|---|
| Hidden-answer accuracy (23 questions × 5 reps = 115) | **77 / 115 = 67.0%** | **69 / 115 = 60.0%** |
| Hidden accuracy, runs whose plan was accepted | 77 / 115 = 67.0% (no plan stage) | 69 / 81 = **85.2%** (14 runs) |
| Runs ending `plan_invalid` | n/a | **6 / 20 (30%)** |
| Task completion rate | 0.667 | 0.952 (accepted plans only; `plan_invalid` runs have no tasks) |
| Incorrect claims reported | 10 | 8 |
| Claim precision | 0.910 | 0.915 |
| Successful LLM calls (failed attempts) | 153 (7) | 73 (5) |
| Agent-level retries | 51 | 6 |
| Engine failures on model-chosen claims | 99 | 0 |
| Provider-reported tokens (in / out / total) | 77,416 / 15,036 / 92,452 | 35,955 / 20,815 / **56,770 (−38.6%)** |
| Provider-reported cost | $0.0608 | $0.0628 (+3.3%) |
| Worst-case charges on failed attempts (not expected to be billed) | $0.0388 | $0.0293 |
| Latency p50 / p95, wall clock | 60.6 s / 151.1 s | 29.4 s / 35.3 s |
| Latency p50 / p95, excluding pacing and rate-limit waits | 13.9 s / 122.6 s | 6.1 s / 13.1 s |
| Paired latency excluding waits (n = 20 pairs) | | −41.5% median paired difference; 95% CI [−32.9 s, −2.8 s]; sign test p = 0.0004 |
| HTTP 429 retries | 0 | 0 |
| HTTP 503 failures | 5 | 5 |
| V1 executor tripwire hits | 0 | 0 |

`multi_agent` is faster and uses fewer tokens, but this baseline does not show it to be more accurate. Its output tokens
are higher (the planner's reasoning), and output costs about 8 times as much as input, so estimated cost is about equal.

**Single-agent long tail (p95 122.6 s excluding waits):** comes from slow provider responses, not pacing.
- **Slow successful calls:** in sales runs 36–38, successful calls took 25–65 s.
- **A timeout:** one call hit the 90.1 s client timeout (no HTTP status).
- **Slow HTTP 503s:** some 503s took up to 43 s to arrive.
Failed attempts are charged the worst case.

## 2. Where accuracy is lost (hidden questions; 5 reps each)

| Dataset | single_agent | multi_agent | Main cause |
|---|---|---|---|
| sales | 20/30 | **30/30** | single: `sales_by_region` and `top_region` missing 5/5 (claim error text not recorded) |
| attendance | 19/25 | 15/25 | multi: 2 `plan_invalid` runs (10 answers); single: `gps_mismatch_count` wrong 3/5 |
| student | 23/30 | 15/30 | multi: 2 `plan_invalid` runs (12 answers) and `pass_rate` wrong 3/3; single: `pass_rate` missing 5/5 |
| ecommerce | 15/30 | 9/30 | both: `top_month_by_revenue` wrong in every run that answered it; weekday/weekend means missing; multi: 2 `plan_invalid` runs |

**Multi-agent losses:** 46 hidden answers. **34 of them (74%)** come from the 6 `plan_invalid` runs. The other 12 are
semantic errors that both architectures share.

## 3. Root cause of `plan_invalid`

The baseline harness only recorded counts. From `578ce5ec` on, each run records the exact validation problems
(`diagnostics`). A separate 12-run diagnostic sample (same code path and settings, separate ledger) gives these causes for
rejected plans:

| Rejected plans (13 plans returned; 9 planner calls got HTTP 503) | Count |
|---|---|
| A non-string `value` on an equality claim (`pass_rate`, `mean_revenue_weekday` / `weekend`, `gps_mismatch_count`): the model writes JSON `true` / `1` where the CSV text is required | **6 of 8** |
| `args` not an object (`row_count`) | 1 |
| Tasks missing `title` | 1 |
| Accepted | 5 |

Two compounding mechanisms:
1. **All-or-nothing plan validation:** one invalid claim rejects the whole plan. In 7 of the 8 rejected plans, only one
   task was invalid.
2. **Transient provider failures use up plan attempts:** an HTTP 503 ("model is experiencing high demand") counts as a
   failed plan attempt. In the baseline, 3 of the 6 `plan_invalid` runs were one 503 plus one rejected plan. In the
   diagnostic sample, 9 of 22 planner calls got 503.

The same type error is the single agent's documented pilot failure (`True` instead of `"True"`). Its run records do not
keep claim error text, so for its `pass_rate` misses this is consistent with, not proven by, this evidence.

## 4. Optimisation priority (by measured impact)

| # | Optimisation | Measured opportunity | Affects |
|---|---|---|---|
| 1 | **Selective task recovery:** validate each task separately, keep the valid ones, re-plan only the invalid tasks with their exact errors, and if the re-plan is still invalid, drop those tasks and report them as failed (never hidden). Behind a flag; off by default. | Up to 34 of 115 multi-agent hidden answers (the `plan_invalid` runs); in 7 of 8 rejected plans, only one task was invalid | multi_agent |
| 2 | Transient-provider (HTTP 5xx) retries with backoff, recorded as provider retries and not as plan attempts, applied to **both** architectures | 10 failed attempts in the baseline (each charged the worst case); 3 of 6 `plan_invalid` runs | both (changes shared run conditions) |
| 3 | State in the op catalogue that `value` is the CSV cell text as a JSON string | 6 of 8 rejected plans; likely also single-agent `pass_rate` / weekday misses | both (prompt change) |
| 4 | Shared semantic errors (`top_month_by_revenue`, `pass_rate` value choice) | 5–8 answers | both |

Each optimisation must be evaluated alone against this baseline: same seed schedule, model and settings, and at least
5 paired reps. The baseline is not changed retrospectively.
