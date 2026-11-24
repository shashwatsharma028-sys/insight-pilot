# Live benchmark (PROVIDER calls)

- single_agent = V1-style single agent, trusted-ops (not the original unrestricted V1 code-generation pipeline)
- multi_agent = V2 multi-agent Supervisor, trusted-ops (planner, trusted analyst, stdlib verifier, insight agent)
- accuracy_hidden: questions whose answers appear in no prompt (profile-metadata coincidences excluded); accuracy_all: all 28
- cost_usd: provider-reported tokens x list prices (an ESTIMATE, not billing)
- harness p6.0-live-trusted-ops; seed 20261124; datasets ['sales', 'attendance', 'student', 'ecommerce']; reps 5
- stopped early: None
- V1 executor tripwire hits: 0

| metric | single_agent | multi_agent |
|---|---|---|
| runs | 20 | 20 |
| run_errors | 0 | 0 |
| failed_provider_attempts | 7 | 5 |
| failed_http_status | {'503': 5, 'None': 2} | {'503': 5} |
| provider_retries_429 | 0 | 0 |
| provisional_unbilled_429 | 0 | 0 |
| agent_retries | 51 | 6 |
| wait_s_total | 787.99 | 402.36 |
| accuracy_hidden | 0.6696 | 0.6 |
| accuracy_all | 0.7214 | 0.6143 |
| incorrect_claims_reported | 10 | 8 |
| claim_precision | 0.9099 | 0.9149 |
| claim_recall | 0.7214 | 0.6143 |
| llm_calls | 160 | 78 |
| input_tokens | 87443 | 48431 |
| output_tokens | 29372 | 31055 |
| total_tokens | 116815 | 79486 |
| cost_usd | 0.099663 | 0.092167 |
| execution_failures | 99 | 0 |
| task_completion_rate | 0.6667 | 0.9524 |
| latency p50/p95 ms (single_agent) | 60602.0 / 151144.1 | |
| latency excl. pacing/rate-limit waits p50/p95 ms (single_agent) | 13852.5 / 122640.2 | |
| latency p50/p95 ms (multi_agent) | 29403.7 / 35297.4 | |
| latency excl. pacing/rate-limit waits p50/p95 ms (multi_agent) | 6084.4 / 13087.9 | |

Paired latency (single -> multi): {'n': 20, 'median_a_ms': 60602.0, 'median_b_ms': 29403.67, 'median_diff_ms': -34422.71, 'relative_change_of_medians': -0.5148, 'relative_median_paired_diff': -0.568, 'ci95_median_diff_ms': [-51448.65, -25769.03], 'sign_test_p': 2e-06, 'verdict': 'faster'}
Paired latency excl. waits (single -> multi): {'n': 20, 'median_a_ms': 13852.46, 'median_b_ms': 6084.39, 'median_diff_ms': -5753.74, 'relative_change_of_medians': -0.5608, 'relative_median_paired_diff': -0.4154, 'ci95_median_diff_ms': [-32850.57, -2794.64], 'sign_test_p': 0.000402, 'verdict': 'faster'}
