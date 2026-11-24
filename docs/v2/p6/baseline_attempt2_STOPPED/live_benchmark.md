# Live benchmark (PROVIDER calls)

- single_agent = V1-style single agent, trusted-ops (not the original unrestricted V1 code-generation pipeline)
- multi_agent = V2 multi-agent Supervisor, trusted-ops (planner, trusted analyst, stdlib verifier, insight agent)
- accuracy_hidden: questions whose answers appear in no prompt (profile-metadata coincidences excluded); accuracy_all: all 28
- cost_usd: provider-reported tokens x list prices (an ESTIMATE, not billing)
- harness p6.0-live-trusted-ops; seed 20261124; datasets ['sales', 'attendance', 'student', 'ecommerce']; reps 5
- stopped early: multi_agent/sales/r1: quota/billing stop: HTTP 429: daily quota or billing/credits ([{
  "error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://aistudio.google.com/d) (run excluded)
- V1 executor tripwire hits: 0

| metric | single_agent | multi_agent |
|---|---|---|
| runs | 2 | None |
| run_errors | 0 | None |
| failed_provider_attempts | 0 | None |
| failed_http_status | {} | None |
| provider_retries_429 | 0 | None |
| provisional_unbilled_429 | 0 | None |
| agent_retries | 5 | None |
| wait_s_total | 52.41 | None |
| accuracy_hidden | 0.7273 | None |
| accuracy_all | 0.7857 | None |
| incorrect_claims_reported | 1 | None |
| claim_precision | 0.9167 | None |
| claim_recall | 0.7857 | None |
| llm_calls | 16 | None |
| input_tokens | 8471 | None |
| output_tokens | 1475 | None |
| total_tokens | 9946 | None |
| cost_usd | 0.006229 | None |
| execution_failures | 9 | None |
| task_completion_rate | 0.8333 | None |
| latency p50/p95 ms (single_agent) | 34000.4 / 35452.2 | |
| latency excl. pacing/rate-limit waits p50/p95 ms (single_agent) | 7795.1 / 8395.6 | |

Paired latency (single -> multi): {'n': 0, 'verdict': 'insufficient pairs'}
Paired latency excl. waits (single -> multi): {'n': 0, 'verdict': 'insufficient pairs'}
