# InsightPilot benchmark: variant `v1-control`

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

- pipeline wall time per run: n=48, median 4238.8 ms, p95 5878.7 ms (min 3434.9, max 6114.9)
- sandbox execution (subprocess) per call: n=192, median 1279.7 ms, p95 1575.1 ms (min 0.1, max 1852.7)
- scripted LLM call: n=384, median 0.0 ms, p95 0.1 ms (min 0.0, max 0.2)

| Dataset | Pipeline wall | Sandbox execution |
|---|---|---|
| attendance | n=12, median 4190.1 ms, p95 4741.9 ms (min 3713.6, max 4741.9) | n=36, median 1372.5 ms, p95 1616.0 ms (min 1073.6, max 1629.8) |
| ecommerce | n=12, median 3875.0 ms, p95 4304.5 ms (min 3434.9, max 4304.5) | n=36, median 1221.6 ms, p95 1517.1 ms (min 1038.5, max 1595.5) |
| sales | n=12, median 5643.6 ms, p95 6114.9 ms (min 4739.6, max 6114.9) | n=84, median 1150.2 ms, p95 1552.1 ms (min 0.1, max 1647.4) |
| student | n=12, median 4109.0 ms, p95 4977.6 ms (min 3642.5, max 4977.6) | n=36, median 1356.9 ms, p95 1564.0 ms (min 1104.3, max 1852.7) |

Limitations: scripted LLM (no model quality, provider latency or provider tokens); one machine; latency includes Python start-up of every sandbox subprocess.
