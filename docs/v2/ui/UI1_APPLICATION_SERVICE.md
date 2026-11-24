# UI-1: V2 application service (engineering record)

**Branch:** `feature/v2-ui-application-service`, from `ff618163`, in its own worktree.
**Status:** implemented and tested offline with injected mock providers. **Not wired to any entry point**: no
launcher or bootstrap change, no Streamlit page, no live provider, and no live call.

## 1. What it is

`v2/app_service.py` (`AnalysisService`) is a UI-agnostic adapter that a future Streamlit page will call. It
**reuses, unchanged**, the real V2 runtime:
- `build_team`, which builds the Supervisor, Planning, Verification and Business Insight agents;
- the LangGraph approval gate (`Supervisor.start_interactive` / `Supervisor.resume`);
- `TrustedOpsAnalyst`, which runs no generated code and needs no execution port;
- `dataset.load` and `profile`;
- `BudgetedPort`, for budgets, limits and per-call records.

It adds only:
- input validation;
- a state machine;
- per-session isolation;
- typed, sanitised view models.

**API:**

| Method | Effect |
|---|---|
| `upload(filename, data)` | Validates and stores a CSV under a random private name → `DatasetView` |
| `start(goal)` | Validates the goal, then runs the real planner until the human approval gate → `ApprovalView` (or a `ResultView` if the run ended before approval) |
| `decide(run_id, plan_id, decision, keep_task_ids=None, note="")` | Approve, reject or keep a subset of tasks, for **the exact plan shown** → `ResultView` |
| `reset(clear_dataset=False)` | Discards the analysis; refused while planning or running |
| `close()` | Deletes every file of the session; refused while planning or running |
| `session_usage()` | Cumulative usage for the session → `SessionUsageView` |

The views are frozen dataclasses with `to_dict()` for JSON. They never expose raw graph state, filesystem paths,
tracebacks, credentials or raw provider responses.

## 2. Dependency injection

`AnalysisService(llm_port, config=ServiceConfig(), base_dir=None)`:
- **`llm_port`** is any object with `complete(skill, system, user)`, `call_source` and `code_trust` (in tests,
  `live_llm.FakeProvider`). The service builds no provider, reads no environment variable or key, and imports no
  V1, test or evaluation module (tested in a fresh interpreter).
- **The team:** `build_team(port, None, approval_port=None, analyst=TrustedOpsAnalyst())`. That means no
  code-generation port, genuine interactive approval (no scripted approver), and a new `InMemorySaver` and
  `thread_id` per analysis.
- **`ServiceConfig`:** model and prices, plus the four optimisation switches, **all off by default and independent**
  (`selective_recovery`, `schema_guidance`, `semantic_guidance`, `provider_retries`), plus `max_steps` and `Limits`.

## 3. State machine

```
idle ─start─▶ planning ─▶ awaiting_approval ─decide (valid)─▶ running ─▶ completed
                 │               │                                   ├─▶ completed_with_failures
                 └─▶ failed      └ invalid / stale / duplicate:      ├─▶ rejected
                                   refused, state unchanged          └─▶ failed
reset: any state except planning / running ─▶ idle        start: idle or any terminal state ─▶ planning
```

- **Locking:** every transition is taken under a lock.
- **Decisions:** a decision moves the phase to `running` **before** the graph resumes, so a second or concurrent
  decision is refused.
- **Mapping:** Supervisor statuses map to phases:
  - `completed` and `completed_with_failures` stay as they are;
  - `rejected_by_human` → `rejected`;
  - `plan_invalid`, `approval_failed`, `budget_exhausted`, `profile_failed`, `isolation_unavailable` and
    `execution_refused` → `failed`, each with a safe message.

## 4. Human approval and resume

- **Before approval:** the graph pauses at the LangGraph `interrupt` before any analysis. Tests show only planner
  calls happened and `TrustedOpsAnalyst.run` was never called. Planning keeps its existing bounded retries.
- **The exact plan:** `ApprovalView.plan_id` is the sha256 of the exact `interrupt` plan. A decision must quote
  `run_id` and `plan_id`; a different or outdated plan is refused (`stale`).
- **Decision validation:**
  - `edit` must keep a non-empty set of distinct task IDs from the plan;
  - `keep_task_ids` is refused with `approve` or `reject`;
  - any malformed decision is refused with no effect.
  - The Supervisor also re-validates the decision and fails closed.
- **Resume:** `Supervisor.resume` continues the **same graph and checkpoint**, and planning is not repeated (tested:
  one planner call per analysis).
- **Failure:** if planning or execution raises, the analysis ends `failed` and is **not continued**; a new run is
  required. Exactly-once execution across process failure is not claimed. Checkpoints are in memory only, and are
  approved for local offline development in this phase.

## 5. Isolation, resources and budget

- **Per-session directory:** each service has its own `tempfile.mkdtemp` directory (0700). The dataset is stored
  as `dataset-<uuid>.csv` (0600, created with `O_EXCL`); the uploaded filename is only a sanitised display name.
- **Upload checks:**
  - `.csv` only;
  - the size is checked **before writing** (default limit 10 MiB);
  - UTF-8 only;
  - `dataset.load` validation;
  - limits on rows (200,000), columns (60) and column names (64 printable characters);
  - goals of 3 to 1,000 characters, with no control characters.
- **Bounded storage:** a new upload replaces the previous file. `reset(clear_dataset=True)` removes it. `close()`
  deletes the whole session directory.
- **One `BudgetedPort` per session, with its ledger inside the session directory:**
  - spend, provider calls and tokens are **cumulative for the session** and are not reset by a new analysis;
  - so is the run count (default 20);
  - defaults: $0.25 estimated, 200 calls, 1M tokens;
  - a reached limit refuses new analyses, even after `reset()`.
- **Never used:** shared V1 report paths, `/tmp/<filename>`, or the process-wide V1 token monitor.

## 6. Error handling

- **Errors:** user-facing errors are `InvalidInput` or `TransitionRefused`, each with a `code` and a safe `message`.
- **Run outcomes:** these are `ResultView.phase`, `message` and `error_code` (`plan_invalid`, `budget_exhausted`,
  `usage_limit`, `provider_unavailable`, `internal_error`, ...).
- **Sanitising:** all displayed text passes `safe_text`, which removes control characters, redacts credential-like
  tokens, replaces filesystem paths and clips the length.
- **Unexpected exceptions:** they never reach a view; they become `internal_error` with a fixed message.
- **Incomplete runs:** these show verified values only as `review_only_claims`. `verified_claims` (the accepted
  results) is set only for complete runs.
- **Verified is not the same as correct:** `verification_scope` states that verification is an independent
  recomputation of the chosen calculation, not proof that the calculation answers the question as intended.

## 7. Tests (offline, injected mocks, new synthetic datasets and goals)

- **Test file:** `tests/v2/ui1/test_app_service_ui1.py`, 50 tests:
  - upload and goal validation, and a path-traversal filename;
  - pause with only the planner called; approve, reject and keep-subset; no re-planning;
  - invalid, stale, duplicate and concurrent decisions; concurrent start; reset and close refused while busy;
  - invalid plan; selective recovery (when enabled); incomplete runs showing review-only results only; provider
    failure with secret and path redaction;
  - session budget, call and run limits that survive resets; an unexpected exception failing closed with a new run
    still working;
  - two sessions with the same filename; a distinct thread and checkpoint per analysis; cleanup;
  - optimisations off by default and independently switchable;
  - no test, harness, V1 or executor imports, and a V1-executor tripwire;
  - V1 and protected modules unchanged; views free of paths and raw state.
- **Mutation checks:** 14 of 14 caught.
- **Results:**
  - **full offline regression: 2122 passed, 13 xfailed, 0 failed**;
  - Streamlit UI smoke and launcher tests: 53 passed;
  - headless `streamlit run ui/app.py` and `streamlit run v2/launch_app.py` (default flags): HTTP 200, no errors.
- **Guards:** `tests/v2/p3/test_contract_purity.py` now lists `v2/app_service.py` as a reviewed non-contract module.
  It is not wired to any entry point.

## 8. Known limitations

- **Checkpoints:** in memory only, so lost when the process ends, and there's no continuation after a failure.
- **Blocking:** `start` and `decide` are synchronous and block the caller (Streamlit will show a spinner); a
  background worker is out of scope.
- **Shared default ledger:** each session ledger is separate, but `BudgetedPort` itself is unchanged.
- **No live provider wiring** and no entry point yet: both are later reviewed checkpoints.
- **Not yet available:** charts, reports and follow-up questions (UI-2 onwards).

## 9. Rollback

The service is a new, unreferenced module. Revert the commit, or simply don't import it. No existing module was
changed except the guard's approved-file list.
