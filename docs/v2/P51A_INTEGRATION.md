# InsightPilot V2 P5.1A: safe integration and contract compatibility

**Status:** P5.1A IMPLEMENTED on `feature/v2-p51-hardened-integration`.
- **Branch base:** P5 `1444491e`, with the P3 lineage (probe head `4cd107e0`) merged in.
- **Design:** `docs/v2/P51_READINESS_REVIEW.md`.

**What is still not built:**
- There is **no container runtime**.
- **P3.6 is not implemented.**
- The image is unchanged and still inert.
- Nothing executes untrusted code.

Hardened execution therefore fails closed outside the offline test suite. **TB3 isolation is not verified, and the application is not secure.**

## What changed

### Branch integration
- `git merge --no-ff 4cd107e0` into the new branch only; no conflicts.
- The merged tree is byte-identical to the tree that was simulated and tested before approval (`b6d88a8a`).

### One dispatcher, two callers
- **The container port.** `multi_agent.execution.ContainerExecutionPort` (alias `HardenedContainerExecution`) has no execution logic of its own. It calls the process-wide `v2.execution.dispatch.HardenedDispatcher` from `bootstrap.hardened_dispatcher()`, which is the same object that `agent.nodes.executor.execute_code` is bound to in hardened mode.
- **Fails closed.** Unless the bootstrap is `HARDENED` with an intact binding, the port raises `IsolationUnavailable` and starts nothing.
- **No second path.** P5.0's injectable backend, an unvalidated second path, was removed.

### Dataset identity (profiling, authorization, execution)
1. **Profiling.** The Supervisor profiles the host CSV, including its SHA-256.
2. **Authorization.** The dispatcher calls the new read-only `DatasetAuthorizer.identify()`. It uses the same descriptor-relative open and checks as `authorize_into`, without making a copy: demo-only pins, digest equal to the pin.
3. **Execution.** The P5 port passes the profiled digest as `expected_sha256`, and any mismatch is refused (`DatasetNotAuthorizedError`).
4. **No host path reaches the runtime.** The runtime receives only `ExecutionRequest.dataset = DatasetIdentity(name, sha256, size)`, never a path. When P3.6 stages the bytes, it must re-check them against this identity.

### Terminating failures
- **Handoff and statuses.** A new reply kind `execution_terminated` (allowed only from the analyst) carries two new terminal statuses:
  - **`isolation_unavailable`** for infrastructure failures:
    - `SandboxUnavailableError` and `SandboxProtocolError`;
    - `V1ExecutorReached` (the tripwire);
    - `IsolationUnavailable`;
    - `ExecutionInfrastructureError`, which unknown port failures and hardened results without container isolation become;
    - any other `InsecureConfigurationError`.
  - **`execution_refused`** for policy failures: `DatasetNotAuthorizedError` and other `SandboxPolicyError`, and `UntrustedCodeRefused`.
- **Who sets the category.** The trusted node wrapper derives it from the exception class, never from model output.
- **No retry.** The run ends at once: no further task and no insight call. A malformed termination also fails closed.
- **What is still retried.** Code errors, timeouts and memory-limit outcomes stay ordinary failed attempts, with feedback, as in V1.

### Only complete runs promote findings
- `final.run_complete` is True only for `completed` and `completed_with_failures`.
- For every other status, claims already verified by independent recomputation are kept under `verified_claims_for_review` (and insights under `insights_for_review`). `accepted_claims` and `insights` stay empty.
- The aborted task's unverified analysis is discarded.

### Routing controls
- **Legacy ports in a hardened process.** The V1 and P4.1 legacy ports refuse construction and execution in a `HARDENED` process, even for reviewed fixtures. `resolve_port` gives untrusted code, hardened flags or a hardened process only the container port.
- **Tripwire.** The P3 binding now also replaces `sandbox_v2.executor.execute_code` (the P4.1 lazy-seaborn executor) with the tripwire. The module is imported at install time, so a later import cannot bring back the real function. `verify`, `repair` and `uninstall` cover it, the P3 leak guard restores it, and the P5 accessor refuses while it has drifted.
- **Census.** The new census (`tests/v2/p51/test_integration_controls_p51a.py`) covers the whole repository, not only `agent/`, `ui/` and `utils/`. It pins every `execute_code` reference, including `setattr(..., "execute_code", ...)` strings, to an explicit allowlist with reasons, and it limits subprocess launch sites.

### Flags
- `IP_FF_ORCHESTRATOR=v2` is accepted by the bootstrap only together with `IP_FF_SANDBOX_HARDENED=1` and `IP_SANDBOX_BACKEND=container`.
  - On its own it is still refused, for two independent reasons.
  - With hardening, production still refuses ("no validated container backend").
- The P3.2a launcher (`v2/launch_app.py`) still refuses every non-default flag.
- Offline P5 tests with reviewed fixtures keep the legacy ports when hardening is off.

### Trusted prelude (contract only)
- `ExecutionRequest.prelude` (`PreludeId.V1` or `PreludeId.LAZY_SEABORN`) tells the future in-image runner which trusted prelude to apply.
- The generated code stays verbatim; no image was rebuilt.
- P4.1's `sandbox_v2/` and all P4/P5 evidence are byte-unchanged; `HARNESS_VERSION` is still `p4.1`.

### V2-only LangGraph environment
- `requirements-v2.txt` adds the exact tested trio: `langgraph==1.2.9`, `langgraph-checkpoint==4.1.1` and `langchain-core==1.4.9`.
- These equal environment B (`docs/v2/ENV_B_PINS.txt`), in which the whole suite below ran.
- `requirements.txt` (examination) is byte-identical to `fd5783c6`.
- `multi_agent.compat` checks the features it needs (`langgraph.types.interrupt`/`Command`, `InMemorySaver`). A too-old LangGraph fails with a message naming `requirements-v2.txt`.

## Contract changes (exact)

| Contract | Before | After |
|---|---|---|
| `ExecutionRequest` fields | `run_id, task_id, code, limits` | `+ dataset: Optional[DatasetIdentity] = None, prelude: PreludeId = V1` |
| `DatasetIdentity` | none | `name` (`^[a-z0-9_]{1,64}\.csv$`, never a path), `sha256` (64 hex), `size > 0` |
| `HardenedDispatcher.__call__` | `(code, csv_path, task_id)` | `(code, csv_path, task_id, *, expected_sha256=None, prelude=PreludeId.V1)`; V1 tuple unchanged |
| `ExecutionPort.execute` | `(code, dataset_path, task_id)` | `+ dataset_sha256=None` (legacy ports ignore it; the container port requires it) |
| P5 routes | 12 + `agent_error` | `+ (data_analyst_agent → supervisor, execution_terminated)` |
| `execution_terminated` payload | none | `{category ∈ {isolation_unavailable, execution_refused}, error_class ∈ the fixed set of that category, message ≤ 500 characters}` |
| `final` | `status, accepted_claims, insights, …` | `+ run_complete, verified_claims_for_review, insights_for_review, execution_terminations` |

## Narrow test-guard updates (no guard weakened)

| Guard | Change |
|---|---|
| P5 `test_no_protected_v1_path_differs_from_the_p41_base` | Only `config/feature_flags.py` may differ, and only with the approved P3 blob `dca46d69`. No other path may differ, and no untracked file may appear. |
| P3.4 `test_execution_contracts_are_unchanged` | The exact field list now includes `dataset` and `prelude`; still exact equality. |
| P3.3 `test_execution_request_still_carries_no_dataset` | Renamed `…_no_dataset_path`: exact fields, and `DatasetIdentity` refuses path-like names. |
| P3.3 `test_new_modules_read_no_environment_and_are_not_wired_into_production` | Only `dispatch.py` may import `DatasetAuthorizer`, and it may call only `identify()`. `authorize_into`, workspace and lease stay unwired everywhere. |
| P5 `test_multi_agent_requires_no_new_dependency` | Internal `v2` and stdlib `sys`/`importlib` added to the internal set; third-party set still exactly `{langgraph, langchain_core}`. |
| P5 placeholder tests (2) | Replaced by stricter ones: fail closed without a hardened bootstrap; no injectable backend; isolation failure terminates with no retry. |

## Validation

All runs were offline, with no API key, in environment B (CPython 3.11.18; langgraph 1.2.9, langgraph-checkpoint 4.1.1, langchain-core 1.4.9), from the real Git checkout.

| Run | Result |
|---|---|
| Before P5.1A: P5 branch | 551 passed, 13 xfailed |
| Before P5.1A: combined P5 + P3 tree | 1550 passed + 1 known guard conflict (C5), 13 xfailed |
| Full suite at `f82fea28` | **1602 passed, 13 xfailed, 0 failed, 0 skipped** (3 warnings: the known Streamlit AppTest stale-widget restart) |
| Full suite at `d7074884` (final code and tests; only this table row changed afterwards) | **1608 passed, 13 xfailed, 0 failed, 0 skipped** (3 warnings: the known Streamlit AppTest stale-widget restart) |
| New P5.1A tests (`tests/v2/p51/`) | 57 |
| Mutation checks (§ below) | 27/27 detected (3 after adding tests) |
| Real `streamlit run ui/app.py` (headless, no key) | health `ok`; Chromium page title "InsightPilot"; UI rendered; 0 Streamlit exceptions; 0 page errors |

The 13 strict V1 security xfails remain xfailed, with 0 tripwire hits.

### Mutation checks (detached worktree of the committed tree)

Every mutant was run against `tests/v2/p51/` plus the P5 trust, supervisor and contract tests.
- **First run:** 24/27 killed.
- **Survivors, each closed with a new test:**
  - N3, a dataset identity that may carry a path. It was covered only by a P3 test outside that set; a direct test was added.
  - N6, the orchestrator accepted without hardening. A second, independent refusal hid it; both reasons are now pinned.
  - N20, a hardened port reporting non-container isolation. A test with a lying hardened port was added.
- **Final: 27/27 killed.**

The other mutants cover:
- identity skipped or unchecked;
- tripwire, verify and accessor drift;
- legacy ports in hardened mode;
- `resolve_port`;
- the classification of both families;
- a fallback to the V1 subprocess;
- a missing profiled digest;
- termination ignored or not stopping;
- incomplete runs promoting findings;
- a malformed termination trusted;
- unknown errors retried;
- an open route;
- category/class consistency;
- `identify` skipping the digest;
- a request without identity.

## Security limitations (unchanged by P5.1A)

- **No container runtime exists (P3.6), and limits are not enforced (P3.7).** Nothing proves isolation; VE-2 has never been run.
- **The binding is routing, not isolation.** The tripwire turns a reached unisolated executor into a fail-closed error; it is not a sandbox.
- **Demo datasets only.** Hardened mode accepts only the four pinned demo datasets; uploads stay refused until P2.
- **Charts.** Charts from the hardened path are still refused (artifact extraction is P3.6).
- **Application scope.** P5.1A secures nothing at the application level: the secure profile is still refused, and P2 and P8 are not implemented.

## Rollback

- **Delete the branch.** No other branch was changed; `main`, `exam-backup-2026-11-17`, `feature/insightpilot-v2` and the P3, P4 and P5 heads are untouched.
- **Revert individual stages.** Alternatively, `git revert` the P5.1A commits on this branch (no history rewrite).
- **Operational rollback:** unset `IP_FF_SANDBOX_HARDENED`/`IP_FF_ORCHESTRATOR`. `streamlit run ui/app.py` never imports `v2` or `multi_agent`.
