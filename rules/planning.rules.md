# Planning Rules

Scope: analysis plans produced by the planner node.

## Hard rules (MUST / MUST NOT)

1. Plans MUST contain at most 5 tasks (API budget) and at least 3
   (below that, the analysis is too shallow to be useful).
2. Task 1 MUST always be descriptive statistics — every downstream
   interpretation depends on knowing the data's basic shape.
3. Every task MUST be executable against the actual dataset: it may only
   reference columns that exist in the dataset summary.
4. Plans MUST be valid JSON matching the AnalysisTask schema. No prose,
   no markdown fences around the JSON.
5. Each task MUST have a dataset-specific description (mentioning actual
   column/segment names), never generic filler like "analyze the data".
6. Plans MUST respect the user's edits verbatim when provided. A user's
   "skip X" MUST remove X; "add Y" MUST add Y. User edits override
   planner preferences.

## Selection logic (SHOULD)

7. If date/time columns exist → SHOULD include a trend analysis.
   Forecasting SHOULD only be added when ≥ 12 time periods exist.
8. If ≥ 2 numeric columns → SHOULD include correlation analysis.
9. If categorical grouping columns exist → SHOULD include one comparative
   (group-by) analysis on the most business-relevant grouping.
10. If the quality report flags outliers → SHOULD include outlier
    investigation scoped to the flagged columns.
11. Tasks SHOULD be ordered so later tasks can build on earlier findings
    (descriptive → structural → causal/anomaly).
12. Priorities SHOULD reflect decision value to a business user, not
    technical interestingness.

## Anti-patterns (MUST NOT)

13. MUST NOT plan analyses requiring data that doesn't exist (e.g. profit
    analysis when only revenue is present).
14. MUST NOT plan two near-duplicate tasks (e.g. "sales by region" and
    "regional sales comparison").
15. MUST NOT plan forecasting on non-temporal data or categorical targets.
16. MUST NOT exceed one forecast task per plan — forecasts are the most
    expensive and least reliable task type.
