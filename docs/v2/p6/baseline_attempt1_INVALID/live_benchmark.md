# Live benchmark (PROVIDER calls)

- single_agent = V1-style single agent, trusted-ops (not the original unrestricted V1 code-generation pipeline)
- multi_agent = V2 multi-agent Supervisor, trusted-ops (planner, trusted analyst, stdlib verifier, insight agent)
- accuracy_hidden: questions whose answers appear in no prompt (profile-metadata coincidences excluded); accuracy_all: all 28
- cost_usd: provider-reported tokens x list prices (an ESTIMATE, not billing)
- harness p6.0-live-trusted-ops; seed 20261124; datasets ['sales', 'attendance', 'student', 'ecommerce']; reps 5
- stopped early: single_agent/attendance/r2: budget $0.5000 could be exceeded (spent $0.495563, worst case of this call $0.005786)
- V1 executor tripwire hits: 0

| metric | single_agent | multi_agent |
|---|---|---|
| runs | 8 | 10 |
| run_errors | 0 | 0 |
| accuracy_hidden | 0.2826 | 0.193 |
| accuracy_all | 0.2857 | 0.2 |
| incorrect_claims_reported | 1 | 0 |
| claim_precision | 0.9412 | 1.0 |
| claim_recall | 0.2857 | 0.2 |
| llm_calls | 74 | 25 |
| input_tokens | 117205 | 51043 |
| output_tokens | 118505 | 33529 |
| total_tokens | 235710 | 84572 |
| cost_usd | 0.331424 | 0.099135 |
| retries | 42 | 0 |
| execution_failures | 15 | 0 |
| task_completion_rate | 0.0833 | 1.0 |
| latency p50/p95 ms (single_agent) | 3183.9 / 6785.8 | |
| latency p50/p95 ms (multi_agent) | 407.5 / 7715.4 | |

Paired latency (single -> multi): {'n': 3, 'median_a_ms': 1694.8, 'median_b_ms': 376.41, 'median_diff_ms': -1227.73, 'relative_change_of_medians': -0.7779, 'relative_median_paired_diff': -0.7244, 'verdict': 'insufficient pairs'}
