# InsightPilot: resume metrics scorecard (P4.0)

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
| Offline test suite on this branch | **334 passed, 13 xfailed** (the 13 strict-xfail V1 security cases), 0 failed, 1 warning (as before) at `3a8a6147`; baseline before P4.0: 270 passed, 13 xfailed | full-suite run |
| P4.0 tests | 64 (57 unit/edge-case, 7 integration through the real pipeline) | `tests/v2/p4/` |
| Mutation checks on the P4.0 harness | 16/16 deliberately broken variants detected. 2 survived at first and exposed weak tests, which were fixed | `docs/v2/p4/mutation_checks.md` |

## Defensible resume bullets

Each bullet is supported by the measurements above.

- Built a deterministic, fully offline evaluation harness for an LLM-driven data-analysis agent (LangGraph + Python sandbox). It scores **28 numerical golden answers and 20 data-quality values across 4 datasets**, with results identical across 20 benchmark runs and zero network or API calls.
- Designed explicit **token provenance**, separating provider-reported tokens from character estimates and mock calls. Per-call accounting reconciles exactly with the existing token monitor and makes it impossible to report mock estimates as cost.
- Implemented a **fair paired-benchmark protocol**: workload fingerprinting, correctness-regression guards, paired bootstrap confidence intervals and exact sign tests. An A/A test on real timings showed no false-positive "improvement".
- **Profiled the agent pipeline.** The sandboxed code-execution subprocess accounts for **~98% of wall time**, and ~1.1–1.3 s of each ~1.3 s execution is library import overhead (interpreter start-up is 13.5 ms). The profiling identified a **~0.5 s per-execution** import that the workloads never use, as a measured optimisation target.
- Produced benchmark reports in JSON, CSV and Markdown, backed by 64 tests (unit, edge-case, integration) and 16 mutation checks, all detected. V1 behaviour was preserved against committed snapshots.

## Do NOT claim (not measured)

- **Speedups:** no optimisation has been implemented. The ~0.5 s seaborn figure is an import measurement, not a pipeline speedup.
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
