# Live benchmark (PROVIDER calls)

- single_agent = V1-style single agent, trusted-ops (not the original unrestricted V1 code-generation pipeline)
- multi_agent = V2 multi-agent Supervisor, trusted-ops (planner, trusted analyst, stdlib verifier, insight agent)
- accuracy_hidden: questions whose answers appear in no prompt (profile-metadata coincidences excluded); accuracy_all: all 28
- cost_usd: provider-reported tokens x list prices (an ESTIMATE, not billing)
- harness p6.0-live-trusted-ops; seed 20261124; datasets ['student']; reps 1
- stopped early: None
- V1 executor tripwire hits: 0

| metric | single_agent | multi_agent |
|---|---|---|
| runs | 1 | 1 |
| run_errors | 0 | 0 |
| accuracy_hidden | 0.8333 | 1.0 |
| accuracy_all | 0.8571 | 1.0 |
| incorrect_claims_reported | 0 | 0 |
| claim_precision | 1.0 | 1.0 |
| claim_recall | 0.8571 | 1.0 |
| llm_calls | 9 | 5 |
| input_tokens | 5722 | 2279 |
| output_tokens | 2857 | 1400 |
| total_tokens | 8579 | 3679 |
| cost_usd | 0.008859 | 0.004184 |
| retries | 3 | 0 |
| execution_failures | 4 | 0 |
| task_completion_rate | 0.6667 | 1.0 |
| latency p50/p95 ms (single_agent) | 8220.3 / 8220.3 | |
| latency p50/p95 ms (multi_agent) | 6622.1 / 6622.1 | |

Paired latency (single -> multi): {'n': 1, 'median_a_ms': 8220.33, 'median_b_ms': 6622.1, 'median_diff_ms': -1598.24, 'relative_change_of_medians': -0.1944, 'relative_median_paired_diff': -0.1944, 'verdict': 'insufficient pairs'}
