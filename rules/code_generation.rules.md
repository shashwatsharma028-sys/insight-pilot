# Code Generation Rules

Scope: all Python code produced by the code generator node for analysis tasks.

## Hard rules (MUST / MUST NOT)

1. Code MUST use the pre-loaded `df` variable. It MUST NOT load files
   (`pd.read_csv`, `open()`) — the sandbox injects the data.
2. Charts MUST be saved to `CHART_PATH` via `plt.savefig(CHART_PATH,
   bbox_inches='tight')` followed by `plt.close('all')`.
3. Code MUST NOT call `plt.show()`, `input()`, or anything interactive.
4. Code MUST print its key findings to stdout with clear labels
   (e.g. `print("Top region:", name, f"${value:,.0f}")`). Unprinted results
   are lost — stdout is the only channel back to the agent.
5. Column access MUST use exact names from the dataset summary. Code MUST NOT
   guess or "normalize" column names.
6. Operations on columns with known missing values MUST handle NaN explicitly
   (dropna on the relevant subset, or fillna with a stated strategy).
7. Date columns MUST be parsed with `pd.to_datetime(..., errors='coerce')`,
   never assumed to already be datetime.
8. Output MUST be pure Python code — no markdown fences, no prose, no
   explanations outside comments.

## Quality standards (SHOULD)

9. One analysis per task — code SHOULD do the assigned task only, not bundle
   extra analyses.
10. Charts SHOULD have a title, axis labels, and readable tick formatting
    (thousands separators for currency).
11. Aggregations SHOULD state their basis in printed output
    (e.g. "monthly mean of daily sales", not just "sales").
12. Percentage changes SHOULD be computed on sorted chronological data —
    verify sort order before diff/pct_change.
13. Code SHOULD stay under ~60 lines. Longer means the task is too broad —
    that is a planning problem, not a coding problem.
14. Statistical claims (correlation, significance) SHOULD print the actual
    statistic and n, not just a verdict.

## Retry-specific rules

15. On retry, the fix MUST address the classified error type. A DATA error
    retry MUST re-check column names/types against the dataset summary;
    a SYNTAX retry MUST NOT change the analytical approach.
16. After 2 failed retries of the same approach, the retry MUST take a
    simpler approach (fewer transformations, more defensive checks) rather
    than repeating variations of the failing strategy.
