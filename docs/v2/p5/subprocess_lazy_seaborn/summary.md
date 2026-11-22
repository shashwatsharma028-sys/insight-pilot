# P5.0 multi-agent evaluation (subprocess, strategy lazy-seaborn)

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
| clean-sales | completed_with_failures | met | 18 | 3 | 1 | 0/7/0/0 | 3012.7 |
| clean-attendance | completed | met | 14 | 0 | 0 | 0/7/0/0 | 2914.5 |
| clean-student | completed | met | 14 | 0 | 0 | 0/7/0/0 | 2888.3 |
| clean-ecommerce | completed | met | 14 | 0 | 0 | 0/7/0/0 | 2339.6 |
| wrong-value-recovered | completed_with_failures | met | 20 | 4 | 2 | 1/7/0/0 | 3851.7 |
| wrong-group-and-label-recovered | completed_with_failures | met | 20 | 4 | 2 | 2/7/0/0 | 3590.5 |
| wrong-value-persistent | completed_with_failures | met | 16 | 2 | 0 | 3/6/0/0 | 3835.7 |
| missing-and-malformed | completed | met | 16 | 1 | 1 | 2/9/0/0 | 3201.7 |
| non-finite | completed | met | 16 | 1 | 1 | 1/8/0/0 | 3463.1 |
| duplicate-claim | completed | met | 16 | 1 | 1 | 1/9/0/0 | 3606.8 |
| type-confusion | completed | met | 16 | 1 | 1 | 1/9/0/0 | 3704.2 |
| within-tolerance | completed | met | 14 | 0 | 0 | 0/7/0/0 | 2160.9 |
| outside-tolerance | completed | met | 16 | 1 | 1 | 1/9/0/0 | 3110.3 |
| unplanned-claim | completed_with_failures | met | 18 | 3 | 1 | 0/7/0/0 | 3447.0 |
| insight-faults | completed_with_failures | met | 19 | 0 | 0 | 0/7/0/0 | 2248.3 |
| invalid-plan-recovered | completed | met | 15 | 0 | 0 | 0/7/0/0 | 2575.0 |
| invalid-plan-persistent | plan_invalid | met | 2 | 0 | 0 | 0/0/0/0 | 79.2 |
| human-reject | rejected_by_human | met | 2 | 0 | 0 | 0/0/0/0 | 8.2 |
| human-edit-drops-task | completed | met | 15 | 1 | 1 | 0/7/0/0 | 2703.9 |
| step-budget | budget_exhausted | met | 5 | 0 | 0 | 0/4/0/0 | 733.5 |
