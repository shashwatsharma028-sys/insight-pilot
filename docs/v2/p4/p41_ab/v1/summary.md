# InsightPilot benchmark: variant `v1`

- harness p4.1, started 2026-11-22T15:15:43Z, 4 datasets x 12 repetitions = 48 runs
- workload fingerprint `3ea6568486926ad46e7e3f453315cb802ec7784fd3af19e4364468c156267e28`
- environment: Python 3.11.18 on Linux-6.18.44-fc-v726-x86_64-with-glibc2.39 (4 CPUs), commit `d961892599e585aebe728a120cdb622cb2fefb14`
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

- pipeline wall time per run: n=48, median 4341.0 ms, p95 5926.7 ms (min 3404.8, max 6622.2)
- sandbox execution (subprocess) per call: n=192, median 1265.7 ms, p95 1609.4 ms (min 0.1, max 2038.5)
- scripted LLM call: n=384, median 0.0 ms, p95 0.1 ms (min 0.0, max 0.4)

| Dataset | Pipeline wall | Sandbox execution |
|---|---|---|
| attendance | n=12, median 4388.9 ms, p95 4789.7 ms (min 3632.0, max 4789.7) | n=36, median 1332.7 ms, p95 1689.9 ms (min 1074.5, max 1771.2) |
| ecommerce | n=12, median 3783.5 ms, p95 4522.1 ms (min 3404.8, max 4522.1) | n=36, median 1252.3 ms, p95 1533.0 ms (min 1027.1, max 1557.9) |
| sales | n=12, median 5582.2 ms, p95 6622.2 ms (min 4797.2, max 6622.2) | n=84, median 1178.6 ms, p95 1556.8 ms (min 0.1, max 2038.5) |
| student | n=12, median 4216.4 ms, p95 4838.3 ms (min 3757.3, max 4838.3) | n=36, median 1368.3 ms, p95 1594.3 ms (min 1070.8, max 1662.9) |

Limitations: scripted LLM (no model quality, provider latency or provider tokens); one machine; latency includes Python start-up of every sandbox subprocess.
