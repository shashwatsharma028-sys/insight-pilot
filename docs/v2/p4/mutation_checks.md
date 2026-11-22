# P4.0 mutation checks

Each mutant was applied to a throw-away copy of the repository. `tests/v2/p4/test_evaluation_units_p40.py` was then run against it. A mutant is detected ("killed") when at least one test fails.

| # | Deliberate defect | Result |
|---|---|---|
| E1 | invalid usage metadata counted as provider-reported | killed |
| E2 | mock calls given a cost | killed |
| E3 | missing answers counted as correct | killed |
| E4 | extra keys in the quality dict ignored | killed |
| E5 | charts outside the output directory accepted | **survived first**; test added; killed |
| E6 | truncated PDF accepted | killed |
| E7 | speed claimed despite a correctness regression | killed |
| E8 | workload fingerprint ignored | killed |
| E9 | sign test always significant | killed |
| E10 | isolation not restored | killed |
| E11 | API key not removed during runs | killed |
| E12 | provenance classes summed | killed |
| E13 | safety-blocked executions used as latency samples | killed |
| E14 | CLI writes into protected repository folders | killed |
| E15 | different repetition counts accepted | killed |
| E16 | fingerprint ignores the retry budget | **survived first**; test fixed (it compared different dataset sets); killed |

Final: 16/16 detected.

## P4.1 (lazy-seaborn V2 sandbox, variants, A/B gates)

Run against `tests/v2/p4/test_lazy_seaborn_p41.py`, with each mutant on a throw-away copy.

| # | Deliberate defect | Result |
|---|---|---|
| L1 | seaborn imported eagerly | killed |
| L2 | seaborn colormaps not pre-registered | killed |
| L3 | rewrite applied to user code too | killed |
| L4 | V1 prelude shape not checked | killed |
| L5 | V1 warning precedence not restored after the lazy import | killed |
| L6 | V2 executor skips the AST safety scan | killed |
| L7 | V2 timeout hard-coded instead of V1's | killed |
| L8 | V2 default chart path not V1's CHARTS_DIR | killed |
| L9 | isolation ignores the variant's executor | killed |
| L10 | A/B schedule not counterbalanced | killed |
| L11 | chart pixel differences ignored | killed |
| L12 | an increase in retries not treated as a regression | killed |
| L13 | control variant silently given the V2 executor | killed |
| L14 | proxy never rebinds `sns` to the module | killed |
| L15 | no eager fallback | killed |
| L16 | V2 subprocess environment differs from V1's (MPLBACKEND dropped) | killed |

Final: 16/16 detected.
