# InsightPilot benchmark: variant `v1`

- harness p4.0, started 2026-11-22T14:49:30Z, 4 datasets x 5 repetitions = 20 runs
- workload fingerprint `b4ca411def67038d3525f3e0c0e72594850015eb5ebb0ec89cedde39e3eff7c0`
- environment: Python 3.11.18 on Linux-6.18.44-fc-v726-x86_64-with-glibc2.39 (4 CPUs), commit `ba83225331372a507bbee2a1503c51184ab7009c`
- LLM: scripted mock (tests/v2/mock_llm.py); no provider calls are possible

## Correctness (first repetition; every repetition must be identical)

| Metric | Result |
|---|---|
| Golden numerical answers | 28/28 correct (0 incorrect, 0 missing) |
| Data-quality reference values | 20/20 |
| Tasks completed (of those expected to complete) | 12/12 |
| Expected failures observed (adversarial task blocked) | 1/1 |
| Task status as expected | 13/13 |
| Retries (total; as scripted) | 4 (13/13 as expected) |
| Sandbox executions (blocked by safety scan; failed) | 16 (3; 4) |
| Valid charts | 7/7 |
| Valid reports (Markdown, PDF, notebook) | 12/12 |
| Behaviour equals committed V1 snapshot | 4/4 datasets |
| Deterministic across repetitions | 4/4 datasets |
| Run errors | 0 |

## LLM calls and tokens (per benchmark pass)

- calls: 32 (business_insights 12, code_generator 16, planner 4)
- `mock_scripted/estimated_chars_div_4`: 32 calls, 44490 input / 3002 output tokens; cost: None (not a provider call: tokens are character estimates of scripted prompts; never cost or savings)

## Latency (all repetitions; machine-dependent)

- pipeline wall time per run: n=20, median 4391.6 ms, p95 5400.5 ms (min 3494.8, max 6314.6)
- sandbox execution (subprocess) per call: n=80, median 1233.8 ms, p95 1597.9 ms (min 0.1, max 1840.7)
- scripted LLM call: n=160, median 0.0 ms, p95 0.1 ms (min 0.0, max 0.2)

| Dataset | Pipeline wall | Sandbox execution |
|---|---|---|
| attendance | n=5, median 4446.1 ms, p95 4487.8 ms (min 3921.9, max 4487.8) | n=15, median 1365.0 ms, p95 1597.9 ms (min 1156.6, max 1597.9) |
| ecommerce | n=5, median 3572.6 ms, p95 4337.2 ms (min 3494.8, max 4337.2) | n=15, median 1208.5 ms, p95 1522.1 ms (min 1091.8, max 1522.1) |
| sales | n=5, median 5349.5 ms, p95 6314.6 ms (min 4755.1, max 6314.6) | n=35, median 1087.6 ms, p95 1611.4 ms (min 0.1, max 1700.5) |
| student | n=5, median 4005.2 ms, p95 4934.0 ms (min 3699.7, max 4934.0) | n=15, median 1368.5 ms, p95 1840.7 ms (min 1090.1, max 1840.7) |

Limitations: scripted LLM (no model quality, provider latency or provider tokens); one machine; latency includes Python start-up of every sandbox subprocess.
