# P5.0 handoff demonstration (real V1 subprocess)

Case `wrong-value-recovered` from `docs/v2/p5/subprocess_v1/multi_agent.json`, at commit `e18c15f8`, on the sales dataset. The scripted analyst deliberately reports `target_attainment_pct` 1% too high on its first attempt for task `sales_t3`.

Reproduce it:

```bash
python -m evaluation multi-agent --out /tmp/p5demo --cases wrong-value-recovered
```

The trace has one line per envelope: sequence number, route, kind, [task #attempt] and a payload summary. It shows no values.

```
  1 supervisor -> planning_agent: plan_request | planning attempt 1; 0 earlier problem(s)
  2 planning_agent -> supervisor: plan | 4 task(s), 8 claim(s)
  3 supervisor -> approval_gate: approval_request | tasks sales_t1, sales_t2, sales_t3, sales_t4
  4 approval_gate -> supervisor: approval_decision | decision 'approve'
  5 supervisor -> data_analyst_agent: analysis_request [sales_t1 #1] | 4 claim spec(s); 0 feedback item(s)
  6 data_analyst_agent -> supervisor: analysis_result [sales_t1 #1] | failed; 0 claim(s), 0 parse error(s)
  7 supervisor -> data_analyst_agent: analysis_request [sales_t1 #2] | 4 claim spec(s); 1 feedback item(s)
  8 data_analyst_agent -> supervisor: analysis_result [sales_t1 #2] | ran ok; 4 claim(s), 0 parse error(s)
  9 supervisor -> verification_agent: verification_request [sales_t1 #2] | 4 reported value(s) to recompute
 10 verification_agent -> supervisor: verification_report [sales_t1 #2] | accepted: 4 verified
 11 supervisor -> business_insight_agent: insight_request [sales_t1 #1] | 4 verified claim(s) only
 12 business_insight_agent -> supervisor: insight_report [sales_t1 #1] | 3 insight(s)
 13 supervisor -> verification_agent: insight_check_request [sales_t1 #1] | 3 insight(s) to ground
 14 verification_agent -> supervisor: insight_check_report [sales_t1 #1] | accepted
 15 supervisor -> data_analyst_agent: analysis_request [sales_t2 #1] | 2 claim spec(s); 0 feedback item(s)
 16 data_analyst_agent -> supervisor: analysis_result [sales_t2 #1] | ran ok; 2 claim(s), 0 parse error(s)
 17 supervisor -> verification_agent: verification_request [sales_t2 #1] | 2 reported value(s) to recompute
 18 verification_agent -> supervisor: verification_report [sales_t2 #1] | accepted: 2 verified
 19 supervisor -> business_insight_agent: insight_request [sales_t2 #1] | 2 verified claim(s) only
 20 business_insight_agent -> supervisor: insight_report [sales_t2 #1] | 2 insight(s)
 21 supervisor -> verification_agent: insight_check_request [sales_t2 #1] | 2 insight(s) to ground
 22 verification_agent -> supervisor: insight_check_report [sales_t2 #1] | accepted
 23 supervisor -> data_analyst_agent: analysis_request [sales_t3 #1] | 1 claim spec(s); 0 feedback item(s)
 24 data_analyst_agent -> supervisor: analysis_result [sales_t3 #1] | ran ok; 1 claim(s), 0 parse error(s)
 25 supervisor -> verification_agent: verification_request [sales_t3 #1] | 1 reported value(s) to recompute
 26 verification_agent -> supervisor: verification_report [sales_t3 #1] | rejected: 1 refuted
 27 supervisor -> data_analyst_agent: analysis_request [sales_t3 #2] | 1 claim spec(s); 1 feedback item(s)
 28 data_analyst_agent -> supervisor: analysis_result [sales_t3 #2] | ran ok; 1 claim(s), 0 parse error(s)
 29 supervisor -> verification_agent: verification_request [sales_t3 #2] | 1 reported value(s) to recompute
 30 verification_agent -> supervisor: verification_report [sales_t3 #2] | accepted: 1 verified
 31 supervisor -> business_insight_agent: insight_request [sales_t3 #1] | 1 verified claim(s) only
 32 business_insight_agent -> supervisor: insight_report [sales_t3 #1] | 1 insight(s)
 33 supervisor -> verification_agent: insight_check_request [sales_t3 #1] | 1 insight(s) to ground
 34 verification_agent -> supervisor: insight_check_report [sales_t3 #1] | accepted
 35 supervisor -> data_analyst_agent: analysis_request [sales_t4 #1] | 1 claim spec(s); 0 feedback item(s)
 36 data_analyst_agent -> supervisor: analysis_result [sales_t4 #1] | blocked by safety scan; 0 claim(s), 0 parse error(s)
 37 supervisor -> data_analyst_agent: analysis_request [sales_t4 #2] | 1 claim spec(s); 1 feedback item(s)
 38 data_analyst_agent -> supervisor: analysis_result [sales_t4 #2] | blocked by safety scan; 0 claim(s), 0 parse error(s)
 39 supervisor -> data_analyst_agent: analysis_request [sales_t4 #3] | 1 claim spec(s); 2 feedback item(s)
 40 data_analyst_agent -> supervisor: analysis_result [sales_t4 #3] | blocked by safety scan; 0 claim(s), 0 parse error(s)
```

## What the trace shows

- **Every message goes through the Supervisor.** Each reply's `reply_to` matches the request it answers; the tests check this for every run.
- **Plan and approval.** The plan (4 tasks, 8 claims) is validated, then approved, before any code is written.
- **`sales_t1`: execution retry.** Attempt 1 fails with a KeyError: the golden fixture deliberately uses a wrong column name. The Supervisor retries with the error as feedback, and attempt 2 is verified.
- **`sales_t3`: verification retry.**
  - The Verification Agent recomputes the ratio from the CSV and **refutes** the reported value (seq 26).
  - The Supervisor feeds back "claim target_attainment_pct: refuted (differs from the independent recomputation)", without the correct value. This is checked by `test_analyst_feedback_names_the_failure_but_not_the_recomputed_value`.
  - Attempt 2 is verified (seq 30). Only then does the Business Insight Agent see the claim, and only its verified value.
- **`sales_t4`: safety-scan block.** The adversarial task tries `import os`. V1's AST safety scan blocks it three times, the V1 retry budget, so the task fails and none of its claims are accepted.

Outcome:
- status: completed_with_failures, because the adversarial `sales_t4` is blocked by design;
- verification: TP 1, TN 7, FP 0, FN 0;
- analysis retries: 4 (`sales_t1` 1, `sales_t3` 1, `sales_t4` 2); recovered tasks: 2 (`sales_t1`, `sales_t3`);
- pipeline: 6535.3 ms.
