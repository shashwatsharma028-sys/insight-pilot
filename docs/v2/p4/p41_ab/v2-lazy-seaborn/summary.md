# InsightPilot benchmark: variant `v2-lazy-seaborn`

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

- pipeline wall time per run: n=48, median 2519.2 ms, p95 3612.9 ms (min 1910.9, max 3687.0)
- sandbox execution (subprocess) per call: n=192, median 727.0 ms, p95 980.9 ms (min 0.1, max 1150.5)
- scripted LLM call: n=384, median 0.0 ms, p95 0.1 ms (min 0.0, max 0.4)

| Dataset | Pipeline wall | Sandbox execution |
|---|---|---|
| attendance | n=12, median 2540.7 ms, p95 3086.6 ms (min 2137.4, max 3086.6) | n=36, median 810.3 ms, p95 1006.5 ms (min 584.9, max 1150.5) |
| ecommerce | n=12, median 2190.3 ms, p95 2541.9 ms (min 1910.9, max 2541.9) | n=36, median 703.1 ms, p95 980.0 ms (min 547.7, max 1042.4) |
| sales | n=12, median 3329.2 ms, p95 3687.0 ms (min 2655.6, max 3687.0) | n=84, median 602.9 ms, p95 993.4 ms (min 0.1, max 1077.2) |
| student | n=12, median 2511.1 ms, p95 2748.8 ms (min 2105.1, max 2748.8) | n=36, median 784.9 ms, p95 956.4 ms (min 587.9, max 979.4) |

Limitations: scripted LLM (no model quality, provider latency or provider tokens); one machine; latency includes Python start-up of every sandbox subprocess.
