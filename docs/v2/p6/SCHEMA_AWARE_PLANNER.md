# P6 optimisation 3: schema-aware planner guidance (engineering record; offline only)

**Branch:** `feature/v2-p6-schema-aware-planner`, from `170beb00`, in a separate worktree; the experiment branch is
untouched.
**Status:** implemented and validated offline. **No live call has been made.**
**Feature flag:** opt-in, `limits["schema_aware_planner"] = True` for the multi-agent Supervisor and
`schema_aware=True` for the single agent. It is off by default; when off, prompts are byte-identical to `170beb00`.
**Claims:** none about live accuracy until an authorised live experiment measures it.

## 1. Root cause (from recorded evidence)

- **What the model was told:** the planner (and the single agent) received the op catalogue as **argument names
  only**: `claims.describe_ops()`, for example `{"count_where_equals": ["column", "value"]}`.
- **What it was not told:** the rule that `value` is raw CSV text (for example `"True"`) is only in the module
  docstring of `multi_agent/claims.py`, which no prompt includes.
- **Why it can't infer it:** the dataset profile deliberately holds **no cell values**, only names, kinds and missing
  counts. The model can see that a column is `boolean`, but not that its cells are the text `True` / `False`.

**Evidence** (`plan_diagnostic_sample/runs.jsonl`, `recovery_experiment/runs.jsonl`):

| Recorded plan-validation problem | Count | Affected |
|---|---|---|
| `'value' must be a string` | **38** (8 diagnostic + 30 recovery) | always a **boolean** column: `pass_rate` (13), `gps_mismatch_count` (9), `mean_revenue_weekday` (8), `mean_revenue_weekend` (8) |
| `args must be an object` | 7 | `count_rows` written with `"args": []` |
| tasks missing `title` | 12 | not addressed here (task structure) |
| `mean_where_equals` given `by` instead of `where_column` | 6 | partly addressed (argument meanings now stated) |
| unknown column `subject` | 2 | not addressed (hallucinated column) |

- **What the recorded rejected plans contained:** JSON `false` for `gps_mismatch_count` (11 times), `true` for
  `pass_rate` (10), and `false` / `true` for weekday / weekend (6 each). On the text column `status`, the model wrote
  `"Present"` correctly every time (11).

**Lowercase `"true"`: confirmed evidence vs hypothesis.**
- **Confirmed (one run):** in `recovery_experiment` `selective_recovery/student/r1`:
  - the repair reply (attempt 2) wrote `pass_rate` as `{"column": "passed", "value": "true"}`, a lowercase string;
  - a string passes validation, so that task (`stu_t1`) was accepted as recovered;
  - its other two tasks were rejected only because they were not being repaired;
  - `pass_rate` was graded **incorrect**. The CSV holds `True`, so `"true"` matches no row.
  - The chain is visible in the recorded rejected-plan payload, `plan_recovery` and the grade.
- **Hypothesis (unverified):** the same misspelling also caused the other incorrect `pass_rate` answers (1 in that
  experiment's control arm, 3 in the baseline). Those runs did not record accepted plan payloads or reported claim
  values, so the cause cannot be confirmed from the evidence.
- **Affected operations:** the `value` argument of `count_where_equals`, `rate_where_equals` and
  `mean_where_equals`, and the `args` container of `count_rows`.
- **The abandoned tasks** in the selective-recovery experiment: **5 tasks, 6 hidden answers**, from **two
  different causes**:

  | Cause | Tasks | Hidden answers lost |
  |---|---|---|
  | Non-string `value` (`gps_mismatch_count`, task `att_t3`) | 4 | 4 |
  | `count_rows` written with `"args": []` (task `eco_t1`, losing `total_orders` and `total_revenue`) | 1 | 2 |
  | **Total** | **5** | **6** |

  An earlier summary attributed all six answers to the `value` type. That was wrong: two of them came from the
  `args` shape. The guidance addresses both causes with separate rules.

## 2. Implementation (smallest justified change)

- **`multi_agent/claims.py`:**
  - `describe_ops_schema()`: for each operation and argument, how it must be written. It is **generated from `OPS`**,
    the same table `validate_claim` enforces, so the guidance cannot drift from the validator.
  - `CLAIM_RULES`: four representation rules.
  - It names no dataset, column or value. The boolean rule ("True" / "False") is the definition of the boolean
    kind (`dataset.BOOLEAN_TOKENS`), not data.
- **`multi_agent/prompts.py`:** `op_schema_section()` is appended after `CLAIM OPS JSON` only when the request
  carries `op_schema`.
- **`multi_agent/supervisor.py`:** with the flag on, every plan request (including selective-recovery repair
  requests) carries `op_schema: true`. With the flag off, the request is unchanged.
- **`evaluation/live.py`:**
  - the flag for both runners;
  - `single_claims_prompt(op_schema=...)`;
  - a matched experiment `--experiment schema_aware_planner`: guidance on vs off, with selective recovery on and the
    same provider policy in both arms.
- **Unchanged:** the validator (no coercion, no flag), selective recovery, provider retries, the insight and analyst
  prompts, the model settings and the system prompts.

**Before** (what every planner and claims prompt had):

```
CLAIM OPS JSON:
{"count_rows": [], "count_where_equals": ["column", "value"], "mean_where_equals": ["column", "value", "where_column"], ...}
```

**After (flag on)**, the following is added immediately after it, to every initial planning request, every
selective-recovery repair request and every single-agent claims prompt:

```
CLAIM ARGUMENT SCHEMA JSON:
{..., "count_rows": {}, "count_where_equals": {"column": "column name, any kind", "value": "cell text (JSON string)"},
 "mean_where_equals": {"column": "column name, numeric", "value": "cell text (JSON string)", "where_column": "column name, any kind"},
 "group_sum": {"by": "column name, boolean/date/text", "by_prefix": "optional integer 1-64: group by the first N characters", "column": "column name, numeric"}, ...}
CLAIM RULES:
- args is always a JSON object; use {} for an op that has no arguments.
- A claim has exactly the fields key, op and args.
- Every column argument is the exact name of a column in DATASET PROFILE JSON, as a JSON string.
- value is the exact cell text to compare with, as a JSON string, never a JSON boolean, number or null. A boolean column contains only the text "True" or "False", with that exact capitalisation.
```

**Size and cost of the addition** (measured; the live tokenizer was not called):

| | Without guidance | With guidance | Increase |
|---|---|---|---|
| Initial planner prompt (system + user) | 2,725–3,022 chars | 4,498–4,795 chars | +1,773 chars (+59% to +65%) |
| Repair prompt (5 replayed rejection fixtures) | 3,353–4,465 chars | 5,126–6,238 chars | +1,773 chars (+40% to +53%) |

- **Tokens:** about **541 extra input tokens** per request, using 3.28 characters per token measured from
  provider-reported planner calls in the baseline. An earlier estimate of about 450 used a characters ÷ 4 rule and
  is superseded.
- **Cost:**
  - about **$0.00016 per request** at $0.30 per million input tokens;
  - at the selective-recovery arm's 1.72 planner calls per run, about **+$0.00028 per run**, roughly 10% of its
    provider-reported cost ($0.00279 per run);
  - the conservative pre-call budget check counts the section as 1,773 bytes, a worst case of $0.00053 per request.
- **Output tokens:** not expected to change, but this is unmeasured; the live evaluation will show it.

## 3. Alternatives considered

| Option | Why not chosen (or deferred) |
|---|---|
| Coerce `true` to `"True"` in the validator or engine | Rejected (requirement). It hides model errors, and a wrong spelling such as `"true"` would still match no row and silently give a wrong answer |
| Put cell values or example values in the profile | Rejected: the profile has no cell values by design, and examples could leak hidden answers |
| Hand-written prompt text per op | Rejected: it would drift from the validator. The schema is generated from `OPS` |
| Change `PLANNING_SYSTEM` | Not needed. Leaving it unchanged keeps the system prompt identical and the change confined to one optional section |
| A JSON-schema "structured output" request to the provider | A provider and transport change, out of scope |

## 4. Offline validation

- **`tests/v2/p6/test_schema_aware_planner_p6.py`, 35 tests:**
  - flag-off prompts byte-identical to `170beb00` (sha256, all 4 datasets, planner and single agent);
  - the schema covers every op and argument;
  - the guidance is present in planner, repair and single-agent prompts when on;
  - no hidden answer and no column name in the guidance;
  - string values accepted; JSON booleans, numbers, null, lists and objects rejected with the unchanged message;
    `args: []` still rejected; the validator has no flag;
  - selective recovery unchanged (repair, abandonment), with the guidance on repair requests;
  - the Supervisor adds the request key only when the flag is on;
  - the single agent uses the guidance only when asked;
  - an offline matched experiment for both architectures, where only the guidance differs;
  - **observed arm verification:** a `PromptAudit` wrapper counts, for every prompt actually handed to the provider
    port, whether it contains the exact guidance section. Every schema-arm planner or claims prompt contained it,
    and no control-arm or insight prompt did. This is also recorded per run (`prompt_audit`) for live runs, without
    storing prompt text, and a mislabelled control arm is caught by the audit alone.
- **Mutation checks: 12 of 12 caught.**
  - 9 on the guidance: always on, boolean rule removed, validator coercing booleans, repairs losing the guidance,
    the single agent ignoring the flag, the control arm also getting it, schema drift, the args rule removed, column
    names leaking.
  - 3 on the audit: always reporting guidance, bypassed in experiments, and a mislabelled control arm with the token
    check disabled.
- **Results (offline, mock provider):**
  - this test file: 36 passed;
  - all P6 tests: 129 passed;
  - **full offline regression: 2017 passed, 13 xfailed, 0 failed** (with all changes in place);
  - Streamlit UI smoke and launcher tests: 53 passed;
  - headless `streamlit run ui/app.py`: health endpoint HTTP 200;
  - offline dry run `--experiment schema_aware_planner --arch multi_agent`: 20 matched pairs, exit 0;
  - observed audit in that dry run: schema-arm planner prompts 20 of 20 with guidance, control 0 of 20, insight
    prompts 0 of 120 in both arms.

## 5. Proposed controlled live evaluation (needs approval and quota)

- **Design:** `--experiment schema_aware_planner --arch multi_agent`. The baseline's 20 multi-agent slots, seed
  20261124, each with an adjacent matched control (guidance off).
- **Held equal in both arms:** selective recovery on, the same provider policy (whatever the provider-resilience
  decision is), model, temperature 0, 2048 output tokens, pacing and the 429 / quota stop.
- **Size and cost:**
  - about 190 requests, which needs its own quota day after the provider-resilience experiment, or the same day only
    if both fit with headroom;
  - a proposed new ledger capped at **$0.25**.
- **Arm verification (precondition, from `prompt_audit`):** 100% of schema-arm planner prompts contain the
  guidance and 0% of control-arm prompts do. Otherwise the experiment is invalid.
- **Metrics reported per arm, matched pairs separately from unmatched runs:**

  | Metric | Definition |
  |---|---|
  | Hidden-answer accuracy | correct / total hidden questions (primary) |
  | Claim precision | correct / reported claims |
  | Incorrect claims | especially `pass_rate`, where a wrong spelling silently gives a wrong answer |
  | Schema rejection rate | planner replies rejected for `value` type or `args` shape ÷ planner replies received, also by cause |
  | All plan rejections | by cause (including missing `title` and unknown columns, which this change does not target) |
  | Repair frequency | runs needing a repair request ÷ runs; repair requests per run |
  | Recovered / abandoned tasks | from `plan_recovery` |
  | Complete planner failures | provider-caused vs validation-caused |
  | Required-task completion | every required task of every run counts |
  | Total tokens | provider-reported input, output and total for successful calls; planner input tokens separately (expected about +541 per request) |
  | Cost per correct answer | provider-reported, plus worst-case charges for failed attempts shown separately |
  | Latency | p50 / p95, with and without waits |
- **Decision rule (set in advance):** retain only if all of these hold:
  - `value` and `args` rejections fall;
  - hidden accuracy does not drop (95% CI lower bound of the paired difference > −1 answer per run);
  - cost per correct answer is not worse by more than 10%.

## 6. Limitations

- **Not addressed:** missing `title`, made-up columns, and the `by` / `where_column` mix-up beyond what the argument
  descriptions state.
- **No semantic fixes:** semantic errors such as `top_month_by_revenue` are deliberately out of scope.
- **Not a guarantee:** guidance makes correct output more likely; the validator and selective recovery remain the
  enforcement.

## 7. Rollback

Leave the flag off (the default), or revert the commit on `feature/v2-p6-schema-aware-planner`. No other branch is
affected.
