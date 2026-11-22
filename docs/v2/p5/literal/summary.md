# P5.0 multi-agent evaluation (literal, strategy v1)

Scripted agents; injected faults. Not evidence about real-model accuracy, tokens or cost.

- Cases: 20; expectations met: 20/20
- Verification: 143 labelled claim checks; TP 12, TN 131, FP 0, FN 0; accuracy 1.0; detection rate 1.0; false-rejection rate 0.0
- Incorrect claims accepted: 0
- Insight checks: TP 3, TN 49, FP 0, FN 0
- Clean runs: 28/28 golden answers accepted and correct
- Handoff replies: 286; accepted by contract 282; rejected 3; agent errors 1
- Analysis retries: 22; recovered tasks: 12; failed tasks: 5
- LLM calls: 154 (provider calls: 0)

| Case | Status | Expected | Requests | Retries | Recovered | TP/TN/FP/FN | Pipeline ms |
|---|---|---|---|---|---|---|---|
| clean-sales | completed_with_failures | met | 18 | 3 | 1 | 0/7/0/0 | 24.9 |
| clean-attendance | completed | met | 14 | 0 | 0 | 0/7/0/0 | 156.0 |
| clean-student | completed | met | 14 | 0 | 0 | 0/7/0/0 | 41.5 |
| clean-ecommerce | completed | met | 14 | 0 | 0 | 0/7/0/0 | 24.5 |
| wrong-value-recovered | completed_with_failures | met | 20 | 4 | 2 | 1/7/0/0 | 27.9 |
| wrong-group-and-label-recovered | completed_with_failures | met | 20 | 4 | 2 | 2/7/0/0 | 27.8 |
| wrong-value-persistent | completed_with_failures | met | 16 | 2 | 0 | 3/6/0/0 | 263.0 |
| missing-and-malformed | completed | met | 16 | 1 | 1 | 2/9/0/0 | 41.5 |
| non-finite | completed | met | 16 | 1 | 1 | 1/8/0/0 | 25.6 |
| duplicate-claim | completed | met | 16 | 1 | 1 | 1/9/0/0 | 231.5 |
| type-confusion | completed | met | 16 | 1 | 1 | 1/9/0/0 | 231.8 |
| within-tolerance | completed | met | 14 | 0 | 0 | 0/7/0/0 | 28.4 |
| outside-tolerance | completed | met | 16 | 1 | 1 | 1/9/0/0 | 19.9 |
| unplanned-claim | completed_with_failures | met | 18 | 3 | 1 | 0/7/0/0 | 19.5 |
| insight-faults | completed_with_failures | met | 19 | 0 | 0 | 0/7/0/0 | 20.9 |
| invalid-plan-recovered | completed | met | 15 | 0 | 0 | 0/7/0/0 | 28.9 |
| invalid-plan-persistent | plan_invalid | met | 2 | 0 | 0 | 0/0/0/0 | 56.7 |
| human-reject | rejected_by_human | met | 2 | 0 | 0 | 0/0/0/0 | 6.8 |
| human-edit-drops-task | completed | met | 15 | 1 | 1 | 0/7/0/0 | 17.6 |
| step-budget | budget_exhausted | met | 5 | 0 | 0 | 0/4/0/0 | 15.5 |
