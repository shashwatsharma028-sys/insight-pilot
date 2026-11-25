# InsightPilot V2: final engineering evidence package

**Branch:** `feature/v2-p6-final-optimization`, from the UI freeze `b42cb611`.
**Scope:** offline only. Every number below is recomputed from committed raw records by
`scripts/p6/evidence_package.py`, and `tests/v2/p6/test_final_optimization_p6.py` checks each one.
**Model:** gemini-3.5-flash-lite through the OpenAI-compatible endpoint, on a free-tier key.
**Cost:** every cost is an **estimate** (provider-reported tokens × list prices), **not billing**.

## 1. Measured results

### A. Selective task recovery (live matched A/B, `recovery_experiment/runs.jsonl`)

- **Design:** 37 run records, giving 18 complete matched pairs. One unmatched control run (student, rep 4, 0/6) is
  excluded from every comparison.
- **Fixed in both arms:** model, temperature 0, 2048 output tokens, 7 s pacing and HTTP 503 handling.
- **Arms:** control = recovery off; optimised = recovery on.

| Metric (18 matched pairs) | Control | Selective recovery | Absolute | Relative |
|---|---|---|---|---|
| Hidden-answer accuracy | 47/104 = 45.2% | **77/104 = 74.0%** | **+28.8 pp** | +63.8% |
| Claim precision (correct / answered) | 58/61 = 95.1% | 96/105 = 91.4% | −3.7 pp | −3.8% |
| Complete planner failure rate | 9/18 = 50.0% | **2/18 = 11.1%** | **−38.9 pp** | −77.8% |
| Required-task completion (every task counts) | 26/54 = 48.1% | **43/54 = 79.6%** | **+31.5 pp** | +65.4% |
| Provider-reported tokens in / out (successful calls) | 28,241 / 16,161 | 31,726 / 16,315 | +3,639 total | +8.2% |
| Tokens per correct hidden answer | 945 | **624** | −321 | **−34.0%** |
| Estimated cost of successful calls | $0.048875 | $0.050305 | +$0.001430 | +2.9% |
| Estimated cost per correct hidden answer | $0.001040 | **$0.000653** | −$0.000387 | **−37.2%** |
| Latency p50 (wall clock) | 27.1 s | 29.1 s | +2.0 s | +7.3% |
| Latency p95: interpolated / nearest-rank* | 107.5 / 163.1 s | 76.6 / 147.4 s | | |
| Latency p50 excluding waits | 6.8 s | 7.2 s | +0.4 s | +5.6% |
| Provider waiting (pacing + rate limit), total | 285.0 s | 350.7 s | +65.7 s | |

- **Statistics:** the paired difference is +1.67 hidden answers per run, 95% bootstrap CI [+0.50, +2.83]. Recovery
  won 9 pairs, lost 2 and tied 7; the sign test, which ignores ties, gives p = 0.065.
- **Latency:** paired latency is not significantly different.
- **\* p95 caveat:** the harness's nearest-rank p95 is the **slowest run** when n = 18, so the earlier record's
  163.1 / 147.4 s is the maximum. Both definitions are reported here.
- **Trade-off:** precision falls 3.7 pp, because more answers are now attempted and some of them are semantically
  wrong.

### B. Multi-agent vs single-agent architecture (live, 40 randomised runs, `baseline_attempt3/runs.jsonl`)

| Metric (20 runs per architecture, before selective recovery) | Single agent | Multi-agent |
|---|---|---|
| Hidden-answer accuracy | 77/115 = 67.0% | 69/115 = 60.0% |
| Provider-reported tokens (successful calls) | 92,452 | **56,770 (−38.6%)** |
| Tokens per correct answer | 1,201 | 823 (−31.5%) |
| Successful provider calls | 153 | 73 |
| Latency p50 excluding waits | 13.9 s | **6.1 s** |
| Paired latency excluding waits (20 pairs) | | **−41.5%** median paired difference, 95% CI [−32.9 s, −2.8 s], sign test p = 0.0004 |
| Estimated cost per correct answer | $0.00079 | $0.00091 (+15%; the planner's reasoning adds output tokens priced about 8× input) |

**What this does NOT show:** this baseline is not an accuracy or cost win for multi-agent. That accuracy gap is what
selective recovery (A) then addressed.

### C. Where the time goes (recorded traces; deterministic work timed offline)

| Share of wall time | Recovery experiment (37 runs) | Baseline multi-agent (20 runs) |
|---|---|---|
| Pacing and rate-limit waiting | 45.5% | 73.7% |
| Successful LLM inference | 40.0% | 24.4% |
| Failed provider attempts (HTTP 503 / timeout) | 14.1% | 1.2% |
| Everything else (graph, prompts, trusted computation, verification) | **0.3%** | **0.6%** |

- **Offline timing of the deterministic work:** the real Supervisor with an instant mock model, on the 4 benchmark
  datasets × 5 runs, takes a **median of 85 ms per run**:
  - trusted computation: 30 ms;
  - verification: 10 ms;
  - Supervisor bookkeeping: 9 ms;
  - insight and planning nodes: under 3 ms.
- **Decision:** no deterministic-execution optimisation was implemented. Removing all of it would save under 0.1 s
  of a median 29 s run, so any "latency gain" claimed there would be manufactured.
- **The real levers:**
  - **Pacing:** a quota-protection setting, not a computational cost.
  - **Provider failures:** the bounded transient-retry policy targets these; its live experiment is scheduled.
  - **Model inference itself.**

### D. Guidance token overhead (offline measurement, not a live result)

| Prompt guidance | Characters | Estimated input tokens (3.28 characters per token, measured) |
|---|---|---|
| Schema section (`op_schema_section`) | 1,773 | about 541 |
| Semantic section (`op_semantics_section`) | 1,952 | about 595 |
| Both, as used in the proposed combined A/B | 3,725 | about 1,136 per planner request |
| **Compact candidate** (`scripts/p6/compact_guidance.py`, not wired to any prompt) | **2,464** | **about 751 (−33.9%)** |

- **What the compact form keeps:** every operation, argument, column kind, result description and rule (tested,
  with 15 rule facts checked individually).
- **What isn't known:** its effect on live accuracy. It is a candidate arm for a future comparison, and no
  efficiency claim is made.

## 2. Engineering changes behind each measured result

| Result | Change | Code |
|---|---|---|
| A | Selective task recovery: per-task validation, repair of only the invalid tasks within the same planner-call budget, and abandoned tasks reported, never hidden | `multi_agent/supervisor.py`, `multi_agent/contracts.py: triage_plan` (`9a65e52c`, review fix `d2c089bc`) |
| B | Multi-agent Supervisor with a trusted operation engine, an independent verifier and grounded insights | `multi_agent/*`, `evaluation/live.py` |
| Diagnosis behind A | Exact plan-validation and failure-stage diagnostics: 34 of 46 multi-agent misses traced to all-or-nothing plan validation | `evaluation/live.py: diagnostics`, `question_diagnostics` |
| Evidence | Independent recomputation of every metric from the raw records | `scripts/p6/evidence_package.py` |

**Raw evidence:**
- `docs/v2/p6/recovery_experiment/runs.jsonl`
- `docs/v2/p6/baseline_attempt3/runs.jsonl`, `live_benchmark.json`
- `docs/v2/p6/plan_diagnostic_sample/runs.jsonl`
- the ledgers: 574 recorded provider attempts, $0.9936 estimated in total across all P6 ledgers
  (`~/.local/state/insightpilot/*.jsonl`, outside the repository)

## 3. Proposed final combined benchmark (prepared; NOT run; needs approval and quota)

- **Design:** `--experiment combined_guidance`, the baseline's 20 multi-agent slots with seed 20261124, each paired
  with an adjacent control.
  - **Optimised arm:** recovery, schema guidance and semantic guidance.
  - **Control arm:** recovery only.
- **Held equal:** model, temperature 0, 2048 output tokens, pacing, 429 policy, and the transient-retry setting
  (taken from the provider-resilience decision, identical in both arms).
- **Validity check:** the prompt audits must show 100% / 0% for **both** sections (tested offline).
- **Scope:** it measures the **combination**, not each section's individual contribution.

**Acceptance criteria (fixed now, before any new live data):**
1. **Validity:** both audits are 100% / 0%, and at least 15 matched pairs complete.
2. **Primary:** the paired hidden-answer difference (optimised − control) has a 95% bootstrap CI lower bound > 0.
3. **Secondary, must hold:**
   - claim precision drops by no more than 2 pp;
   - required-task completion drops by no more than 2 pp;
   - estimated cost per correct answer rises by no more than 15% (guidance adds about 1,136 input tokens per planner
     request).
4. **Decision:**
   - **retain** if 1, 2 and 3 all hold;
   - **inconclusive** if 1 and 3 hold, the point estimate is above 0 but the CI includes 0;
   - **reject** otherwise.
   - Matched pairs are reported first; unmatched runs separately.

**Quota and budget:**
- **Quota:** the free tier allows **500 requests per model per day** (resetting at 08:00 UTC).
- **Today's plan:** the scheduled provider-resilience experiment uses about 190 requests (its own estimate,
  including retries). The combined benchmark needs about 40 runs × 4.4 calls, plus about 10% retries and the
  preflight: **about 195 requests**. Together that's about 385 of 500.
- **The rule:** the combined run starts only if the **actual** requests remaining after the provider experiment are
  **at least 230** (195 plus a 35-request margin), and only with approval.
- **Budget:** P6 has spent $0.9936 estimated of the $5 overall cap. The provider experiment is capped at $0.30.
  Proposed cap for the combined run: **$0.30**, on a new ledger. The worst case leaves at least $3.40 of the $5 cap.
- **If quota or approval isn't available today,** the experiment stays **pending**, and the resume uses only the
  measured results in section 1.

## 4. Limitations

- **Small samples:** 18 matched pairs (A) and 20 runs per architecture (B), on one model and one free-tier day each.
  The run-level sign test in A is p = 0.065; the answer-level bootstrap CI excludes zero.
- **Precision trade-off:** selective recovery lowers claim precision by 3.7 pp.
- **Multi-agent costs more per correct answer** in the architecture baseline.
- **Unmeasured live:** schema guidance, semantic guidance, the compact format and provider retries. No accuracy,
  latency or cost claim is made for them.
- **Benchmark only:** results are for 4 benchmark datasets in trusted-ops mode; the synthetic generalisation
  workload is not implemented.
- **Costs are estimates:** the key is free-tier, so actual billing is most likely $0.

## 5. Proposed resume bullets (measured results only) and interview explanations

1. **Diagnosed and fixed the main failure mode of a LangGraph multi-agent analytics system:** selective task
   recovery that repairs only invalid plan tasks within the same LLM-call budget. In a matched live A/B on Gemini
   (18 pairs), hidden-answer accuracy rose **45.2% → 74.0% (+28.8 pp)**, complete planner failures fell
   **50% → 11%**, and cost per correct answer fell **37%**.
   - *Interview:* the planner proposes several tasks at once. One malformed claim, such as a boolean where text was
     required, used to discard the whole plan. I recorded the exact validation errors, saw that usually one task in
     three was wrong, and changed the Supervisor to keep the valid tasks and ask the model to fix only the broken
     one. I measured it against a control running back to back under the same provider conditions. The trade-off is
     a 3.7-point drop in precision, because more questions now get answered.

2. **Benchmarked single-agent vs multi-agent architectures** on 40 randomised live runs with the model, data and
   question wording held fixed: the multi-agent design used **39% fewer tokens** and was **42% faster** (paired
   median, p < 0.001) per analysis.
   - *Interview:* splitting planning from per-task insight meant one planning call instead of a model call per
     question, so fewer tokens and less time. I report it honestly: in that baseline the multi-agent version was less
     accurate and costed more per correct answer, because planner reasoning produces expensive output tokens. That
     finding is what led to bullet 1.

3. **Built evaluation-grade diagnostics** (exact failure traces, per-question failure-stage and semantic-error
   classification, independent metric recomputation). They traced **74% of multi-agent misses (34 of 46)** to
   all-or-nothing plan validation, and **confirmed** the root cause of 9 of 32 historical wrong answers by replaying
   the recorded plans.
   - *Interview:* rather than guess why answers were wrong, I made the harness record what actually ran, re-executed
     it, and labelled every error as confirmed, a hypothesis or unknown. That's what made the fixes evidence-driven.

4. **Ran a cost-controlled live LLM evaluation programme:**
   - **574 recorded provider attempts** within a hard **$5 budget**, at **$0.99 estimated spend**;
   - pre-call budget checks, conservative charging for failed calls, and daily-quota detection;
   - backed by **2,200+ offline tests** and mutation testing (every injected defect caught across the P6 and UI
     phases).
   - *Interview:* each live call passes a pre-call worst-case budget check. Failed calls are charged conservatively,
     and the run stops on real daily-quota errors rather than retrying blindly. Offline, mock providers and
     deliberately injected bugs prove the tests catch regressions.

**Potential impact (UNVERIFIED until the pending live experiments):**
- **Combined guidance:** it targets the confirmed grouping, result-shape and value-type errors.
- **Compact guidance:** it would cut about 385 input tokens per planner request if accuracy holds.
- **Provider retries:** they target the 14% of wall time lost to failed provider attempts.
