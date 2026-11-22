# P5.0 mutation checks

Each mutant was applied to a throw-away copy of the committed tree. The fast P5 tests were then run against it:
- `test_contracts_p50.py`;
- `test_verification_p50.py`;
- `test_execution_trust_p50.py`;
- `test_supervisor_p50.py`.

A mutant is detected ("killed") when at least one test fails. The first run used commit `679ae8f3`. The three survivors were re-run at `870ac959`, the commit that added their tests.

| # | Deliberate defect | Result |
|---|---|---|
| M1 | verifier accepts any reported value | killed |
| M2 | tolerance loosened to rel 1e-3 | killed |
| M3 | booleans equal numbers | killed |
| M4 | extra dict keys ignored | killed |
| M5 | dataset SHA-256 not checked | killed |
| M6 | non-finite values accepted | killed |
| M7 | feedback leaks the recomputed value | killed |
| M8 | NA group keys kept | killed |
| M9 | insight citations not checked | killed |
| M10 | insight numbers not checked | killed |
| M11 | duplicate CLAIM lines resolved last-wins | **survived first** (a later layer still rejected the wrong value); unit test added; killed |
| M12 | legacy port accepts untrusted code | killed |
| M13 | untrusted code resolved to a legacy port | killed |
| M14 | hardened placeholder falls back to the legacy subprocess | killed |
| M15 | analyst skips the trust check | killed |
| M16 | plan not validated | killed |
| M17 | `reply_to` not checked | **survived first** (end-to-end runs never produce a stale reply); single-step Supervisor test added; killed |
| M18 | envelope task/attempt mismatch not checked | **survived first** (the payload validator caught only payload-level mismatches); single-step test added; killed |
| M19 | verdict/status consistency not checked | killed |
| M20 | one extra retry beyond the V1 budget | killed |
| M21 | no approver means approve | killed |
| M22 | an edit may add tasks | killed |
| M23 | approval port receives the live plan | killed |
| M24 | step budget not enforced | killed |
| M25 | analyst parse errors ignored by the verifier | killed |
| M26 | agents may hand off to each other | killed |
| M27 | Pearson pairs not NA-filtered | killed |

Final: 27/27 detected.
