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
| accuracy_hidden | 0.0 | 0.0 |
| accuracy_all | 0.0 | 0.0 |
| incorrect_claims_reported | 0 | 0 |
| claim_precision | None | None |
| claim_recall | 0.0 | 0.0 |
| llm_calls | 9 | 2 |
| input_tokens | 18048 | 5827 |
| output_tokens | 18432 | 4096 |
| total_tokens | 36480 | 9923 |
| cost_usd | 0.009178 | 0.002221 |
| retries | 6 | 0 |
| execution_failures | 0 | 0 |
| task_completion_rate | 0.0 | None |
| latency p50/p95 ms (single_agent) | 1999.2 / 1999.2 | |
| latency p50/p95 ms (multi_agent) | 5480.9 / 5480.9 | |

Paired latency (single -> multi): {'n': 1, 'median_a_ms': 1999.17, 'median_b_ms': 5480.85, 'median_diff_ms': 3481.69, 'relative_change_of_medians': 1.7416, 'relative_median_paired_diff': 1.7416, 'verdict': 'insufficient pairs'}
