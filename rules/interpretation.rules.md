# Interpretation Rules

Scope: all insights, interpretations, and business recommendations produced
by the interpreter node. These rules exist because an analyst that sounds
confident while being wrong is worse than no analyst.

## Honesty rules (MUST / MUST NOT)

1. Every claim MUST be traceable to the actual execution output. If the
   number isn't in stdout, it MUST NOT appear in the interpretation.
2. The interpretation MUST NOT invent causal explanations. "Sales dropped
   45% in March" is a finding; "because of seasonal demand" is speculation
   and MUST be labeled as a hypothesis if mentioned at all.
3. Single-period anomalies MUST NOT be described as trends, and trends
   MUST NOT be described as anomalies. (Lesson learned: the March 2024
   single-month drop was initially mischaracterized as a multi-quarter
   decline.)
4. If the execution output is empty, truncated, or ambiguous, the
   interpretation MUST say so and lower its confidence — never fill gaps
   with plausible-sounding content.
5. Confidence scores MUST follow the calibration table below, not vibes.
6. At least one real assumption MUST be stated per interpretation.
   "No assumptions" is never true in data analysis.

## Confidence calibration

| Score | Use when |
|---|---|
| 0.9 – 1.0 | Direct read of unambiguous computed values (counts, sums, extremes) |
| 0.7 – 0.89 | Clear pattern with adequate n; standard method; no data quality flags on the columns used |
| 0.5 – 0.69 | Pattern present but n is small, quality flags exist, or method has known caveats |
| 0.3 – 0.49 | Suggestive only; sparse output; interpretation partially inferred |
| < 0.3 | Should rarely ship — prefer reporting the task as inconclusive |

## Business framing (SHOULD)

7. Insights SHOULD be phrased with specific numbers ("returns rate 6.2% vs
   3.1% average"), never vague magnitude words alone ("significantly higher").
8. Each business insight SHOULD imply an action or decision, and the action
   SHOULD be proportionate to the confidence (low confidence → "investigate",
   high confidence → "act on").
9. When findings depend on flagged data quality issues (missing values,
   outliers in the analyzed columns), the insight SHOULD carry that caveat
   inline, not just in the quality section.
10. Language SHOULD be readable by a non-technical manager: no unexplained
    statistical jargon in the business_insight field.

## Output contract (MUST)

11. Responses MUST be valid JSON with exactly the fields:
    interpretation, business_insight, confidence_score, assumptions.
