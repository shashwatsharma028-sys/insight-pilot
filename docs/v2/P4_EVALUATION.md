# InsightPilot V2: P4.0 evaluation and benchmarking foundation

**Status:** P4.0 IMPLEMENTED on `feature/v2-p4-evaluation`, branched from `feature/insightpilot-v2` at `1fd3af3c`. **P4.1 (lazy-seaborn V2 sandbox prelude) is IMPLEMENTED and measured**: see `docs/v2/p4/P41_LAZY_SEABORN.md`. P4.2 is not started.

## What it measures

`evaluation/` runs the **unmodified V1 LangGraph pipeline** (ingest → plan → code generation → V1 subprocess sandbox → retry → interpretation → report) on the four demo datasets. It uses the scripted LLM from `tests/v2/mock_llm.py`, so there is no network, no API key and no cost.

| Measurement | Definition | Reference |
|---|---|---|
| Numerical accuracy | each of the 28 golden answers printed by the sandboxed code is correct, incorrect or missing (golden tolerance: rel 1e-9, abs 1e-6); unexpected and duplicate keys are reported | `tests/v2/golden/expected/*.json`, computed independently with the standard library |
| Data-quality accuracy | 5 values per dataset (rows, columns, duplicates, missing values per column, quality score) from the V1 ingestion report | the same files (20 values) |
| Task completion | completed of those expected to complete; expected failures observed (the adversarial filesystem task must be blocked); status as expected | the scenario's `expected_status` |
| Retries and failures | retries per task against the script; failed tasks; error types; sandbox executions blocked by the safety scan | the scenario's `expected_retries` |
| Execution latency | `time.perf_counter` around every `agent.nodes.executor.execute_code` call and around each whole pipeline run; median, p95 (nearest rank), min and max | none (machine-dependent) |
| Chart validity | an expected chart exists inside the run's output directory, has the PNG signature and decodes with Pillow (dimensions recorded) | the scenario's `expected_charts` |
| Report validity | Markdown: an H1 title plus a `### n. <title>` heading per completed task. PDF: a `%PDF-` header and a `%%EOF` trailer. Notebook: `nbformat.validate` passes, with at least 1 + completed-task code cells | none |
| V1 behaviour parity | `normalize_run` of the run equals the committed V1 snapshot | `tests/v2/golden/snapshots/*_graph_safe_mode_off.json` |
| LLM calls | per call: skill, call source, token source, tokens, duration | none |
| Determinism | every deterministic metric is identical across repetitions | none |

## Token provenance (`evaluation/provenance.py`)

Every LLM call carries two labels:
- `call_source`: `mock_scripted` or `provider`;
- `token_source`: `provider_reported`, meaning valid `usage_metadata` with non-negative integers, or `estimated_chars_div_4`, which is exactly the V1 token monitor's fallback.

Invalid usage metadata is flagged as an anomaly and estimated. The rules:
- Totals are kept **per class and never summed across classes**.
- A cost is computed only for `provider/provider_reported` with configured prices.
- Mock estimates are labelled "never cost or savings".

In every run, the per-call estimates reconcile exactly with the V1 monitor's totals (an integration test checks this).

## Fair paired comparison (`evaluation/paired.py`)

`python -m evaluation compare A/benchmark.json B/benchmark.json --out DIR` refuses (exit 2) unless all of these are identical:
- the workload fingerprint (dataset SHA-256s, scenario including the fixture code, expected values, harness version, retry budget, safe mode);
- the dataset set;
- the repetition count;
- the token-provenance classes.

The comparison then works as follows:
- **Correctness:** answers, quality, completion, expected failures, statuses, charts, reports, determinism and run errors are compared metric by metric. Any decrease is a **REGRESSION**, and then no improvement may be claimed.
- **Latency pairing:** pipeline wall time is paired by (dataset, repetition), and sandbox time by (dataset, task, attempt, repetition). Safety-blocked executions are excluded as samples.
- **Latency statistics:** each metric reports the median paired difference, a seeded bootstrap 95% CI and an exact two-sided sign test. A metric is "faster" or "slower" only if the CI excludes 0, p < 0.05 and there are at least 5 pairs.
- **LLM calls and tokens** are compared per provenance class. Mock token deltas are labelled as prompt-size changes, not cost.

**Protocol for a future V1-versus-V2 claim:**
1. Run both variants on the same machine and commit-pinned inputs, with at least 5 repetitions; the harness interleaves datasets.
2. Register V2 as a `harness.Variant` (a context manager applied around each run).
3. Compare, and claim only what the comparison reports.

## Usage

```bash
python -m evaluation run --out /tmp/bench --reps 5
```
```bash
python -m evaluation compare /tmp/benchA/benchmark.json /tmp/benchB/benchmark.json --out /tmp/cmp
```

`--out` must lie outside the repository's `reports/`, `lessons/` and `data/` (it is refused otherwise). Outputs:
- `benchmark.json`;
- `runs.csv`, `tasks.csv`, `llm_calls.csv`, `executions.csv`;
- `summary.md`.

## Measured baseline (committed evidence)

- `docs/v2/p4/baseline_v1/`: V1, 4 datasets × 5 repetitions, commit `ba832253`;
- `docs/v2/p4/aa_v1_vs_v1/`: two independent baseline runs compared, the A/A check;
- `docs/v2/p4/sandbox_overhead.json`: an interpreter and import breakdown.

The numbers are summarised in `docs/v2/RESUME_METRICS_SCORECARD.md`.

## Limitations

- **Scripted LLM:** this says nothing about model answer quality, provider latency, provider tokens or cost.
- **Small workload:** four small demo datasets, 13 tasks per pass, one adversarial task. This is not a representative production workload.
- **Latency is machine-dependent.** It was measured on one 4-CPU cloud container and includes every sandbox subprocess's Python start-up and library imports.
- **The sandbox is the V1 subprocess sandbox.** The P3 container backend is not wired in and is not measured here.
- **Reused fixtures:** the harness relies on the P0 golden fixtures and the scripted LLM. Its correctness bar is therefore "equal to the independent references and the committed V1 behaviour", not a broader quality judgement.

## Most promising P4.1 optimisation (proposal only; needs review)

**Measured:**
- the sandbox subprocess is **97.9%** of pipeline wall time (median over 20 runs, range 92.3–98.3%);
- an executed sandbox call takes a median of **1,283 ms**, while the interpreter itself starts in **13.5 ms**;
- the V1 prelude's imports take **1.07–1.26 s** across two measurement sets;
- `seaborn` alone adds **≈ 0.50 s** (cumulative 570 → 1,071 ms). No golden fixture uses it: the prelude's `seaborn-v0_8-darkgrid` is a Matplotlib style.

**Candidate A (low risk):** make `sns` in the sandbox prelude a lazy proxy that imports seaborn on first attribute access. Generated code that uses `sns` keeps working; code that never touches it saves the import. Expected effect is roughly −0.5 s per execution on this machine, but that is unverified. It must be measured with `evaluation.paired` (A = V1, B = lazy-seaborn variant), with zero correctness regressions, unchanged V1 snapshots and the 13 strict-xfails intact.

**Candidate B (larger, security review required):** a pre-imported worker (fork server) that forks a fresh process per execution. This would remove most of the ~1.1 s import cost, but it changes the sandbox process model, so it belongs with the P3 isolation review.
