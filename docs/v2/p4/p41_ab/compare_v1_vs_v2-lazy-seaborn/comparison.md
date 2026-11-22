# Paired comparison: `v1` (A) vs `v2-lazy-seaborn` (B)

- workload `3ea6568486926ad46e7e3f453315cb802ec7784fd3af19e4364468c156267e28` (identical for both)
- environments identical (ignoring commit): True
- **verdict: improvement claims: pipeline_wall: median -1812.17 ms per pair (95% CI [-2048.24, -1621.91]), sign test p=0.0; sandbox_execution: median -570.82 ms per pair (95% CI [-594.25, -540.86]), sign test p=0.0**

| Correctness metric | A | B | Regression |
|---|---|---|---|
| answers_correct | 28 | 28 | no |
| quality_correct | 20 | 20 | no |
| tasks_completed_of_expected | 12 | 12 | no |
| expected_failures_observed | 1 | 1 | no |
| task_status_as_expected | 13 | 13 | no |
| charts_valid | 7 | 7 | no |
| reports_valid | 12 | 12 | no |
| deterministic_datasets | 4 | 4 | no |
| run_errors_absent | 0 | 0 | no |
| behavior_matches_v1_snapshot | 4 | 4 | no |
| retries_total | 4 | 4 | no |
| sandbox_executions | 16 | 16 | no |
| sandbox_failed_executions | 4 | 4 | no |
| safety_blocked_executions | 3 | 3 | no |
| llm_calls | 32 | 32 | no |
| chart_pixels_identical | 7 | 7 | no |

| Latency (paired) | n | median A ms | median B ms | median paired diff (B-A) ms | 95% CI ms | change of medians (B-A)/A | sign p | verdict |
|---|---|---|---|---|---|---|---|---|
| pipeline_wall | 48 | 4341.05 | 2519.22 | -1812.17 | [-2048.24, -1621.91] | -42.0% | 0.0 | faster |
| sandbox_execution | 156 | 1316.83 | 752.86 | -570.82 | [-594.25, -540.86] | -42.8% | 0.0 | faster |

Percentages use the baseline (A) median as the denominator. Verdicts need a 95% bootstrap CI of the median paired difference that excludes 0, an exact sign test p < 0.05 and at least 5 pairs.

- LLM calls: A 32, B 32
- tokens `mock_scripted/estimated_chars_div_4`: input 44490 -> 44490, output 3002 -> 3002 (character estimates of scripted prompts: a change in PROMPT SIZE, not cost)
