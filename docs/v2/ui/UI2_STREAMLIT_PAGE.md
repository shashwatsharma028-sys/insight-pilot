# UI-2: V2 Streamlit page and UI-1 review corrections (engineering record)

**Branch:** `feature/v2-ui-streamlit-integration`, from `9531fdd6`, in its own worktree.
**Status:** implemented and tested offline with injected mock providers.
- **No live calls.** Every model reply comes from an injected mock.
- **Not wired to any production entry point:** no launcher or bootstrap change. The page is `v2/app_page.py`, **not** under `ui/`: three existing guards protect `ui/` as V1, which matches the project rule that V1 folders are not modified. `ui/app.py` is byte-identical.
- **Offline review:** the page runs locally through the test fixture `tests/v2/ui2/offline_app.py`.

## 1. UI-1 review corrections (`v2/app_service.py`), each with regression tests

| Review point | Correction |
|---|---|
| The approval view must be the exact plan | Claim keys, operations and arguments are passed through **exactly**: no clipping and no redaction, because they are what is approved and executed. A test rebuilds the plan from the view and compares it with the planner's plan. `plan_id` is computed from the exact plan |
| Structured results, explicit truncation | `ValueView(kind, value, truncated, total_entries, shown_entries)`. Mappings stay mappings; more than 500 entries are truncated **explicitly**; text over 10,000 characters likewise; nothing is silently turned into a string |
| Sensitive information | **Analytical data** (arguments, values, column names) is exact and escaped only when rendered. **Model prose** (titles, descriptions, insights) has control characters removed and credential-like tokens redacted. **Diagnostics** (errors, failures, validation problems) are `safe_text`: paths and secrets redacted, length clipped |
| Mock vs provider vs conservative usage | `usage_source` is `mock`, `provider`, `mixed` or `none`, with a matching `usage_label` (mock means synthetic tokens of about characters ÷ 4; provider means provider-reported tokens plus an estimate). `conservative_label` explains the worst-case charges for failed attempts, which are reported separately from successful calls |
| Malformed approval payloads | Non-string, unhashable or duplicate task IDs, non-JSON or NaN arguments, non-dict arguments, or a missing or empty task list end in `failed` / `invalid_plan_payload`, with nothing analysed. Non-string or unhashable `keep_task_ids` and non-string decisions are refused cleanly |
| Never stuck busy | Team construction, the planning call, approval-view mapping, resume and result mapping are all inside fail-closed handlers. A constructor failure deletes its directory. An upload storage failure keeps nothing and does not change the phase |
| Session cleanup | `close()`; a `weakref.finalize` that deletes the directory when the service is garbage-collected; a heartbeat file; and `cleanup_stale_sessions(base, max_idle_s)`, which only touches directories with this module's prefix **and** marker file. **Limitation:** a browser session that disappears keeps its files until GC, the stale-session cleanup or the end of the process |
| Cumulative limits survive resets | Unchanged and re-tested. `session_usage().limit_reached` now also reflects the runs-per-session limit, which UI-2 testing found was missing |
| Security claims | No claim of comprehensive multi-user security: per-session limits are not per-user limits |

## 2. Page architecture (`v2/app_page.py`)

- **Entry:** `render(service_factory, provider_label=None)`, with the service factory injected.
- **Sessions:** one `AnalysisService` per Streamlit session, in `st.session_state`. Nothing user-specific is cached at module or global level (tested with two concurrent sessions).
- **View models only:** the page renders the service's typed views; it never sees LangGraph state and doesn't orchestrate anything.
- **Sections:**
  1. **Dataset:** CSV upload with the limits shown, then rows, columns, missing values and a column-schema table.
  2. **Question:** a goal text area, and Start, enabled only with a dataset, a goal and remaining usage.
  3. **Plan for approval:** "Nothing has been computed yet"; each task's title, ID, type and description; and a table with each claim's exact JSON arguments and plain-language return semantics (from `claims.OP_RESULTS`). Abandoned tasks are listed. A multiselect picks the tasks to run, then "Approve selected tasks" or "Reject plan".
  4. **Results:** a status banner with the error code if any, the verification-scope disclaimer, and four tabs:
     - **Findings:** verified accepted values (scalar metrics and mapping tables, with an explicit truncation note), insights with "Based on: …" citations, and **review-only** results under a "NOT accepted results" warning;
     - **Tasks:** task statuses, attempts and failures; recovered and abandoned tasks;
     - **Activity:** recorded events, plus time by graph node and by agent, all recorded after the call returned;
     - **Usage:** the mock or provider label, attempts (successful and failed), tokens, estimated cost and conservative charges, and calls by skill.
  - **Sidebar:** cumulative session usage (analyses, attempts, tokens, estimated spend against the limits), plus Reset analysis (usage kept), Remove dataset and End session (files deleted; then "Start a new session").
- **Decisions:**
  - the buttons queue the action through `on_click` callbacks;
  - **`run_id` and `plan_id` are bound at render time**, so the service receives the identifiers of the plan the user saw;
  - the goal and task selection are read at click time;
  - the action runs under an honest spinner, then the page reruns;
  - duplicate, stale or late decisions are refused by the service and reported as warnings.
- **Theme:** colours come from Streamlit's built-in light and dark themes. One constant stylesheet only adjusts spacing and borders.

## 3. Security

- **No untrusted text in HTML:** dataset headers, file names, goals, model titles, descriptions, insights and messages are never interpolated into HTML.
  - Markdown is rendered only through `md()`, which escapes **every** ASCII punctuation character, so no links, images, HTML, emphasis, code, headings, autolinks or LaTeX can form.
  - Tables use dataframes, which render plain text.
  - The only `unsafe_allow_html` call injects the constant stylesheet (tested: exactly one HTML block, equal to the constant).
  - A `$` in labels is escaped too. Testing found `$0.0001 of $0.25` rendering as a LaTeX span; this is fixed and tested.
- **Tests:**
  - HTML and script tags, `onerror`, Markdown links and images, `javascript:` links, emphasis, code spans, headings, URLs, LaTeX, credential-like strings, unsafe paths and a malicious **file name** and **column header**;
  - checked on the workspace, plan and results screens;
  - nothing unescaped survives, and credentials are redacted from what a reader sees.
- **No internal leaks:** no raw tracebacks, internal paths, secrets, provider responses or graph state reach the page. Unexpected exceptions show "Something went wrong" or "could not start".
- **Purity:** no V1 executor, no benchmark imports and no scripted approvals in the runtime (import test in a fresh interpreter). No security guard was weakened.

## 4. Tests

- **`tests/v2/ui2/test_service_corrections_ui2.py`:** 40 tests for the review corrections.
- **`tests/v2/ui2/test_v2_page_ui2.py`:** 26 AppTest tests:
  - the offline label, upload and schema, invalid upload, and goal validation;
  - exact plan arguments, with no computation before approval;
  - approve with results, insights, activity and mock usage; reject; subset; duplicate and stale decisions; controls removed after a decision; the question kept after an analysis;
  - abandoned tasks, an invalid plan, review-only results never shown as accepted, and a provider failure;
  - cumulative usage and its escaped label, the run limit, end session and a new session, two isolated sessions, and remove dataset;
  - malicious strings and the escape function; safe messages for unexpected exceptions and factory failures;
  - import purity, and V1 byte-identical.
- **UI-1 suite:** 50 tests, still passing and unchanged. The page lives in `v2/app_page.py`, not `ui/`, so the V1 folder and its guards stay exactly as they were.
- **Mutation checks: 20 of 20 caught**, 12 on the page (including the two bugs found during testing: the LaTeX `$` label and the lost question) and 8 on the corrections. Three tests were strengthened after their first mutation run:
  - **Global caching:** the mutation was rewritten to really share the service between sessions.
  - **Raw exception text:** secret and path checks now read the **un-escaped** text a person sees.
  - **Stale-session cleanup:** the foreign directory now has an old heartbeat but no marker.

## 5. Known limitations

- **Synchronous calls:** the page blocks during planning and execution, and shows a spinner rather than live progress.
- **In-memory checkpoints:** an interrupted analysis needs a new run.
- **Not yet built:** charts, downloadable reports and follow-up questions are UI-3. There are no placeholder controls for them.
- **No live provider and no entry point:** both come in later reviewed checkpoints.
- **Abandoned browser sessions:** their files remain until GC, the stale-session cleanup or the end of the process.

## 6. Rollback

`v2/app_page.py` is new and not referenced by any entry point. Revert the branch commits. The service changes are corrections that are backward-compatible with the UI-1 API, and the UI-1 suite passes.
