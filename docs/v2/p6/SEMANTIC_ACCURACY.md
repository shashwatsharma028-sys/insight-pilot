# P6 optimisation 4: evidence-driven semantic accuracy (engineering record; offline only)

**Branch:** `feature/v2-p6-semantic-accuracy`, from the schema-aware checkpoint `bb2847d9`, in its own worktree.
**Status:** implemented and validated offline. **No live call has been made.** Mock-provider tests show the code
works as designed, not that a real model reasons better.

## 1. The problem

The verifier recomputes **the operation the planner chose**. A plan can therefore be structurally valid and
correctly computed, and still answer a **different question** than the one asked. This change looks for such
semantic errors in the recorded evidence, corrects the ones whose mechanism is confirmed, and adds diagnostics so
future runs can tell the rest apart.

## 2. Evidence: every incorrect answer recorded so far (32)

**Sources:** `baseline_attempt3`, `baseline_attempt2_STOPPED`, `plan_diagnostic_sample` and `recovery_experiment`.

**What counts as "confirmed":**
- the spec that actually ran was recorded, which is the case for tasks kept at triage in selective-recovery runs;
- re-running it with the trusted engine reproduces the incorrect value;
- and that matches the run's recorded grade.

| Question | Incorrect | Confirmed | Mechanism (confirmed cases) | Unconfirmed cases |
|---|---|---|---|---|
| `top_month_by_revenue` | 13 | **4** | `argmax_group_sum` **by `month`**: a text column of month **names** with no year, over 2 years of data. It returns `"March"` (both Marches merged); the question asks for a calendar month `YYYY-MM` (reference: `date` with `by_prefix` 7 → `"2023-03"`) | 9 (baseline multi 3, single 5; recovery control 1): **plausible hypothesis**, because all 11 recorded plans for this question, in both arms, used `month` |
| `mean_revenue_weekday` / `weekend` | 8 | **4** | `group_mean` by `is_weekend` returns a **mapping** `{"False": 1234.60…, "True": 1028.05…}` where the question asks for **one** filtered mean (reference: `mean_where_equals`). The right numbers are inside the mapping, but the claim has the wrong shape | 4 (baseline): plausible hypothesis (5 recorded plans used `group_mean`) |
| `pass_rate` | 6 | **1** | the lowercase string `"true"` matches no row (the CSV holds `True`), so the rate is 0 | 5: hypothesis only (no accepted spec recorded) |
| `gps_mismatch_count` | 5 | **0** | — | **unknown**: no accepted spec for this question was ever recorded; the 11 recorded attempts were all rejected JSON booleans |

**Totals:** 9 confirmed, 23 unrecorded. The unrecorded 23 are hypotheses, not findings.

**Category mapping:**

| Requested category | Finding |
|---|---|
| Grouping granularity / column selection | **confirmed** (`top_month`) |
| Operation selection / aggregation shape | **confirmed** (weekday/weekend) |
| Value representation | **confirmed**, 1 case; **already targeted by the schema-aware branch** ("exact capitalisation"), so **not duplicated here** |
| Filter interpretation | no confirmed case (`gps_mismatch_count` cause unknown) |
| Result communication | **not observable**: insight text is not graded, and the multi-agent grounding check only blocks unsupported numbers |

## 3. Diagnostic gaps found, and what was added (evaluation-only)

**What was missing:** the harness did not record:
- the accepted plan (only rejected plans, from `578ce5ec`);
- the specs the single agent computed;
- the values a run reported;
- the stage at which a missing answer was lost.

That is why 23 of the 32 incorrect answers can't be explained.

**Added in `evaluation/live.py`. Nothing here is passed to an agent, used for grading, or changes a prompt, model
setting or computation:**
- **multi-agent `diagnostics`:**
  - `accepted_claim_specs`: task id, key, op and args of the plan that was approved and run;
  - `reported_values`: the accepted or review-only values;
  - `verification`: each task's status, attempts, outcome history and verified keys.
- **single-agent `diagnostics`:** `claim_specs` (the specs it computed) and `reported_values`.
- **per run, `question_diagnostics` (computed after grading):**
  - for each question: `correct`; `missing` with a stage (`no_plan`, `not_planned`, `abandoned`, `compute_error`,
    `not_accepted`); or `incorrect` with a semantic category (`result_shape`, `operation`, `grouping`, `column`,
    `value_representation`, `filter_value`, `same_spec`, `spec_unrecorded`);
  - the category is found by comparing the spec that ran with the reviewed reference spec, which is used only here
    and by the offline mock.
- **summaries:** `semantic_errors_by_category`, `missing_by_stage` and `semantic_error_rate` for hidden questions.
- **Safety bounds:**
  - values are recorded only for the 4 controlled benchmark datasets;
  - every value and argument is clipped to 400 characters;
  - nothing is logged outside the evaluation records, and no credentials or prompts are logged.

## 4. The correction (smallest general change, opt-in, independent of the schema guidance)

**Flag:** `limits["semantic_planner"]` (multi-agent) and `semantic_guidance=True` (single agent). It is off by
default and independent of the schema-aware flag; either, both or neither can be on.

**When on, every plan request (including repair requests) and every single-agent claims prompt also includes
`prompts.op_semantics_section()`:**
- **`CLAIM OP RESULTS JSON`** (`claims.OP_RESULTS`): what each op returns, for example `"group_mean": "a mapping
  {group: mean of column}, one entry per group"` or `"mean_where_equals": "one number: the mean of column over the
  rows whose where_column cell is exactly value"`. The keys are tested to equal `OPS`, and every description is
  tested against **both** the trusted engine and the independent verifier on synthetic data.
- **`CLAIM SEMANTICS`** (five rules):
  1. pick the op whose result shape matches the question;
  2. use a `*_where_equals` op, not a `group_*` op, for a value restricted to one filter value;
  3. groups are the exact cell text or its prefix, and ties go to the first group in sorted order;
  4. every cell of a `date` column starts with `YYYY-MM-DD`, which is the date kind's own definition
     (`dataset._DATE`), so `by_prefix` 7 gives the calendar month and 4 the year;
  5. a text column naming a period without its year merges that period across years; for a calendar period, group
     a date column with `by_prefix`.

**What it does not do:**
- no dataset, column, question key or value is named;
- the section is one constant for every dataset;
- user intent is not rewritten, and nothing is coerced;
- validators, engine, verifier, `OPS` and all prompts with the flag off are unchanged (AST- and sha256-tested);
- no extra LLM call, agent or dependency.

**Overhead (measured, at 3.28 characters per token):**
- **semantics section:** 1,952 characters, about **595 input tokens** per planner or claims request. That's +65%
  to +72% on an initial planner prompt (repair prompt 3,577 → 5,529 characters), about $0.00018 per request, or
  about +$0.00031 per run (roughly +11% of provider-reported cost per run).
- **with the schema section as well:** about 1,136 tokens; the initial planner prompt grows +123% to +137%.

## 5. Alternatives considered

| Option | Decision |
|---|---|
| A rule "for YYYY-MM use date + by_prefix 7" tied to these questions | Rejected: benchmark-specific |
| Rewriting `group_mean` into `mean_where_equals` or picking the mapping entry | Rejected: rewrites intent and hides the planner's choice |
| A `month` / period operation in the engine | Rejected: a new op and verifier change; `by_prefix` already expresses it |
| Cell-value examples in the profile | Rejected: the profile has no values by design, and examples risk leaking answers |
| A second "critic" LLM call to check semantics | Rejected: an extra call and a new agent (not approved) |
| Op result shapes in the validator (reject mapping claims) | Rejected: a mapping is legitimate when every group is asked for |

## 6. Offline validation

**Results (offline, mock provider):**
- this test file: 55 passed;
- **full offline regression: 2072 passed, 13 xfailed, 0 failed**;
- Streamlit UI smoke and launcher tests: 53 passed;
- headless `streamlit run ui/app.py`: HTTP 200;
- offline dry run (`--experiment semantic_planner --schema-aware-guidance`): 20 pairs, exit 0;
- observed audit in the dry run: semantics section in 20 of 20 semantic-arm and 0 of 20 control planner prompts;
  schema section in 20 of 20 in both arms (held equal).
- **Test file:** `tests/v2/p6/test_semantic_accuracy_p6.py`, 55 tests:
  - op result descriptions checked against engine **and** verifier on a **novel synthetic** two-year shop dataset;
  - exact-text matching; date prefixes; tie order;
  - both confirmed mechanisms reproduced on novel data, with structurally valid plans giving wrong answers;
  - the semantic classifier;
  - the two sections independently switchable (4 combinations);
  - schema-only prompts byte-identical to `bb2847d9`;
  - no hidden-answer leakage and no dataset-specific content;
  - validators, engine, verifier and `OPS` unchanged;
  - diagnostics correct, not changing grading, and never reaching a prompt;
  - values only for benchmark datasets;
  - selective-recovery repairs carry the section;
  - an offline matched experiment for both architectures with observed per-arm audits, and a guard against holding
    the isolated variable constant.
- **Mutation checks:** 11 of 11 caught.

## 7. Proposed live evaluation (needs approval, quota and budget; not run)

**A. Primary matched experiment:** `--experiment semantic_planner --arch multi_agent`
- the baseline's 20 multi-agent slots, seed 20261124, each with an adjacent matched control;
- **held equal:** selective recovery on; schema-aware guidance and the provider-retry policy set **once for both
  arms** (`--schema-aware-guidance` and `--transient-retries`), according to their own experiment decisions;
- **validity:** the audit must show 100% of semantic-arm planner prompts with the section and 0% of control prompts;
- **size and cost:** about 190 requests, a proposed $0.25 ledger cap, and its own quota day.

**B. Generalisation check, required before any claim:**
- a small, seeded **synthetic workload** of 2–3 new datasets with novel questions (calendar-year or quarter
  aggregation, filtered means, filtered rates, top group);
- golden answers computed by the independent verifier from reviewed reference specs;
- run as the same matched design;
- **not implemented yet:** it needs a small harness extension to accept a workload other than the 4 benchmark
  datasets. The synthetic data builder in the tests is the starting point.

**Recorded, per arm, matched pairs first and unmatched runs separately:**
- hidden-answer accuracy; claim precision; required-task completion; incorrect claims;
- semantic error rate by category; missing answers by stage;
- repair frequency;
- total input and output tokens; LLM calls; estimated cost per correct answer (worst-case charges for failed
  attempts shown separately);
- total wall-clock latency and provider waiting time.

**Acceptance criteria (set before any run):** retain only if all of these hold:
1. confirmed-category errors (`grouping` + `result_shape`) fall in A **and** in B;
2. hidden accuracy does not drop in either (95% bootstrap CI lower bound of the paired difference > −1 answer per
   run);
3. no new error category rises by more than 2 answers in total;
4. cost per correct answer is not worse by more than 15%, given the +595-token overhead.

Otherwise, keep the diagnostics and reject or revise the guidance.

## 8. Limitations

- **Small evidence base:** 9 confirmed cases, in one dataset and 2 question types plus one value case. The other 23
  incorrect answers have no recorded spec.
- **Insights not checked for semantics:** result communication in insight text is not graded.
- **Cost:** the guidance is long (+595 tokens), and both sections together more than double the planner prompt.
- **Not a guarantee:** guidance can reduce errors but not enforce them; a model can still ignore it.
- **Generalisation run not yet possible:** the live generalisation workload needs the harness extension described
  above.

## 9. Rollback

Leave `semantic_planner` / `semantic_guidance` off (the default), or revert the commit on this branch. The
diagnostics are evaluation-only and can stay.

## 10. Interview explanation (plain language)

The system's checker recomputes whatever calculation the AI planner chose, so a calculation can be perfectly
correct and still answer the wrong question. I found two such cases in recorded runs:
- **Monthly revenue:** asked for the best calendar month, the planner grouped by a month-**name** column, which
  merged March of both years into one total.
- **Weekday revenue:** asked for one average, it produced a table of averages for every group.

I proved each by re-running the exact recorded plan and getting the same wrong answer, and I labelled the other
failures as unexplained instead of guessing. The fix tells the planner, in general terms that work for any dataset,
what each operation returns and how dates can be grouped by month or year. It is a switch that can be tested on its
own against a control. I also added evaluation-only diagnostics, so every wrong answer can now be classified.

**Potential business impact (UNVERIFIED until live evaluation):**
- if the guidance removed the confirmed categories, it would affect up to 8 of the 9 confirmed wrong answers in the
  recorded evidence (the 9th is the value-representation case handled by the schema branch);
- the diagnostics already turn "the answer was wrong" into a specific, fixable category.

Neither is a measured accuracy gain.
