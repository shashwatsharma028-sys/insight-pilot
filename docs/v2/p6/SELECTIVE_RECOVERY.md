# P6 optimisation 1: selective task recovery (engineering record)

**Status: retain.** The feature is opt-in: `limits["selective_plan_recovery"] = True`. It is off by default; when it
is off, the code path and run state are unchanged.
**Code:** `multi_agent/supervisor.py`, `multi_agent/contracts.py: triage_plan`, `evaluation/live.py` (commit `9a65e52c`).
**Evidence:** `recovery_experiment/runs.jsonl`. The baseline (`baseline_attempt3/`) is unchanged
(sha256 `89177de0…a2d4`).
**Cost:** every cost is an ESTIMATE, provider-reported tokens × list prices, not billing. The experiment hit Gemini's
**free-tier** daily quota (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`), so the key appears to be on the free tier.

## 1. Original bottleneck (measured)

In the 40-run live baseline, **6 of 20 multi-agent runs (30%) ended `plan_invalid`** and produced no answers. That cost
**34 of the 46 hidden answers multi-agent missed (74%)**.

## 2. How it was identified

1. The baseline only recorded counts, so the harness was changed to record the exact validation problems and
   rejected plan payloads for each run. Grading is unchanged.
2. A separate 12-run diagnostic sample on the unchanged code gave the causes. Of 8 rejected plans, 6 had a
   non-string `value` on an equality claim, for example JSON `true` where the CSV text `"True"` is required.
3. **In 7 of those 8, only one task was invalid,** but plan validation was all-or-nothing: one bad claim discarded
   the whole plan.

## 3. Implementation (no new agents, no new infrastructure, prompts unchanged)

- **Triage:** `contracts.triage_plan` validates each task with the unchanged strict `validate_plan`. It then re-checks
  each accepted task against the merged plan (duplicate IDs, duplicate claim keys, the task limit). Nothing is coerced
  or repaired.
- **Keep and repair:** the Supervisor keeps the valid tasks. It sends only the invalid ones back with their exact
  errors, through the same planning prompt template and within the same `max_plan_attempts` (2), so there are no
  extra planner calls.
- **Repair rules:** a repair reply may only contain the tasks being repaired.
- **Abandonment:** an unrepairable task is **abandoned and reported**: in the approval request (the human sees it),
  in `final["plan_recovery"]`, and as `completed_with_failures`. It is never silently dropped, and its questions are
  graded as missing.
- **Unchanged:** the merged plan is re-validated as a whole before human approval. Independent verification is
  unchanged.
- **Offline tests (34):**
  - replay of every recorded rejection, where the validator reproduces each recorded message verbatim;
  - abandonment, provider errors during repair, and merged-plan checks;
  - that a fully valid plan is identical with the flag on or off;
  - that verification still runs, and a hidden-answer leak test on repair prompts;
  - the experiment runner itself.
  - **Mutation checks:** 9 of 9 injected defects were caught.
- **Full offline regression:** 1955 passed, 13 xfailed.

## 4. Experiment design

- **Workload:** the baseline's 20 multi-agent `(dataset, rep)` slots, in the baseline's seeded order (seed 20261124).
- **Pairing:** each slot runs the optimised variant and an **adjacent matched control**, which is the baseline code
  path. The order within each pair is seeded and random, so provider load affects both alike.
- **Same as baseline:** model gemini-3.5-flash-lite, temperature 0, 2048 max output tokens, 7 s pacing, HTTP 429
  handling, HTTP 503 handling (not retried), prompts and op engine.
- **Budget:** a separate ledger, capped at $0.25.
- **Completed:** **18 of 20 slots as matched pairs.**
  - Slot 18 has a control only; slot 19 never started.
  - The run stopped, as required, on a genuine **daily-quota** HTTP 429: per-day free-tier quota of 500 requests,
    `retryDelay` 69,145 s.
  - The stopped run is excluded. No V1 executor was reached (tripwire hits 0).

## 5. Results (18 matched pairs, 104 hidden answers per arm)

Required-task completion uses the **corrected denominator**: every required task of every run counts, including
`plan_invalid` runs and abandoned tasks.

| Metric | Control (recovery off) | **Selective recovery** | Baseline, same 18 slots (earlier date) |
|---|---|---|---|
| Hidden-answer accuracy | 47/104 = 45.2% | **77/104 = 74.0%** | 69/104 = 66.3% |
| Complete planner failures (`plan_invalid`) | 9/18 = 50.0% | **2/18 = 11.1%** | 4/18 = 22.2% |
| `plan_invalid` causes | 5 both plans rejected, 3 one HTTP 503 + one rejected, 1 both 503 | **2 both 503 (no plan received)** | not recorded |
| Required-task completion | 26/54 = 48.1% | **43/54 = 79.6%** | 40/54 = 74.1% |
| Tasks recovered / abandoned | 0 / 0 | **11 recovered** (in 11 runs) / 5 abandoned (all still invalid after repair) | n/a |
| Incorrect claims (claim precision) | 3 (0.951) | 9 (0.914) | 8 |
| Task-level agent retries | 6 | 2 | 6 |
| Planner calls | 31 | 31 | 32 |
| LLM calls (successful) | 61 (53) | 76 (68) | 74 (71) |
| Provider failures | 8 (HTTP 503 × 8) | 8 (HTTP 503 × 7, timeout × 1) | 3 |
| Agent validation rejections | 17 | 17 | not recorded |
| Successful provider-reported tokens | 44,402 | 48,041 (+8.2%) | 53,890 |
| Provider-reported cost | $0.0489 | $0.0503 (+2.9%) | $0.0595 |
| Worst-case charges for failed attempts | $0.0457 | $0.0472 | $0.0173 |
| **Provider-reported cost per correct hidden answer** | $0.00104 | **$0.00065 (−37%)** | $0.00086 |
| **Provider-reported tokens per correct hidden answer** | 945 | **624 (−34%)** | 781 |
| Latency p50 / p95, wall clock | 27.1 s / 163.1 s | 29.1 s / 147.4 s | 32.0 s / 41.7 s |
| Latency p50 / p95, excluding pacing | 6.8 s / 150.7 s | 7.2 s / 120.9 s | 6.1 s / 25.2 s |

- **Accuracy:** **+30 hidden answers (+28.8 points).**
  - Per run, +1.67 answers; 95% bootstrap CI [+0.50, +2.83].
  - Of the 18 pairs, recovery won 9, lost 2 and tied 7; the sign test, which ignores ties, gives p = 0.065.
- **Latency:** no significant difference. The paired median difference excluding waits is +1.36 s, 95% CI
  [−2.1 s, +7.5 s], p = 0.48.
- **Provider load:** provider failures were identical in the two arms (8 each), which confirms the matched control
  saw the same conditions.
- **Why the control is worse than the original baseline:** provider load was higher on this date. The comparison
  against the earlier-date baseline (66.3% → 74.0%) is weaker evidence and only shown for context.

## 6. Trade-offs and limitations

- **More answered also means more wrong answers exposed.** 9 incorrect claims vs 3; precision 0.914 vs 0.951. Recovered
  ecommerce weekday/weekend tasks now report values, but some are semantically wrong. These are shared semantic errors,
  for example `top_month_by_revenue`, not recovery errors.
- **Repairs are not always possible.** `att_t3` (`gps_mismatch_count`) was still invalid after repair in 4 of 5
  abandonments. The model repeats the non-string `value` even when given the exact error.
- **It cannot help when no plan arrives.** Both remaining `plan_invalid` runs were double HTTP 503s.
- **Small sample:** 18 pairs, not the planned 20, on one model and day. The run-level sign test is p = 0.065.
  The answer-level effect and its bootstrap CI exclude zero.
- **Slightly more LLM calls per run** (4.2 vs 3.4), because recovered tasks go on to need insight calls.

## 7. Recommendation and next optimisation

- **Retain** selective task recovery. Enable it for V2 multi-agent runs; keep it off by default for the exam
  workflow.
- **Next highest impact: retry transient provider failures (HTTP 5xx and timeouts),** recorded as provider retries,
  not plan attempts, and applied identically to both architectures.
  - In this experiment, double HTTP 503s caused **every** remaining complete planner failure: 12 of 104 answers.
  - HTTP 503s hit 7–8 calls per 18 runs in each arm.
- **After that, state in the op catalogue that `value` is the CSV cell text as a JSON string.** This targets the 5
  abandoned tasks (6 answers).
- **Planning constraint:** the free-tier quota is 500 requests per day per model, so a paired 40-run experiment
  (about 160 calls) must fit within one quota day.

## 8. Resume bullet and interview explanation (live, provider-reported data only)

- **Resume bullet:** *Diagnosed a 30% complete-failure rate in a LangGraph multi-agent analytics pipeline (all-or-nothing
  plan validation) from recorded validation traces, and built selective task recovery that repairs only invalid
  tasks within the same LLM-call budget. In a matched live A/B on Gemini (18 pairs), this raised hidden-answer
  accuracy from 45% to 74%, cut complete planner failures from 50% to 11% and lowered cost per correct answer by 37%,
  with no significant latency change.*
- **Interview explanation:** the planner proposes several analysis tasks at once. If any single claim was malformed,
  for example a boolean where text was required, the whole plan was thrown away and the run produced nothing. I
  recorded the exact validation errors, found that usually only one task of three was wrong, and changed the
  Supervisor to keep the valid tasks and ask the model to fix only the broken ones. Strict validation, human approval
  and independent verification are unchanged, and anything that still can't be fixed is reported as failed rather
  than hidden. In a paired live test under identical provider conditions, correct answers rose by 30 of 104.
