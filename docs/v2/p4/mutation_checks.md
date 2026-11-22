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
