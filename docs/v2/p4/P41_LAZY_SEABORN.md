# P4.1: lazy-seaborn V2 sandbox prelude, measured A/B result

## What changed

- **`sandbox_v2/` (new, opt-in):**
  - `lazy_prelude.derive_lazy_wrapper_text` takes the **V1 wrapper's own output** and replaces exactly the one prelude line `import seaborn as sns`; user code is never rewritten.
  - The replacement block registers seaborn's 12 colormaps from the installed `seaborn/cm.py` data **without importing seaborn**: `rocket`, `mako`, `flare`, `crest`, `vlag`, `icefire` and their `_r` variants, which is the only import side effect visible to analysis code.
  - `sns` becomes a proxy. The first attribute access imports seaborn, binds the real module and re-applies `warnings.filterwarnings('ignore')`, which preserves V1's effective warning precedence.
  - **Fail-safe paths:** an unexpected `cm.py` layout falls back to the eager V1 import, and an unexpected V1 prelude shape is refused with `PreludeMismatch`.
- **`sandbox_v2.executor.execute_code`:** the V1 executor statement for statement, apart from the wrapper call (proved on the syntax tree). It keeps:
  - the AST safety scan and the temp-file subprocess;
  - `[sys.executable, tmp_path]` and the same environment;
  - `MAX_EXECUTION_TIME`;
  - chart detection, `classify_error`, timeout handling and clean-up.
- **`evaluation/`:**
  - the `v2-lazy-seaborn` and `v1-control` variants;
  - a counterbalanced A/B runner;
  - chart pixel hashes;
  - stricter paired gates: any change in chart pixels, retries, executions, failed or blocked executions, or LLM calls is a regression.
- **V1 is unchanged:** `sandbox/`, `agent/`, `utils/` and `ui/` were not touched, and no V1 module imports `sandbox_v2`. The default variant remains `v1`.

## Side effects of `import seaborn` (measured, seaborn 0.13.2)

| Side effect | Effect on lazy loading |
|---|---|
| rcParams changes | none |
| pandas option changes | none |
| 12 colormaps registered | handled by pre-registration |
| scipy pre-loaded (633 modules) | not pre-loaded under V2; code must import scipy itself, as under V1 to use it by name |
| 2 scipy warning filters added | V1's later `filterwarnings('ignore')` dominates; V2 re-applies it after the lazy import |

**Documented difference:** until its first use, `sns` is not a module object, so `isinstance(sns, ModuleType)` is False. This is pinned by a test.

## Benchmark design

- **Command:**
  ```bash
  python -m evaluation ab --out <dir> --reps 12 --variants v1,v2-lazy-seaborn,v1-control
  ```
- **Session and machine:** one session, one machine (4-CPU Linux cloud container, CPython 3.11.18, environment B), commit `d9618925` with a clean tree.
- **Workload:** for both variants, the identical workload fingerprint `3ea65684…`: 4 datasets, 13 scripted tasks per pass, golden answers, the scripted LLM, the same Python environment and the same hardware.
- **Size:** 12 repetitions per dataset per variant, 144 runs in total.
- **Order:** dataset-level interleaving. For each (repetition, dataset) cell, the three variants ran back to back, with the order cycling through all 6 permutations. Each variant held each position exactly 16 times, which balances order and drift bias.
- **Control:** `v1-control` is V1 under another name, in the same session.

## Correctness gates (all passed; nothing differs between V1 and V2)

| Gate | V1 | V2 |
|---|---|---|
| Golden numerical answers | 28/28 | 28/28 |
| Data-quality values | 20/20 | 20/20 |
| Tasks completed of expected; adversarial task blocked | 12/12; 1/1 | 12/12; 1/1 |
| Task status as expected; retries (as scripted) | 13/13; 4 (13/13) | 13/13; 4 (13/13) |
| Sandbox executions; failed; safety-blocked | 16; 4; 3 | 16; 4; 3 |
| Valid charts; decoded pixels identical to V1 | 7/7 | 7/7; 7/7 identical |
| Valid reports | 12/12 | 12/12 |
| Behaviour equal to the committed V1 snapshots | 4/4 | 4/4 |
| Deterministic across 12 repetitions | 4/4 | 4/4 |
| Run errors | 0 | 0 |
| LLM calls per pass; mock token estimates | 32; 44,490 / 3,002 | 32; 44,490 / 3,002 |

## Latency (all runs of each variant)

| | V1 median | V1 p95 | V2 median | V2 p95 |
|---|---|---|---|---|
| Pipeline wall per run (n=48) | 4,341.0 ms | 5,926.7 ms | **2,519.2 ms** | 3,612.9 ms |
| Sandbox execution per call (n=192, incl. 36 safety-blocked of ~0.1 ms) | 1,265.7 ms | 1,609.4 ms | **727.0 ms** | 980.9 ms |

## Paired comparison (`evaluation.paired`)

| Metric | Pairs | Median paired diff (V2 − V1) | 95% bootstrap CI | Change of medians, ÷ V1 median | Pairs where V2 was faster | Exact sign test | Verdict |
|---|---|---|---|---|---|---|---|
| Pipeline wall | 48 (dataset, repetition) | **−1,812.2 ms** | [−2,048.2, −1,621.9] | **−42.0%** (4,341.1 → 2,519.2 ms) | 48/48 | p = 7.1×10⁻¹⁵ | **faster** |
| Sandbox execution | 156 (dataset, task, attempt, rep; blocked excluded) | **−570.8 ms** | [−594.3, −540.9] | **−42.8%** (1,316.8 → 752.9 ms) | 156/156 | p = 2.2×10⁻⁴⁷ | **faster** |
| Control: V1 vs V1-control, wall | 48 | −14.3 ms | [−134.9, +110.2] | −2.4% | 26/48 | p = 0.67 | no significant difference |
| Control: V1 vs V1-control, sandbox | 156 | +8.2 ms | [−22.9, +36.2] | +1.3% | 75/156 | p = 0.69 | no significant difference |

The control in the same session shows no difference, so the V2 effect is not an artefact of order or machine drift.

## Import overhead (`p41_ab/import_overhead.json`)

Each script is the real wrapper around trivial user code, measured over 20 rotated rounds:

| Script | Median |
|---|---|
| V1 prelude | 1,211.9 ms |
| V2 prelude | **639.2 ms** (−572.7 ms) |
| V2 prelude + first use of `sns` | 1,189.7 ms |

So code that does use seaborn pays the import on first use, with no penalty beyond V1.

## Limitations

- **One machine, one session:** this is not evidence for other hardware.
- **Scripted LLM:** the golden fixtures never use seaborn, so the benchmark measures the best case for this optimisation. Real model-generated code that calls `sns` would pay the import (no worse than V1, as the table above shows).
- **Mock token estimates are unchanged** and are estimates of scripted prompts, not provider usage or cost.
- **V1 subprocess sandbox:** the P3 container runtime is not involved and was not modified.
- **Security:** the subprocess isolation, the AST safety scan, the timeout, the chart paths and the error classification are V1's (proved on the syntax tree and by parity tests). The lazy block runs inside the same sandbox subprocess as the V1 prelude. It reads the installed seaborn's `cm.py` and executes its colormap data, which is the same trusted dependency code V1 executes on `import seaborn`.

## Evidence

- `docs/v2/p4/p41_ab/<variant>/`: `benchmark.json`, CSVs and `summary.md` for each variant;
- `docs/v2/p4/p41_ab/compare_*/`: the paired comparisons;
- `docs/v2/p4/p41_ab/import_overhead.json`;
- `docs/v2/p4/mutation_checks.md`: L1–L16, all detected.
