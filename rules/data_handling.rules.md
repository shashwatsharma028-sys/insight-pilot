# Data Handling Rules

Scope: how the system treats user-uploaded datasets throughout the pipeline.

## Privacy rules (MUST / MUST NOT)

1. Uploaded datasets MUST stay on the local filesystem. The raw dataset
   MUST NOT be uploaded anywhere or transmitted beyond what rule 2 permits.
2. Only the dataset PROFILE goes to the LLM: schema, dtypes, shape,
   aggregate statistics, and a capped sample (first 5 rows). The full
   dataset MUST NOT be sent to the LLM API.
3. If a dataset visibly contains sensitive identifiers (emails, phone
   numbers, government IDs, exact addresses), the sample sent to the LLM
   SHOULD mask those columns' values (keep column names, mask contents).
4. Generated reports and notebooks MUST NOT embed the full raw dataset —
   aggregates and derived charts only.
5. Temporary files created during execution MUST be deleted after each run
   (sandbox temp scripts are; uploaded files in /tmp SHOULD be cleaned on
   session reset).

## Integrity rules (MUST)

6. The original uploaded file MUST never be modified. All cleaning
   (dedup, imputation) happens on in-memory copies inside generated code.
7. Quality issues MUST be surfaced before analysis, not silently fixed:
   the user sees duplicates/missing/outlier counts in the approval screen.
8. Row/column counts reported to the user MUST come from the actual file
   read, not from LLM output.

## Scale limits (SHOULD)

9. Datasets above 100 MB or 1M rows SHOULD trigger a warning and sampled
   analysis rather than full-data analysis (subprocess memory ceiling).
10. Datasets with more than 100 columns SHOULD have the profile truncated
    to the most informative columns (highest variance numerics + highest
    cardinality-utility categoricals) to keep prompts within token budget.
