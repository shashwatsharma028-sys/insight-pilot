# Retry & Failure Rules

Scope: how the system behaves when things go wrong — task failures, LLM
failures, and partial runs. The design principle: fail gracefully, never
silently, never infinitely.

## Retry budget (MUST)

1. Each task gets a retry budget (default 3, user-configurable 1–5).
   The budget MUST be enforced — no unbounded retry loops.
2. Every retry MUST carry the classified error type and the previous
   failing code to the generator. Blind regeneration is forbidden.
3. Rate-limit (429) waits MUST NOT count against the task's retry budget —
   they are infrastructure pauses, not code failures.
4. A task that exhausts its budget MUST be marked FAILED with its last
   error preserved, and the run MUST continue to the next task.
   One broken task MUST NOT kill the run.

## Failure transparency (MUST)

5. Failed tasks MUST appear in the final report with their error summary —
   never silently dropped.
6. Every attempt (success, retry, failure) MUST be logged to the execution
   timeline with timestamp, node, and detail.
7. If ALL tasks fail, the report MUST still generate, containing the quality
   assessment and the failure log — partial value beats no output.

## LLM failure handling (MUST / SHOULD)

8. Planner LLM failure → run MUST stop with a clear user-facing error
   (nothing downstream can work without a plan).
9. Interpreter LLM failure → MUST NOT fail the task. Fall back to raw
   execution output with confidence 0.3 and a stated caveat.
10. Per-minute rate limits → MUST wait the API-suggested delay + buffer,
    max 3 waits per call.
11. Daily quota exhaustion → MUST fail fast with actionable guidance
    (wait / switch model / new key). Waiting on a daily quota is forbidden.

## Degradation ladder (SHOULD)

When repeated failures suggest a systemic issue, degrade in this order
rather than failing the whole run:

1. Simplify: retry with a simpler analytical approach
2. Skip charts: retry computing text results only
3. Mark task failed, continue run
4. If >50% of tasks fail → surface a prominent warning that the dataset
   may be incompatible, with the dominant error type named
