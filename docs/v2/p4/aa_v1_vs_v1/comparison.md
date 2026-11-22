# Paired comparison: `v1` (A) vs `v1` (B)

- workload `b4ca411def67038d3525f3e0c0e72594850015eb5ebb0ec89cedde39e3eff7c0` (identical for both)
- environments identical (ignoring commit): True
- **verdict: no significant improvement**

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

| Latency (paired) | n | median A ms | median B ms | median diff ms | 95% CI | sign p | verdict |
|---|---|---|---|---|---|---|---|
| pipeline_wall | 20 | 4391.63 | 4332.37 | 155.57 | [-197.38, 418.52] | 0.823803 | no significant difference |
| sandbox_execution | 65 | 1283.39 | 1365.82 | 33.14 | [-12.7, 105.06] | 0.457021 | no significant difference |

- LLM calls: A 32, B 32
- tokens `mock_scripted/estimated_chars_div_4`: input 44490 -> 44490, output 3002 -> 3002 (character estimates of scripted prompts: a change in PROMPT SIZE, not cost)
