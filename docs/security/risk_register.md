# InsightPilot: security risk register (P1)

**Scope.** InsightPilot as merged on `feature/insightpilot-v2` @ `6152da3a` (application code identical to `main` / `exam-backup-2026-11-17` @ `fd5783c6`). Line numbers refer to that tree.

**Method.** Manual code review, data-flow tracing, the 13 strict-xfail tests from P0, read-only inspection of the installed Streamlit 1.59.2 / python-multipart 0.0.32 sources (the exam versions), and read-only `git` history and GitHub metadata queries.

- No exploit payloads were run in P1.
- No live LLM calls were made.
- No application code was changed.

Companion documents: [threat model](threat_model.md), [abuse cases](abuse_cases.md), [HTML render audit](html_render_audit.md).

## Rating scales

| Field | Values |
|---|---|
| **Severity** | Critical / High / Medium / Low / Info. This is a judgement combining impact and likelihood for the *current single-user laptop deployment*; where multi-user exposure raises it, that is stated. |
| **Likelihood** | High: a normal user action or a single crafted input triggers it. Medium: needs a crafted file or model manipulation. Low: needs local access or unusual configuration. |
| **Impact** | What the attacker gains: code execution, secret theft, integrity loss, disclosure or denial of service. |
| **Exploitability** | Who can do it and with what. |
| **Validation test** | **A proposed test name for the fixing phase; it does not exist yet.** The only tests that exist today are the P0 tests named under "Existing tests" and in [Coverage by existing P0 tests](#coverage-by-existing-p0-tests). |
| **Evidence confidence** | **Confirmed-test**: an existing P0 test observes the insecure behaviour. **Confirmed-code**: a deterministic code path, read line by line, not executed in P1. **Plausible**: depends on runtime or third-party behaviour that has not been validated yet. **Future**: architectural risk for planned V2 features; not exploitable today. |

### OWASP references are provisional

The two official pages requested were unreachable from this session: `genai.owasp.org` failed DNS resolution, even after the host was added to the environment's network allowlist.

- **Agentic Applications.** IDs `ASI01`–`ASI10` below follow the *OWASP Top 10 for Agentic Applications 2026* as I know it.
- **LLM Top 10.** I could not confirm that an "LLM Top 10 2026" edition exists or what it contains. IDs below therefore use the **LLM Top 10 2025** list (`LLMxx:2025`).

**Both must be checked against the official pages before this register is cited externally.** All mappings are collected in one place, [threat_model.md § 9](threat_model.md#9-framework-mapping-provisional), so they can be corrected in a single pass.

## Summary

| ID | Title | Severity | Confidence | Phase |
|---|---|---|---|---|
| R-01 | API credential committed to public git history | **Critical** | Confirmed (presence); key validity unknown | Owner action now; P2 gate |
| R-02 | Generated code can read the parent process environment, including `GOOGLE_API_KEY` | **Critical** | Confirmed-test | P3 |
| R-03 | No OS-level isolation; AST denylist bypassable, so arbitrary code runs as the app user | **Critical** | Confirmed-test | P3 |
| R-04 | Generated code can read and write files anywhere the app user can | **High** | Confirmed-test | P3 |
| R-05 | Generated code has unrestricted network access | **High** | Confirmed-test | P3 |
| R-06 | Dataset path interpolated into the unscanned wrapper (file-name code injection) | **High** | Confirmed-test (executor); Plausible (browser upload) | P3 (+P2) |
| R-07 | Uploaded file name not sanitised: path traversal on upload save and report/notebook paths | **High** | Confirmed-code (not executed) | P2 |
| R-08 | Approval enforced by the UI only; graph proceeds past `await_approval`; toggle removes gate | **High** | Confirmed-test | P8 |
| R-09 | Approval scope gaps: edited plan and generated code run without review; no audit record | Medium | Confirmed-code | P8 |
| R-10 | Indirect prompt injection from CSV values, headers and file name into planner/code-generator prompts | **High** | Confirmed-code (data flow); Plausible (model behaviour) | P2 (+P6) |
| R-11 | Persistent memory poisoning through `lessons/lessons.json` | Medium | Confirmed-code | P2 (+P8) |
| R-12 | Second-order injection: sandbox stdout/stderr reaches interpreter, retry prompts, insights and chat | Medium | Confirmed-code | P2 (+P6) |
| R-13 | HTML injection: untrusted text in 15 `unsafe_allow_html` sites | Medium | Confirmed-code (HTML); Plausible (script execution) | P2 |
| R-14 | Sensitive-data disclosure through tracebacks, logs, reports, exports and provider prompts | Medium | Confirmed-code | P2 + P5 |
| R-15 | Notebook export: `csv_path` in a quoted string (code injection on later run); exported code runs unsandboxed | Medium | Confirmed-code | P2 (+P3) |
| R-16 | Unbounded consumption: no cap on plan size, prompt growth, run budget or provider back-off | Medium | Confirmed-code | P2 + P5 |
| R-17 | No sandbox resource limits beyond 30 s wall clock | Medium | Confirmed-test (memory) | P3 |
| R-18 | Unauthenticated server on all interfaces; process-global state shared across sessions | Medium (High on shared networks) | Confirmed-code | P3 + P5 |
| R-19 | LLM-controlled task id used as chart file name (path traversal) | Medium | Confirmed-test | P2 + P3 |
| R-20 | Chart path spoofable through a `CHART_SAVED:` line in generated stdout | Low | Confirmed-code | P3 |
| R-21 | ReportLab markup injection in PDF export | Low | Plausible | P2 |
| R-22 | Markdown rendering of sandbox output in progress status messages | Low | Plausible | P2 |
| R-23 | Supply chain: unpinned dependencies, committed binary `venv/`, prompt files trusted without integrity checks | Low | Confirmed-code | P3 |
| R-24 | Governance rules overstate enforced sandbox controls | Info | Confirmed-code | P3 |

**Totals:** 3 Critical, 7 High, 9 Medium, 4 Low, 1 Info. 13 findings map to the 13 existing strict-xfail tests, which cover R-02, R-03, R-04, R-05, R-06, R-08, R-17 and R-19. Future architectural risks are listed separately in [§ F](#f-future-architectural-risks-not-currently-exploitable).

---

## Findings

### R-01: API credential committed to public git history

- **Severity / likelihood / impact:** Critical / High / quota theft, billing abuse, impersonation of the project's Gemini usage.
- **Exploitability:** anyone on the internet. The repository is **public** (GitHub API: `"visibility":"public"`).
- **Evidence (Confirmed for presence):**
  - Commit `ed13ea82` (2026-07-19) added `.env` with keys `GOOGLE_API_KEY` (53-character value, not a placeholder) and `LLM_MODEL`.
  - Commit `34df73df` (2026-08-13) deleted the file, but the blob remains reachable from `origin/main`, `origin/exam-backup-2026-11-17`, `origin/feature/insightpilot-v2` and other branches.
  - The value does not match the classic `AIza…` Google key format, so P1 cannot tell whether it is or was a live key. The value was **not** printed, used or tested.
  - The project's own rule `rules/safety.md:22-24` classifies a committed secret as "a sev-1 defect requiring immediate key rotation".
- **Affected:** git history (`ed13ea82:.env`), `.gitignore:2` (added later), `utils/llm.py:13` (`load_dotenv()`).
- **Existing tests:** none.
- **Mitigation:**
  1. **Owner action, now:** revoke that key in Google AI Studio / Cloud Console, check its usage and billing history, and issue a new key. This needs no git history rewrite, so it is compatible with `CLAUDE.md`'s ban on rewriting `main` / the exam backup.
  2. Updating the exam laptop's `.env` with the new key is the owner's decision; until then the old key, if still in use, stays exposed.
  3. Enable GitHub secret scanning and push protection.
  4. P2: add a secret-scanning gate for new commits.
- **Validation test:** a CI job that scans the HEAD tree and all new commits with a secret scanner and fails on findings (the revoked historical blob is allow-listed by hash after revocation is confirmed). A manual check that the old key returns an authentication error.
- **Phase:** immediate owner action; P2 (gate).

### R-02: Generated code can read the parent environment, including `GOOGLE_API_KEY`

- **Severity / likelihood / impact:** Critical / Medium (needs injected or hallucinated malicious code, see R-10) / API-key theft.
- **Exploitability:** anyone who can influence the generated code: a crafted CSV (R-10), poisoned lessons (R-11) or a compromised model response.
- **Evidence:**
  - `sandbox/executor.py:115-121` runs the subprocess with `env={**os.environ, 'MPLBACKEND': 'Agg'}`.
  - `utils/llm.py:13` loads `.env` into `os.environ`, so the key is in the child's environment.
  - The denylist does not stop `pd.io.common.os.environ` (R-03).
- **Existing test:** `tests/v2/test_v1_security_baseline.py::test_sandbox_cannot_read_parent_secrets` (strict-xfail; observed reading a dummy value).
- **Mitigation:**
  - Start the sandbox with an explicit minimal environment containing no credentials.
  - In the secure profile, run it in a container with no secrets mounted.
  - Keep the API key out of the process that spawns sandboxes where practical.
- **Validation test (V2, must pass):** `tests/v2/security/test_hardened_secrets.py::test_hardened_sandbox_env_has_no_secrets`. With a dummy `GOOGLE_API_KEY` set in the parent and the hardened flag on, the probe's output does not contain the dummy value and `environ` contains only allowlisted keys. The V1 xfail stays as is.
- **Phase:** P3.

### R-03: No OS-level isolation; the AST denylist is bypassable

- **Severity / likelihood / impact:** Critical / Medium / arbitrary code execution as the Streamlit user on the exam laptop.
- **Exploitability:** as R-02.
- **Evidence:**
  - `agent/rules_engine.py:98-153`: a denylist of 14 import roots and 5 call names, checking only `ast.Import`, `ast.ImportFrom` and calls whose target is a bare `ast.Name`.
  - It does not inspect attribute chains (`pd.io.common.os`), `getattr` with computed strings, dunder traversal or modules outside the list (`pathlib`, `builtins`, `tempfile`, `glob`, `io` and others).
  - `sandbox/executor.py:115` runs the code with the app's own interpreter, user and permissions.
- **Existing tests:** `test_static_scanner_flags_bypass[pandas_reexported_os]`, `[getattr_builtin_eval]`, `[dunder_subclasses]`, `[pathlib_read]`, `[builtins_module]` (all strict-xfail).
- **Mitigation:**
  - Treat generated code as untrusted and run it inside OS/container isolation: no network, read-only root, a dataset-only bind mount, a write-only chart directory, a non-root UID, seccomp/rlimits.
  - The AST scan becomes defence in depth: an allowlist plus a dunder ban.
  - `config/feature_flags.py` already refuses a secure profile without the container backend.
- **Validation test (V2):** `test_hardened_sandbox_contains_bypass_corpus`. Each of the 5 bypass programs runs under the hardened backend, and none can read the dummy secret, a file outside the dataset, or open a socket; the probe outcome is asserted, not the scanner result.
- **Phase:** P3.

### R-04: Unrestricted filesystem read/write from generated code

- **Severity / likelihood / impact:** High / Medium / disclosure of any file readable by the user, and tampering with app code, `rules/`, `skills/`, `lessons/`, `data/` (persistence across sessions, damage to exam material).
- **Evidence:**
  - No filesystem confinement in `sandbox/executor.py:106-121`.
  - The pandas I/O functions `read_*` / `to_*` are not on the denylist (`agent/rules_engine.py:98-107`).
  - This contradicts `rules/sandbox_safety.rules.md:14-16`.
- **Existing tests:** `test_sandbox_cannot_read_files_outside_dataset`, `test_sandbox_cannot_write_outside_chart_dir` (strict-xfail).
- **Mitigation:** a container with read-only dataset mount, tmpfs scratch, a single writable chart directory and no access to the repository.
- **Validation test (V2):** `test_hardened_fs_read_denied`, `test_hardened_fs_write_denied`, and `test_hardened_cannot_modify_repo` (the probe's attempt to write `rules/` fails and the file hash is unchanged).
- **Phase:** P3.

### R-05: Unrestricted network egress from the sandbox

- **Severity / likelihood / impact:** High / Medium / exfiltration of secrets (R-02), dataset contents and files (R-04); download of further payloads.
- **Evidence:**
  - No network namespace or egress control in `sandbox/executor.py:115-121`.
  - `pd.io.common.urlopen` reaches the network.
  - The child also inherits proxy variables from `os.environ`.
- **Existing test:** `test_sandbox_cannot_open_network_connections` (strict-xfail, loopback listener owned by the test).
- **Mitigation:** container `--network none` (or an equivalent namespace), no proxy variables in the child.
- **Validation test (V2):** `test_hardened_network_denied`. A loopback listener receives no connection and DNS resolution of an external name fails inside the sandbox.
- **Phase:** P3.

### R-06: Dataset path interpolated into the unscanned wrapper

- **Severity / likelihood / impact:** High / Medium / code execution that **bypasses the AST scan entirely**, because the wrapper is never scanned.
- **Exploitability:** a user, or anyone who can get a user to upload a file, with a file named, for example, `report" if 0 else (…) or r"x.csv`. Such names are legal on macOS and Linux.
- **Evidence:**
  - `sandbox/executor.py:61`: `df = pd.read_csv(r"{csv_path}")`.
  - `ui/app.py:803`: `save_path = f"/tmp/{uploaded.name}"`, with the name unsanitised.
  - python-multipart 0.0.32 un-escapes `\"` in the uploaded file name (`multipart.py:214-215`), so a quote in a browser-sent file name reaches `uploaded.name`.
- **Existing test:** `test_dataset_path_cannot_inject_code` (strict-xfail at the executor level).
- **Not yet validated:** an end-to-end upload through a real browser.
- **Mitigation:**
  - Never interpolate data into generated source.
  - Pass the dataset path as an argument or environment variable, or mount it at a fixed path (P3).
  - Save uploads under a server-generated name (P2, see R-07).
- **Validation test (V2):** `test_hardened_wrapper_has_no_interpolated_paths`, with the crafted path from the V1 test; the marker is not printed and the wrapper source contains no user-derived bytes. Plus `test_upload_saved_under_generated_name` (P2).
- **Phase:** P3 (+P2).

### R-07: Upload file name not sanitised (path traversal)

- **Severity / likelihood / impact:** High / Low–Medium (a browser sends only a base name; a crafted multipart request is needed) / write of an attacker-chosen `.csv`-named file anywhere the user can write (for example overwriting `data/*.csv` demo datasets). Report and notebook written outside `reports/`.
- **Exploitability:** anyone who can reach the Streamlit port (R-18); XSRF protection still applies.
- **Evidence (Confirmed-code, not executed):**
  - `ui/app.py:801-807` writes to `/tmp/{uploaded.name}` and derives `dataset_name` from the same name.
  - Streamlit 1.59.2 `web/server/starlette/starlette_routes.py:742-746` stores `name=upload.filename` unchanged.
  - python-multipart strips only Windows drive/UNC prefixes (`multipart.py:218-219`), not `../`.
  - `dataset_name` is reused in `agent/nodes/report_generator.py:154, 234` output paths.
- **Existing tests:** none.
- **Mitigation:**
  - Save to a per-session directory under a random name; keep the original name only as an escaped display label.
  - Derive report/notebook names from a sanitised slug.
  - Reject names containing path separators or control characters.
- **Validation test:** `test_upload_name_traversal_rejected`, which drives the upload handler with `../../x.csv` and asserts no file outside the session directory. Plus `test_report_paths_stay_in_reports_dir` with `dataset_name="../../x"`.
- **Phase:** P2.

### R-08: Approval enforced only by the UI

- **Severity / likelihood / impact:** High / Medium (any non-UI caller, or the user switching the toggle off) / generated code runs with no human review.
- **Evidence:**
  - `agent/graph.py:73` defines `await_approval` as a pass-through lambda.
  - `agent/graph.py:99` adds an unconditional edge `await_approval → generate_code`.
  - `agent/graph.py:30-36` skips the gate when `safe_mode` is false.
  - `ui/app.py:824` exposes the toggle; the pause exists only because the UI breaks out of `analyst_graph.stream` (`ui/app.py:948-966`).
- **Existing tests:** `test_graph_does_not_generate_code_without_approval` (strict-xfail); `tests/v2/test_e2e_golden.py::test_safe_mode_stream_pauses_at_approval_before_any_code` (passes, documents the UI pause).
- **Mitigation:**
  - A graph-level interrupt (LangGraph `interrupt()`) that requires a recorded approval token bound to the exact plan hash.
  - The secure profile forbids disabling approval.
- **Validation test (V2):** `test_hitl_v2_graph_blocks_without_approval`: invoking the V2 graph without an approval record never reaches `generate_code`. `test_hitl_v2_rejects_replayed_or_mismatched_approval`.
- **Phase:** P8.

### R-09: Approval scope gaps

- **Severity / likelihood / impact:** Medium / Medium / the user approves plan A but plan B (LLM-rewritten from their edit) runs. Generated code is never reviewed. No audit record exists.
- **Evidence:**
  - `ui/app.py:1026-1040`: when edits are present, `planner_node` produces a new plan that is executed immediately, without being displayed.
  - Approval is never shown code.
  - No approval record is stored in state or on disk.
- **Existing tests:** none (P0 `test_ui_plan_edit_replans_before_running` documents the current behaviour).
- **Mitigation:**
  - Re-display and re-approve any regenerated plan.
  - Bind approval to a plan hash.
  - Optional risk-tiered code review.
  - Append-only approval log.
- **Validation test:** `test_edited_plan_requires_second_approval`; `test_approval_record_contains_plan_hash_actor_time`.
- **Phase:** P8.

### R-10: Indirect prompt injection from dataset content

- **Severity / likelihood / impact:** High / Medium / attacker-chosen analysis code, which chains into R-02 to R-05, plus manipulated plans and insights.
- **Exploitability:** anyone who supplies a CSV to a user (cell values, header names, file name).
- **Evidence (Confirmed-code for the data flow):**
  - `agent/nodes/data_ingestion.py:73-80` puts the first 5 rows, column names and the dataset name into `df_summary`.
  - Those reach the planner (`agent/nodes/planner.py:128, 148, 164`) and code generator (`agent/nodes/code_generator.py:134, 146`) with no delimiting, quoting or "data, not instructions" framing.
  - Task titles and descriptions written by the planner flow into the code prompt (`code_generator.py:137-138, 149-150`).
- **Not yet validated:** whether the configured Gemini model obeys such text. That needs an approved evaluation; budget is 0.
- **Existing tests:** none.
- **Mitigation:**
  - Quote and delimit data blocks; truncate cell values and sanitise column names.
  - An injection heuristic (monitor, then enforce).
  - Most importantly, make R-02 to R-05 harmless (P3), so a successful injection cannot reach secrets, files or network.
  - P6 verifier checks results against independently computed values.
- **Validation test:** `test_prompt_builder_quotes_dataset_content`, which feeds the injection corpus (`tests/security/corpus/`) through the prompt builder and asserts the data sits inside delimiters with role framing. `test_injection_corpus_flagged_in_monitor_mode`. With mocked LLM output that obeys the injection, the hardened sandbox contains it (ties to R-03).
- **Phase:** P2 (+P3, P6).

### R-11: Persistent memory poisoning through the lesson store

- **Severity / likelihood / impact:** Medium / Medium / persistent instructions injected into **every future planner prompt**, across datasets and sessions.
- **Evidence:**
  - User plan-edit text (first 200 characters) is stored in the `cause` field (`agent/reflection_engine.py:104-116`, called from `agent/nodes/planner.py:112-115`).
  - LLM-chosen task titles and the first 200 characters of sandbox stderr are stored in `issue` / `cause` (`reflection_engine.py:72-73, 83-84`). Generated code fully controls its stderr.
  - Storage is append-only with no size cap, validation, provenance or locking (`:121-133`).
  - Retrieval ranks same-dataset-name first, then newest, so other datasets' lessons are included (`:149-169`).
  - Lessons are injected verbatim (`:172-183`, `planner.py:118-120, 171`).
- **Existing tests:** `tests/v2/test_e2e_golden.py::test_reflection_lessons_written_to_isolated_store_only` (proves the write path); `test_ui_plan_edit_replans_before_running` (user correction is stored).
- **Mitigation:**
  - Store only structured, enumerated fields (error type, task type, counts), never free text from stderr, titles or user edits.
  - Provenance and per-dataset scoping, a size cap, file locking.
  - Quote lessons as data in prompts.
  - Optionally require approval before a lesson becomes active (P8).
- **Validation test:** `test_lesson_store_rejects_free_text_from_stderr`, which feeds a task whose stderr contains an injection marker and asserts the marker never appears in the stored lesson or in the next planner prompt. Plus `test_lessons_scoped_to_dataset` and `test_lesson_store_bounded`.
- **Phase:** P2 (+P8).

### R-12: Second-order injection through sandbox output

- **Severity / likelihood / impact:** Medium / Medium / manipulated interpretations, insights and chat answers; injection into retry prompts and lessons.
- **Evidence:**
  - Stdout goes to the interpreter prompt (`agent/nodes/interpreter.py:72-73`).
  - Stderr goes to retry prompts (`agent/nodes/code_generator.py:41, 58, 77, 93`; the previous code at `:50, 69, 85, 97`).
  - Raw output reaches the interpretation fallback (`interpreter.py:128`).
  - Insights and interpretations reach the follow-up prompt (`agent/memory/conversation.py:46-75`), Markdown report and PDF (`agent/nodes/report_generator.py:72-97`).
- **Existing tests:** none.
- **Mitigation:**
  - Treat tool output as untrusted: delimit, truncate, strip control and markup.
  - P6 verifier compares claims with independently computed values.
- **Validation test:** `test_execution_output_is_quoted_in_interpreter_prompt`; `test_verifier_flags_claim_not_supported_by_reference` (P6).
- **Phase:** P2 (+P6).

### R-13: HTML injection in the Streamlit UI

- **Severity / likelihood / impact:** Medium / Medium / spoofed UI, phishing links, remote-image beacons that can carry data. **Script execution is not established**: Streamlit bundles DOMPurify, but whether it applies to `st.markdown(..., unsafe_allow_html=True)` was not verified.
- **Evidence:** 15 of 62 `unsafe_allow_html=True` sites interpolate untrusted text without escaping, for example:
  - chat messages, user and LLM (`ui/app.py:719`, rendered at `:1210, :1359`);
  - log detail and action (`:701, :1343-1352`);
  - CSV column names (`:1267, :1275, :1284`, `:1000`);
  - LLM task fields (`:1009, :1163, :1175, :1241`);
  - file name (`:784`, `:609-611` through `:909, :1123`).

  The full inventory is in [html_render_audit.md](html_render_audit.md).
- **Existing tests:** none.
- **Mitigation:** one escaping helper for every interpolated value (`html.escape`, or `md_inline` where formatting is wanted); a lint test that forbids f-string interpolation in `unsafe_allow_html` calls unless wrapped.
- **Validation test:** `test_render_helpers_escape_untrusted` (unit: each helper given `<img src=x>` returns escaped text); `test_no_unescaped_interpolation_in_unsafe_html` (AST lint over `ui/app.py`); a browser test for script execution (needs approved tooling).
- **Phase:** P2.

### R-14: Sensitive-data disclosure through UI, logs, reports, exports and provider prompts

- **Severity / likelihood / impact:** Medium / High / paths, usernames, data samples and error text disclosed to viewers and recipients of exports.
- **Evidence:**
  - Full tracebacks in the browser (`ui/app.py:973-974, 1103-1104`).
  - The absolute `csv_path` (which can include the OS username) in the notebook (`agent/nodes/report_generator.py:130`).
  - Error excerpts in the Markdown report (`report_generator.py:90`); log `detail` with stdout/stderr excerpts (`agent/nodes/executor.py:85, 108`).
  - Provider console prints (`utils/llm.py:83`).
  - The first 5 rows of every dataset sent to Gemini (`agent/nodes/data_ingestion.py:73-80`), with no notice or redaction option.
- **Existing tests:** none.
- **Mitigation:**
  - Generic UI errors with details in a local log.
  - Path scrubbing in all exports (not just display).
  - Redaction and opt-out for data samples sent to the provider.
  - P5 trace redaction by default.
- **Validation test:** `test_exports_contain_no_absolute_paths`; `test_ui_error_shows_no_traceback`; `test_prompt_sample_redaction_option`; `test_trace_export_has_no_raw_rows_or_secrets` (P5).
- **Phase:** P2 + P5.

### R-15: Notebook export injection; exported code runs unsandboxed

- **Severity / likelihood / impact:** Medium / Low–Medium (the user must open and run the notebook) / code execution in the user's Jupyter with full privileges.
- **Evidence:**
  - `agent/nodes/report_generator.py:127-131` builds `df = pd.read_csv('{csv_path}')` in single quotes, so a file name containing `'` breaks out of the string.
  - `:135-145` exports LLM-generated code that will run outside any sandbox when the notebook is executed.
- **Existing tests:** none.
- **Mitigation:**
  - Use a literal-safe representation (`repr()` of a sanitised relative name).
  - Add a warning cell; optionally export code as non-executing Markdown in the secure profile.
- **Validation test:** `test_notebook_setup_cell_is_literal_safe` (crafted name parses as a single string literal); `test_notebook_contains_untrusted_code_warning`.
- **Phase:** P2 (+P3).

### R-16: Unbounded consumption

- **Severity / likelihood / impact:** Medium / Medium / API cost, long hangs, UI unavailability.
- **Evidence:**
  - Plan size is uncapped: the prompt asks for 5 tasks (`agent/nodes/planner.py:45`), but `_parse_plan` accepts any number (`:60-94`).
  - Up to 6 code-generation attempts per task (`ui/app.py:826`, `agent/nodes/executor.py:72`), each re-sending the full stderr and previous code (`code_generator.py:35-97`).
  - No per-run token or cost budget.
  - Back-off sleep taken from the provider's message is uncapped (`utils/llm.py:78-84`).
  - The whole CSV is re-read on every rerun (`ui/app.py:775, 908`); default 200 MB upload limit; no row/column limits.
- **Existing tests:** none.
- **Mitigation:**
  - Cap tasks, retries, prompt sizes and sleep.
  - A per-run budget with a fail-closed stop (P2).
  - Measure per run and session (P5).
- **Validation test:** `test_plan_capped_at_5_tasks` (mock returns 50); `test_retry_prompt_truncated`; `test_run_stops_at_budget`; `test_backoff_sleep_capped`.
- **Phase:** P2 + P5 (optimisation later in P9).

### R-17: No sandbox resource limits beyond 30 s wall clock

- **Severity / likelihood / impact:** Medium / Medium / memory, disk or process exhaustion of the exam laptop.
- **Evidence:** `sandbox/executor.py:17, 115-121`: only `timeout=30`; no memory, CPU, file-size or process limits (`rules/sandbox_safety.rules.md:26-27` lists them as SHOULD).
- **Existing test:** `test_sandbox_memory_is_limited` (strict-xfail, 300 MB allocation succeeded).
- **Mitigation:** container limits on memory, CPU, PIDs and disk.
- **Validation test (V2):** `test_hardened_memory_limit`, `test_hardened_pid_limit`, `test_hardened_disk_quota`.
- **Phase:** P3.

### R-18: Unauthenticated server and shared cross-session state

- **Severity / likelihood / impact:** Medium (High on shared networks such as exam or college Wi-Fi) / Medium / quota abuse, cross-session data mixing and disclosure.
- **Evidence:**
  - No `.streamlit/config.toml` is tracked. In Streamlit 1.59.2, `server.address` defaults to unset (`config.py:916-927`), so it listens on all interfaces. There is no authentication.
  - Process-global state:
    - token monitor (`agent/token_monitor.py:134`);
    - lesson store (R-11);
    - upload paths `/tmp/{name}` (`ui/app.py:803`);
    - chart files named by LLM task id (`sandbox/executor.py:47`, ids like `task_001` repeat across sessions);
    - report and notebook files named by dataset name (`report_generator.py:154, 234`).
- **Existing tests:** none. The isolation fixtures in P0 hide this by design.
- **Mitigation:**
  - Bind to `127.0.0.1` by default (documentation and secure-profile config).
  - Authentication for any shared deployment.
  - Per-session output directories and session-scoped monitors (P5).
- **Validation test:** `test_secure_profile_binds_localhost`; `test_two_sessions_do_not_share_charts_reports_or_lessons` (two AppTest sessions, same dataset and task ids).
- **Phase:** P3 (deployment) + P5 (session scoping).

### R-19: LLM-controlled task id used as chart file name

- **Severity / likelihood / impact:** Medium / Medium / chart written outside `reports/charts`, overwriting `.png` files the user can write.
- **Evidence:** the planner keeps the LLM-supplied `id` (`agent/nodes/planner.py:76`), which is used in `os.path.join(CHARTS_DIR, f"{task_id}.png")` (`sandbox/executor.py:47, 134`).
- **Existing test:** `test_task_id_cannot_escape_chart_dir` (strict-xfail).
- **Mitigation:**
  - Server-generated task ids, or validation against `^[A-Za-z0-9_-]{1,40}$` (P2 plan schema).
  - Fixed output mount (P3).
- **Validation test:** `test_plan_schema_rejects_unsafe_task_ids`; `test_hardened_chart_path_confined`.
- **Phase:** P2 + P3.

### R-20: Chart path spoofable through generated stdout

- **Severity / likelihood / impact:** Low / Medium / display of an arbitrary existing local image in the UI; spoofed chart attribution.
- **Evidence:** `sandbox/executor.py:128-131` takes the first `CHART_SAVED:(.+)` match from stdout without validation. The UI only checks `os.path.exists` (`ui/app.py:1179, 1408`), which blocks URLs but not other local files. The report uses only the basename under `reports/charts` (`report_generator.py:78-80, 198-203`).
- **Existing tests:** none.
- **Mitigation:** ignore stdout markers; accept only the server-defined chart path.
- **Validation test:** `test_chart_path_marker_ignored`.
- **Phase:** P3.

### R-21: ReportLab markup injection in the PDF export

- **Severity / likelihood / impact:** Low / Medium / malformed markup aborts PDF generation (handled as "PDF generation failed"). ReportLab's paragraph markup may load local images or links from injected tags.
- **Evidence:** `agent/nodes/report_generator.py` builds `Paragraph(...)` from report lines containing LLM, dataset and error text without XML escaping (Markdown is stripped, markup is not).
- **Not yet validated:** the exact tag support in ReportLab 5.0.0.
- **Existing tests:** none.
- **Mitigation:** `xml.sax.saxutils.escape` before `Paragraph`.
- **Validation test:** `test_pdf_paragraph_text_escaped` (an `<img src=…>` marker renders as text and the PDF builds).
- **Phase:** P2.

### R-22: Markdown rendering of sandbox output in progress status

- **Severity / likelihood / impact:** Low / Medium / a remote image beacon through Markdown image syntax in progress messages.
- **Evidence:** `ui/app.py:956` renders log `detail` (which contains stdout/stderr excerpts, `agent/nodes/executor.py:85, 108`) with `status_placeholder.info(...)`, which renders Markdown.
- **Not yet validated:** whether Streamlit renders images in `st.info`.
- **Existing tests:** none.
- **Mitigation:** escape Markdown in status text, or render it as plain text.
- **Validation test:** `test_status_text_markdown_escaped`.
- **Phase:** P2.

### R-23: Supply chain

- **Severity / likelihood / impact:** Low / Low / compromised or drifting dependencies; unreviewable binaries.
- **Evidence:**
  - `requirements.txt` uses only `>=` lower bounds.
  - `venv/` (25,242 files including macOS `.dylib` binaries) is tracked.
  - Prompts and rules are loaded from `skills/` and `rules/` and trusted as instructions (`agent/skills_loader.py`, `agent/rules_engine.py:51-70`), and they are writable by generated code (R-04).
- **Existing tests:** none.
- **Mitigation:**
  - A hash-pinned lock file for the secure profile.
  - Remove `venv/` from tracking (deferred cleanup, needs review).
  - Integrity check of prompt and rule files at start-up.
- **Validation test:** `test_secure_profile_requires_lockfile_hashes`; `test_prompt_files_match_manifest`.
- **Phase:** P3 (dependency changes need a review gate).

### R-24: Governance rules overstate enforced controls

- **Severity:** Info.
- **Evidence:**
  - `rules/sandbox_safety.rules.md:12-21` states "MUST NOT" network, file and subprocess access and lists an import *allowlist* (`:29-32`). Enforcement is only a denylist (R-03) and nothing for network or files.
  - The "Known gaps" section (`:40-46`) says AST scanning is not implemented, although it is (`agent/rules_engine.py:109`).
- **Mitigation:** align the documentation with the enforced controls when P3 lands.
- **Validation test:** a documentation review checklist in the P3 handoff.
- **Phase:** P3.

---

## F. Future architectural risks (not currently exploitable)

These concern V2 features that do not exist yet. They are design requirements, not confirmed vulnerabilities.

| ID | Risk | Introduced by | Requirement / acceptance test |
|---|---|---|---|
| F-01 | Inter-agent message injection or spoofing between planner, coder, analyst and verifier | P7 | Typed, schema-validated messages; agents never forward raw tool output as instructions. Test: `test_agent_contract_rejects_unschematised_message`. |
| F-02 | Unsafe tool delegation: a sub-agent gains the parent's privileges (sandbox, file, approval) | P7 | Per-agent tool allowlist; only the Coder can call the sandbox, only through the hardened backend. Test: `test_agent_tool_permissions_enforced`. |
| F-03 | Cascading failures: one agent's error or hallucination propagates and amplifies | P6/P7 | Deterministic supervisor routing, bounded retries, verifier gate. Test: `test_fault_injection_contained`. |
| F-04 | Verifier circularity: it trusts stdout or lets the LLM judge itself | P6 | The verifier compares claims with values recomputed from the dataset (P0 golden harness pattern). Test: `test_verifier_detects_seeded_wrong_claim`. |
| F-05 | Checkpoint or approval store tampering (persistent HITL state) | P8 | Integrity-protected records, approval bound to the plan hash, memory store only in tests. Test: `test_tampered_checkpoint_rejected`. |
| F-06 | Approval fatigue / human trust exploitation (persuasive plan text hides risky steps) | P8 | Risk-tiered review showing concrete actions and code diffs, not just LLM prose. Test: `test_high_risk_code_shown_verbatim`. |
| F-07 | Trace store becomes a sensitive-data sink | P5 | Redaction by default; no raw rows, secrets or full prompts. Test: `test_trace_redaction_defaults`. |
| F-08 | Rogue or looping agents in orchestration | P7 | Step and budget caps per run; kill switch. Test: `test_orchestrator_stops_at_step_cap`. |

## Coverage by existing P0 tests

All 13 are in `tests/v2/test_v1_security_baseline.py`, `strict=True, raises=AssertionError`. They characterise **V1** and stay unchanged: V2 fixes are proven by separate passing tests (see `docs/v2/BASELINE.md`).

| P0 test | Register ID |
|---|---|
| `test_static_scanner_flags_bypass[builtins_module]` | R-03 |
| `test_static_scanner_flags_bypass[dunder_subclasses]` | R-03 |
| `test_static_scanner_flags_bypass[getattr_builtin_eval]` | R-03 |
| `test_static_scanner_flags_bypass[pandas_reexported_os]` | R-03 |
| `test_static_scanner_flags_bypass[pathlib_read]` | R-03 |
| `test_sandbox_cannot_read_parent_secrets` | R-02 |
| `test_sandbox_cannot_read_files_outside_dataset` | R-04 |
| `test_sandbox_cannot_write_outside_chart_dir` | R-04 |
| `test_task_id_cannot_escape_chart_dir` | R-19 |
| `test_dataset_path_cannot_inject_code` | R-06 |
| `test_sandbox_cannot_open_network_connections` | R-05 |
| `test_sandbox_memory_is_limited` | R-17 |
| `test_graph_does_not_generate_code_without_approval` | R-08 |

Passing V1 controls that must keep passing:

- `test_v1_scanner_blocks_direct_dangerous_constructs` (8 cases): direct imports of `os`, `subprocess`, `socket` and `shutil`, plus `eval`, `exec`, `open` and `__import__`.
- `test_v1_timeout_constant_unchanged`.
- `tests/v2/test_e2e_golden.py::test_unsafe_generated_code_is_blocked_and_never_executes`.
- `test_safe_mode_stream_pauses_at_approval_before_any_code`.
