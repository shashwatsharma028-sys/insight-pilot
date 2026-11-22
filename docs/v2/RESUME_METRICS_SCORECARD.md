# InsightPilot: resume metrics scorecard (P4.0 + P4.1 + P5.0)

**Rule:** every number here is a reproducible measurement from a committed file. Anything that was not measured is listed as such. Nothing is extrapolated.

## How the numbers were produced

- **Code:** `evaluation/` at commit `ba832253` on `feature/v2-p4-evaluation`, branched from `feature/insightpilot-v2` at `1fd3af3c`. V1 code is unmodified.
- **Host:** Linux x86_64 cloud container, 4 CPUs, CPython 3.11.18 with the environment-B package pins (`docs/v2/ENV_B_PINS.txt` on the P3 branch; `docs/v2/BASELINE.md` here).
- **LLM:** the scripted mock (`tests/v2/mock_llm.py`). There were **no provider calls and $0 spent**.
- **Commands:**
  ```bash
  python -m evaluation run --out <dirA> --reps 5
  ```
  ```bash
  python -m evaluation run --out <dirB> --reps 5
  ```
  ```bash
  python -m evaluation compare <dirA>/benchmark.json <dirB>/benchmark.json --out <cmp>
  ```
- **Evidence:**
  - `docs/v2/p4/baseline_v1/`: `benchmark.json`, `runs.csv`, `tasks.csv`, `llm_calls.csv`, `executions.csv`, `summary.md`;
  - `docs/v2/p4/aa_v1_vs_v1/`;
  - `docs/v2/p4/sandbox_overhead.json`.

## Measurements

| Metric | Value | Source |
|---|---|---|
| Golden numerical answers | **28/28** correct, 0 incorrect, 0 missing | baseline `summary` |
| Data-quality reference values | **20/20** | baseline |
| Tasks completed (of those expected) | 12/12; the adversarial filesystem task was blocked (1/1) | baseline |
| Task status as expected | 13/13; retries 4 (13/13 as scripted) | baseline |
| Sandbox executions per pass | 16 (3 blocked by the static safety scan) | baseline |
| Valid charts / reports | 7/7 PNG; 12/12 (Markdown, PDF, notebook) | baseline |
| Behaviour equal to the committed V1 snapshots | 4/4 datasets | baseline |
| Determinism | every deterministic metric identical across 5 repetitions, 4/4 datasets (20 runs) | baseline |
| LLM calls per pass | 32 (planner 4, code_generator 16, business_insights 12) | baseline |
| Tokens per pass | 44,490 input / 3,002 output, **character estimates of mock prompts** (class `mock_scripted/estimated_chars_div_4`); not provider tokens, not cost | baseline |
| Pipeline wall time per run | median **4.39 s**, p95 5.40 s (n=20) | baseline |
| Sandbox execution per call | median **1.23 s**, p95 1.60 s (n=80, including 15 blocked calls of ~0.1 ms) | baseline |
| Sandbox share of pipeline wall time | median **97.9%** (92.3–98.3%, n=20) | `sandbox_overhead.json` |
| Interpreter start-up vs prelude imports | 13.5 ms vs 1.07–1.26 s (two measurement sets) | `sandbox_overhead.json` |
| seaborn import cost alone | ≈ 0.50 s per execution (cumulative 570 → 1,071 ms) | `sandbox_overhead.json` |
| A/A paired comparison (two independent baseline runs) | no false positive: wall time median difference +156 ms, 95% CI [−197, +419] ms, sign p=0.82 (n=20); sandbox +33 ms, CI [−13, +105], p=0.46 (n=65) | `aa_v1_vs_v1/` |
| Offline test suite on this branch | after P4.1: **377 passed, 13 xfailed** (the 13 strict-xfail V1 security cases unchanged), 0 failed, 1 warning (pre-existing) at `d9618925`; after P4.0: 334 passed, 13 xfailed at `3a8a6147`; baseline before P4.0: 270 passed, 13 xfailed | full-suite runs |
| P4.0 + P4.1 tests | 107: P4.0 64 (57 unit/edge-case, 7 integration); P4.1 43 (prelude, V1 parity, adversarial, variants, gates, full-workload integration) | `tests/v2/p4/` |
| Mutation checks | P4.0: 16/16 (2 survived at first and exposed weak tests, which were fixed); P4.1: 16/16 | `docs/v2/p4/mutation_checks.md` |

## P4.1: lazy-seaborn V2 sandbox prelude (measured A/B, `docs/v2/p4/P41_LAZY_SEABORN.md`)

**Design:**
- one session on the same machine, environment and workload fingerprint (`3ea65684…`), commit `d9618925` with a clean tree;
- 4 datasets × 12 repetitions × 3 variants (`v1`, `v2-lazy-seaborn`, `v1-control`) = 144 runs;
- dataset-level interleaving, with the variant order cycling through all permutations (each variant in each position 16 times).

**Command:**
```bash
python -m evaluation ab --out <dir> --reps 12 --variants v1,v2-lazy-seaborn,v1-control
```

| Metric | V1 | V2 (lazy seaborn) | Paired result (V2 − V1) |
|---|---|---|---|
| Pipeline wall time, median / p95 (n=48 each) | 4,341.0 / 5,926.7 ms | **2,519.2** / 3,612.9 ms | median paired diff **−1,812.2 ms**, 95% CI [−2,048.2, −1,621.9]; V2 faster in 48/48 pairs; exact sign test p=7.1×10⁻¹⁵; change of medians **−42.0%** (denominator: V1 median 4,341.1 ms) |
| Sandbox execution, median / p95 (n=192 each; 36 safety-blocked calls of ~0.1 ms included) | 1,265.7 / 1,609.4 ms | **727.0** / 980.9 ms | over 156 executed pairs: **−570.8 ms**, CI [−594.3, −540.9]; 156/156; p=2.2×10⁻⁴⁷; **−42.8%** (denominator: V1 paired median 1,316.8 ms) |
| Control: V1 vs V1-control | n/a | n/a | wall −14.3 ms, CI [−134.9, +110.2], p=0.67; sandbox +8.2 ms, CI [−22.9, +36.2], p=0.69: no significant difference |
| Prelude only (import overhead) | 1,211.9 ms | 639.2 ms | −572.7 ms; with first use of `sns`: 1,189.7 ms, no penalty beyond V1 |
| Correctness gates | 28/28, 20/20, 12/12 + 1/1 blocked, 7/7 charts, 12/12 reports, 4/4 snapshots | identical, **7/7 charts pixel-identical** | no regression in any of 16 gates (retries 4, executions 16, failed 4, blocked 3 and LLM calls 32, all equal) |
| Mock token estimates per pass | 44,490 / 3,002 | 44,490 / 3,002 | unchanged; estimates only, not cost |

## P5.0: multi-agent coordination prototype (scripted, `docs/v2/P5_MULTI_AGENT.md`)

These are properties of the coordination and verification logic, measured on scripted agents with deliberately injected faults. They are **not** model-quality measurements. The results were identical in literal mode, with the real V1 subprocess and with the real lazy-seaborn subprocess (commit `e18c15f8`).

| Measurement | Result | Evidence |
|---|---|---|
| Fault and control cases whose expected outcome was met | 20/20 | `docs/v2/p5/*/summary.md` |
| Injected wrong claims detected by independent recomputation | 12/12 (detection rate 1.0) | the same |
| Correct claims wrongly rejected | 0 of 131 | the same |
| Incorrect claims accepted | 0 | the same |
| Ungrounded insights rejected; grounded ones wrongly rejected | 3/3; 0 of 49 | the same |
| Clean runs: golden answers accepted and correct | 28/28 | the same |
| Mutation checks on the verifier, contracts, trust gate and Supervisor | 27/27 detected (3 after adding tests) | `docs/v2/p5/mutation_checks.md` |
| Provider calls; cost | 0; none (mock character estimates only) | the same |

## Defensible resume bullets

Each bullet is supported by the measurements above.

- Built a deterministic, fully offline evaluation harness for an LLM-driven data-analysis agent (LangGraph + Python sandbox). It scores **28 numerical golden answers and 20 data-quality values across 4 datasets**, with results identical across 20 benchmark runs and zero network or API calls.
- Designed explicit **token provenance**, separating provider-reported tokens from character estimates and mock calls. Per-call accounting reconciles exactly with the existing token monitor and makes it impossible to report mock estimates as cost.
- Implemented a **fair paired-benchmark protocol**: workload fingerprinting, correctness-regression guards, paired bootstrap confidence intervals and exact sign tests. An A/A test on real timings showed no false-positive "improvement".
- **Profiled the agent pipeline.** The sandboxed code-execution subprocess accounts for **~98% of wall time**, and ~1.1–1.3 s of each ~1.3 s execution is library import overhead (interpreter start-up is 13.5 ms). The profiling identified a **~0.5 s per-execution** import that the workloads never use, as a measured optimisation target.
- Produced benchmark reports in JSON, CSV and Markdown, backed by 107 tests (unit, edge-case, integration) and 32 mutation checks, all detected. V1 behaviour was preserved against committed snapshots.
- **Cut median end-to-end pipeline latency by 42% (4.34 s → 2.52 s) and median sandbox execution time by 43%** in an offline, counterbalanced A/B benchmark: 144 runs on one machine, V2 faster in 48/48 paired runs, 95% CI of the paired difference [−2.05, −1.62] s. The change was an opt-in lazy-import sandbox prelude, with zero correctness regressions (28/28 answers, 20/20 quality values, 7/7 pixel-identical charts) and a same-session A/A control showing no effect.

- Designed a **supervisor-led multi-agent prototype** (LangGraph) with four specialist agents, typed handoff contracts and a human approval gate. An LLM-free Verification Agent recomputes every reported number from the raw data. In 20 scripted fault-injection scenarios it **detected 12/12 injected wrong results, wrongly rejected 0 of 131 correct ones and let 0 incorrect claims through**, with bounded, feedback-driven retries. Untrusted model code is refused by the unhardened sandbox by construction (27/27 mutation checks detected).

## Do NOT claim (not measured)

- **P5.0 multi-agent:**
  - The verification and detection numbers come from scripted agents and injected faults. They are not real-model accuracy, and they do not show that a multi-agent system beats V1.
  - No latency, token or cost comparison with V1 was made: the workloads differ.

- **Speedups beyond what was measured:** the 42% is for this offline workload, whose fixtures never use seaborn; that is the best case. Code that uses `sns` pays the import on first use, at no more than V1. It was measured on one 4-CPU machine with the V1 subprocess sandbox. Do not generalise it to other hardware, real-LLM workloads or the P3 container backend.
- **Money:** no cost savings and no token reductions.
- **Model quality:** nothing about LLM accuracy, answer quality or real-model behaviour. The LLM was scripted.
- **Provider numbers:** no provider latency and no provider token counts.
- **Scale:** nothing about production workloads; the data are 4 small demo datasets.
- **Containers:** nothing about container or isolation performance. The P3 container backend is not wired in, and these measurements use the V1 subprocess sandbox.
- **Other machines:** no latency on any machine other than the one recorded.

## Limitations

- The scripted LLM makes the workload deterministic, but it tells you nothing about model quality or provider behaviour.
- The latency figures are for one 4-CPU cloud container; other hardware will differ. Run-to-run variation is visible (two baseline runs: pipeline medians 4.39 s and 4.33 s).
- The workload is small: 13 tasks per pass, plus one adversarial task.
