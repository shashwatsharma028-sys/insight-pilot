> **Record status:** readiness review as delivered for review (2026-11-22), unchanged. The reviewer approved stage **P5.1A only** (integration branch, contracts, routing guards, V2-specific LangGraph pins); P3.6, VE-2, live models and benchmarking remain unapproved. Implementation notes for P5.1A: `docs/v2/P51A_INTEGRATION.md`.

# InsightPilot V2: P5.1 readiness review (P3 hardened execution integration)

**Type:** design and readiness review only. No code, dependency, sandbox, branch or default-pipeline change was made.
**Date:** 2026-11-22.
**Reviewed by:** reading the code at exact commits, plus read-only experiments in a scratch directory (listed in §0.2).

## 0. Verdict and evidence base

### 0.1 Verdict

**P5 is not ready to execute untrusted model-generated code, and nothing in this review changes that.** The blocking facts, all verified in code:

1. **No production container runtime exists.**
   - `v2/execution/runtime.py` defines only the `ExecutionRuntime` protocol.
   - `bootstrap.ensure_ready()` has no runtime parameter. It always refuses hardened mode: "no validated container backend exists in this build (P3.2b)".
   - The only path into `HARDENED` mode is `install_for_tests()`. It requires `"pytest" in sys.modules` and a runtime with `is_test_double = True`.
2. **The sandbox image is inert.**
   - `inert_entrypoint.py` prints one line and exits with status 78.
   - There is no in-container supervisor or runner (`/opt/ip/supervisor.py` appears nowhere in the code).
3. **No isolation-effect test exists.** These acceptance-matrix rows are all PLANNED:
   - ownership O-01…04;
   - storage S-01…05;
   - isolation I-01…11, I-13 and I-14;
   - resources R-01…06;
   - container cleanup C-xx;
   - G-03…G-06.

   No `@container` or `IP_TEST_REQUIRE_CONTAINER` test exists in `tests/`, `scripts/` or `v2/`.
4. **Independent rootless-host validation (VE-2) has never been executed.**
   - `docs/v2/P3_VALIDATION_ENVIRONMENT.md` says so itself.
   - The only container evidence comes from a GitHub-hosted runner whose account is root-equivalent; the CI report states this itself (§2.2).

**What is ready:** P3's host-side building blocks are implemented and unit-tested:
- contracts, flag rules, the dispatcher (V1-tuple contract) and the V1 tripwire;
- demo-only dataset authorization, workspace and lease;
- the IPX1 frame decoder, the PNG validator and re-encoder, and the atomic chart store.

P5's fail-closed placeholder works as designed. **The smallest safe P5.1** is therefore a contract-level integration behind test doubles: an adapter, error translation and routing guards, with production still failing closed. Real isolation waits for P3.6 + P3.7 and VE-2 evidence.

### 0.2 Exact state reviewed

| Ref | SHA | Notes |
|---|---|---|
| `feature/v2-p5-multi-agent` | `1444491e9fc6…` | P5.0 head; 551 passed + 13 xfailed (full suite at `e18c15f8`, the last code commit) |
| `feature/v2-p4-evaluation` | `d321785c9a89…` | P4.1 head (P5's base) |
| `feature/v2-p3-secure-execution` | `2d0a5a73d16b…` | P3.5b Phase A docs head |
| `feature/v2-github-ubuntu-probe` | `4cd107e0eeca…` | = P3 head + 7 commits (CI workflow, `rootless.py --passwd=false`, CI report, hermetic chart references, tests) |
| `main`, `exam-backup-2026-11-17` | `fd5783c6…` | untouched |
| `feature/insightpilot-v2` | `1fd3af3c…` | untouched; it is the merge base of both lineages |

The read-only experiments in this review:
1. **Detached worktrees.** Both P3 heads were checked out as detached worktrees in the scratchpad (removed afterwards).
2. **Merge simulation.** `git merge-tree --write-tree 1444491e 4cd107e0` creates a tree object only, with no ref or commit.
3. **Full suite on the combined tree.** The tree was exported to the scratchpad with `git archive` and the full offline suite was run there.
4. **LangGraph probes.** Throw-away venvs installed 27 combinations of LangGraph and checkpoint versions, and a probe of P5's LangGraph features was run in each.
5. **GitHub Actions record.** The run and job metadata and logs of the probe workflow were read.

---

## 1. Task 1: code and contract differences between P5 and P3

### 1.1 Lineages

- Both branches descend from `1fd3af3c`. P5 adds `sandbox_v2/`, `evaluation/`, `multi_agent/`, tests and docs.
- P3 adds:
  - `v2/`;
  - `scripts/p3/`;
  - `tests/v2/p3/`;
  - docs;
  - the probe workflow;
  - **one change to a shared file, `config/feature_flags.py`** (decision D1: `IP_FF_SANDBOX_HARDENED=1` now requires `IP_SANDBOX_BACKEND=container`; otherwise `InsecureConfigurationError`).
- **No file is changed by both lineages.** `git merge-tree` is clean (tree `b6d88a8a`).

### 1.2 Execution interfaces side by side

| Aspect | P5 `multi_agent/execution.py` | P3 `v2/execution/*` |
|---|---|---|
| Entry point | `ExecutionPort.execute(code, dataset_path, task_id) -> ExecutionOutcome` | `HardenedDispatcher(code, csv_path, task_id) -> (success, output, chart_path, error_type)`, the V1 tuple; calls `ExecutionRuntime.execute(ExecutionRequest) -> ExecutionResult` |
| Request | code, absolute host dataset path, `"<task>_a<attempt>"` | `ExecutionRequest(run_id 32 hex, task_id, code, limits)`. **No dataset field** ("dataset authorisation (P3.3) must exist before any runtime may receive data"; P3.3 now exists, but the request was never extended) |
| Result | `ExecutionOutcome(ok, stdout, error, error_type: str, chart_path: str, backend: str, isolation: str (free text), seconds: float, blocked_by_safety_scan)` | `ExecutionResult(run_id, isolation: IsolationLevel, outcome ∈ {SUCCEEDED, CODE_ERROR, TIMED_OUT, MEMORY_LIMIT, BLOCKED_BY_SCANNER}, stdout, stderr, *_truncated, chart_path, chart_rejected_reason ∈ 15 fixed codes, duration_ms: int)`, strictly validated |
| Isolation label | free-text string; `hardened: bool` | `IsolationLevel` enum with exactly two values; the dispatcher refuses anything but `ROOTLESS_CONTAINER` |
| Infrastructure failure | `IsolationUnavailable(RuntimeError)` from the placeholder | `SandboxUnavailableError` and `SandboxProtocolError`, which subclass `InsecureConfigurationError`; "never retried by asking the LLM for new code" |
| Policy failure | `UntrustedCodeRefused(PermissionError)`: trust gate | `SandboxPolicyError`, `DatasetNotAuthorizedError`, `ArtifactRejectedError(reason)` |
| Charts | V1 `CHART_SAVED`/default-path detection (legacy ports) | dispatcher **rejects any chart** (`SandboxProtocolError`: "artifact extraction is not implemented"); validator and store exist but are not wired in |
| Scanner | V1 AST scan inside the V1/V2 executors | dispatcher runs the same V1 scan first (defence in depth) |
| Routing guard | `CodeTrust`: legacy ports accept only `SCRIPTED_FIXTURES`; `resolve_port` gives untrusted code, or `sandbox_hardened` flags, only the hardened port; the analyst re-checks on every run | process-global `binding`: `agent.nodes.executor.execute_code` → the dispatcher; `sandbox.executor.execute_code` → `v1_executor_tripwire`; bootstrap re-verifies and repairs on every run |
| Who calls it | the P5 Supervisor graph (no `executor_node`) | the V1 graph and the Streamlit loop through `executor_node` |

### 1.3 Conflicts found (none is visible as a git conflict)

**C1. The retry policy contradicts P3's error rule (correctness and safety).**
- P5's `_agent_node` catches every exception from an agent and turns it into an `agent_error`.
- For the analyst, that consumes an analysis attempt and is retried up to three times.
- P3 requires infrastructure and policy errors to **stop the run and never be retried**.
- Today the placeholder raises `IsolationUnavailable` and the task is retried 3 times. That is harmless now (nothing runs), but it is wrong for a real backend.

**C2. The tripwire does not cover P4.1 or P5 code.**
- `binding.install()` replaces only `sandbox.executor.execute_code`.
- `sandbox_v2.executor.execute_code` (P4.1) launches its own `subprocess.run`, so it bypasses the tripwire.
- `multi_agent`'s `LazySeabornExecution` uses it.
- In a hardened process, P5's trust gate is today the *only* thing keeping scripted-fixture code off an unisolated subprocess.

**C3. The call-site census has a blind spot.**
- `test_executor_node_call_site_census` scans only `agent/`, `ui/` and `utils/`.
- Its premise ("only `executor_node` calls `execute_code`, so the one binding covers every path") is no longer true. Call sites now also exist in:
  - `multi_agent/execution.py`;
  - `sandbox_v2/executor.py`;
  - `evaluation/isolation.py`.

**C4. The bootstrap refuses the P5 flag.**
- `_prerequisite_problems` reports "not integrated yet" for any non-hardening flag.
- `IP_FF_ORCHESTRATOR=v2` + `IP_FF_SANDBOX_HARDENED=1` is therefore refused, even in test mode.

**C5. P5's guard test fails on the merged tree.**
- `tests/v2/p5/test_integration_p50.py::test_no_protected_v1_path_differs_from_the_p41_base` pins `config/` to `d321785c`.
- P3's approved D1 change to `config/feature_flags.py` violates that, as shown by `git diff d321785c b6d88a8a -- config` returning `config/feature_flags.py`.
- **The fix must not weaken the guard.** Pin the one allowed blob hash of `config/feature_flags.py`, not the whole folder.

**C6. The placeholder backend contract is unvalidated.**
- `HardenedContainerExecution.execute` returns whatever `backend.run()` returns.
- It does not check type, isolation level, run id or chart provenance.

**C7. Dataset handoff mismatch.**
- P5 passes the host path to the port.
- P3's design never lets the runtime see the host path. The runtime sees only `in/dataset.csv`, a copy verified by digest.
- The path must stop at the `DatasetAuthorizer`.

### 1.4 Combined-tree regression (P5 + P3, throw-away, not a merge)

Full offline suite on tree `b6d88a8a` in env B (Python 3.11.18): **1537 passed, 14 failed, 13 xfailed**, 3 warnings.

The 14 failures break down as follows:
- **13 P3 dataset, workspace and lifecycle tests: environment only.**
  - `git archive` extracted `data/` with mode 0775. `DatasetAuthorizer` correctly refuses a group-writable data directory; that is the control working.
  - With the mode set back to 0755 (the repository's real mode), **all 120 tests in those three files pass**.
- **1 P5 test: needs a git checkout.**
  - Evaluated directly with `git diff`, it is the **real conflict C5**.

**Expected post-merge baseline:** 1550 passed + 13 xfailed, plus 1 known guard failure (C5) until the reviewed guard fix lands. In detail:
- 551 from P5;
- 1000 P3-only tests;
- minus the C5 guard test.

It must be re-measured on the real integration branch.

---

## 2. Task 2: P3 readiness, implemented versus planned (verified in code, not from documents)

### 2.1 Component status

| Component | Status | Evidence |
|---|---|---|
| Flag rule D1 (hardened ⇒ container) | IMPLEMENTED | `config/feature_flags.py` diff; F-01…F-05 |
| Contracts: `Limits`, `ExecutionRequest`, `ExecutionResult`, error families, `IsolationLevel` | IMPLEMENTED | `limits.py`, `runtime.py`, `results.py`, `errors.py`, `isolation.py` |
| Launcher `v2/launch_app.py` + bootstrap + binding + tripwire + dispatcher | IMPLEMENTED; **production always refuses hardened mode** | `bootstrap.py:110–119` |
| Demo-only `DatasetAuthorizer` (four pinned SHA-256; descriptor-relative copy with a TOCTOU check) | IMPLEMENTED; **not wired into the dispatcher** | `datasets.py`, DS-01…15 |
| `JobWorkspace`, `InstanceLease` (flock liveness, report-only reconciliation) | IMPLEMENTED; not wired in | `workspace.py`, `lease.py` |
| IPX1 frame encoder and strict decoder; attestation shape and verify | IMPLEMENTED; **not wired in ("P3.6")** | `frame.py` |
| Host PNG validation and re-encode; atomic no-overwrite `ChartStore` | IMPLEMENTED; not wired in | `artifacts.py`, `chart_store.py` |
| Pure in-container supervisor helpers (collectors, attestation parsing, `/out` classification) | IMPLEMENTED as pure functions only; no operational supervisor | `supervisor_helpers.py` |
| Sandbox image: digest-pinned base, hash-locked dependencies, staged context, inert entrypoint | IMPLEMENTED (P3.5a) and **built in CI** (§2.2) | `image/`, CI run #4 |
| `ContainerRuntime` (detection, argv builder, run/kill/remove, preflight self-test, reconcile) | **NOT IMPLEMENTED (P3.6)** | no module exists |
| In-container supervisor and runner (PID 1, signal plan, frame emission, `kill(-1)`) | **NOT IMPLEMENTED (P3.6)** | none |
| Resource and storage limit enforcement and verification | **NOT IMPLEMENTED (P3.7)** | values are provisional (D9); only the probe argv sets memory, pids, cpus and a `/tmp` size |
| End-to-end integration, UI through the launcher, benchmark | **NOT IMPLEMENTED (P3.8)** | |
| Deployment unit (Q1/D3) | **UNRESOLVED** | design §20–21 |
| P2 guardrails (secure profile prerequisite) | **NOT IMPLEMENTED**; `P2_GUARDRAILS_IMPLEMENTED = False` | `bootstrap.py:36` |

### 2.2 Ubuntu CI evidence: image validation is not security validation

GitHub Actions `p3-ubuntu-rootless-probe`, **run #4 (id 45033387515) at `4cd107e0`: success**. Every stage passed:
- 3 host gates, including a real enforcement probe in a rootless container;
- offline build 1;
- image verification;
- a clean build 2 with **identical image ID `0ffd44b3…`** (reproducible);
- inert start-up: warm median 86.0 ms, cold 90.2 ms;
- **hash-pinned golden fixtures inside the image: 28/28 answers, 12/12 exact hermetic charts**;
- the OS package inventory (105 dpkg packages);
- the readiness tests: 210 passed, **8 skipped** (refusal paths that need different host facts or uid 0).

What this **does not** prove (the CI report says so itself):

- **The account is not a non-root boundary.** "Runner account is root-equivalent (passwordless sudo, docker group): this is not VE-2 non-root or cross-account validation."
- **No vulnerability scan was run.** "Vulnerability scan: NOT RUN (no approved scanner and database source)."
- **Only trusted code ran.** The fixtures that ran in the image were trusted, hash-pinned code through the development driver `scripts/p3/rootless.py`, not a production backend and not untrusted code. No bypass corpus ran with the AST scanner disabled. There were no network, filesystem, signal, `/proc` or fork-bomb probes.
- **The probe argv is only part of the planned run command (§8.1).** It sets `--network=none`, `--ipc=none`, a read-only root, cap-drop ALL, no-new-privileges, uid 10001, `--passwd=false`, memory, pids and cpus. It lacks:
  - `--dns=none` and `--no-hosts`;
  - `--cgroupns=private`;
  - `--log-driver=passthrough`;
  - `--timeout`;
  - `--ulimit nofile/core`;
  - tmpfs `nr_inodes`;
  - `--image-volume=ignore`;
  - run labels;
  - image pinning by ID.

  `/out` is 64 MiB against the planned 32 MiB.
- **The evidence is not preserved.** It exists only as a job log. The run uploaded **no artifacts**, and GitHub expires logs. Preserve the report in the repository (Phase 0).

### 2.3 Missing P3.6 work (minimum for P5 integration)

1. **`ContainerRuntime`** implementing `ExecutionRuntime`:
   - rootless Podman detection;
   - an allowlisted argv builder exactly per §8.1;
   - a minimal CLI environment;
   - run, kill and remove with labels;
   - a bounded read of `max_frame_bytes + 1` from container stdout;
   - `decode_frame` + `verify_attestation`;
   - CLI stderr capped and never shown to the user;
   - `preflight()` self-test (cgroup v2 controllers, OOM and PID probe, attestation).
2. **An in-image supervisor and runner** replacing the inert entrypoint:
   - runs as PID 1 with the signal plan, `PR_SET_DUMPABLE=0` and child reaping;
   - runs the trusted prelude + `/in/code.py` against `/in/dataset.csv`;
   - stage-A chart canonicalisation;
   - one frame after every child process is dead.
3. **Extend `ExecutionRequest`** with the authorised dataset (name, sha256, size, workspace) and a prelude selector (enum). Wire `DatasetAuthorizer`, `JobWorkspace`, `InstanceLease`, the frame decoder, `reencode_png` and `ChartStore` into the dispatcher; the chart path is set only by `ChartStore.publish`.
4. **A production entry** (`ensure_ready` constructing the real runtime) only after the validated-environment gate.
5. **Concurrency semaphore** (`max_concurrent = 2`) and the queue timeout.
6. **Container reconciliation** (still report-only) and crash cleanup (`--rm`, `--timeout`, C-06).
7. **P3.7: limits enforced and read back** (`memory.max`, `pids.max`, `cpu.max`, tmpfs bytes and inodes, nofile) and proven by the R-, S- and I-tests.

### 2.4 Remaining P3.5b independent-environment checks (VE-2, never executed)

- The full gate → build → reproducibility → start-up → golden-in-image sequence, as the **unprivileged `ipval`** account on a reviewer-controlled Ubuntu 24.04 VM with cgroup v2 delegation.
- The **full offline suite as `ipval`** with the env B pins.
- **Cross-account probes as `ipprobe`:** `ls /home/ipval`, the container store, `podman ps -a` and `podman images` must all fail or show nothing.
- **An OS vulnerability review of the image:** the scanner and database source need approval.
- An evidence bundle (`00-environment.txt` with hashes) and a secret check.
- Then the P3.6/P3.7 isolation tests must run there with **0 skips** (claim rule, §12).

### 2.5 Readiness for integration, per area

| Area | Ready? | Gap |
|---|---|---|
| Dataset authorization | **Component ready; integration not ready** | not in `ExecutionRequest`; demo-only (4 datasets), so P5 in hardened mode is limited to the demo datasets; uploads need P2 |
| Chart handling | **Host side ready; path not ready** | the dispatcher refuses every chart; no in-container stage A; no `/out` handling; the `ChartStore` filename is `<stem>-<run_id>.png`, not V1's `<task_id>.png` |
| Error mapping | **Partially ready** | outcome → V1-tuple mapping exists (`dispatch._to_v1_tuple`); P5 has no mapping for the infrastructure and policy families (C1) and no `MEMORY_LIMIT` handling in its feedback |
| Result-frame validation | **Ready (decoder)** | not wired in; not exercised against a real supervisor |
| Resource limits | **Not ready** | provisional values; nothing enforces them in a backend; no R-/S-tests |
| Fail closed with no runtime | **Ready** | P3 production refuses; P5's placeholder raises and starts no process (tested) |

### 2.6 Stale documentation (not evidence of anything)

1. **`docs/v2/P3_SECURE_EXECUTION_DESIGN.md`** (both P3 branches):
   - It still says "P3.5b is INCOMPLETE … no image has been built (Phases C–E pending on VE-2)".
   - CI run #4 did build and verify the image on VE-1. The statement stays correct for **VE-2**, but it is wrong as a statement about building.
   - The stage table row P3.5b has the same problem.
2. **`docs/v2/P3_VALIDATION_ENVIRONMENT.md`:** "VE-1 (GitHub Actions) needs its own approval and is not covered here". VE-1 was later approved and run; the document does not record that.
3. **`.github/workflows/p3-ubuntu-rootless-probe.yml`:** its history header records runs 1–3, not run #4 (the passing one).
4. **`v2/__init__.py`:** "P3.1 status: only pure execution contracts exist … There is no launcher, no executor". The launcher, bootstrap and dispatcher exist.
5. **`v2/execution/results.py`:** "the planned dispatcher (P3.6) will build it … Nothing builds one yet". The dispatcher exists (P3.2b) and consumes it; the planned P3.6 runtime will build it.
6. **`v2/execution/runtime.py`:** "dataset authorisation (P3.3) must exist before any runtime may receive data". P3.3 exists; the request has simply not been extended.
7. **`docs/v2/P5_MULTI_AGENT.md`:** it calls `HardenedContainerExecution` "the P3 integration point", which is accurate. It does not mention C1–C4.
8. **The P5 scorecard:** accurate; no change needed.

---

## 3. Task 3: integration design (smallest safe)

### 3.1 Principles

1. **One dispatcher, two callers.** Do not write a second hardened path for P5. The P5 port wraps the *same* `HardenedDispatcher` that the V1 graph uses, obtained from the process-wide bootstrap. Security fixes then land once.
2. **The only executor of untrusted code is `IsolationLevel.ROOTLESS_CONTAINER`.** It is checked:
   - at port construction;
   - at dispatcher construction (existing);
   - on every result (existing).
3. **No fallback anywhere.**
   - The legacy ports refuse to be *constructed* in a `HARDENED` process, even for fixtures.
   - The tripwire also covers `sandbox_v2`.
   - Infrastructure errors terminate the run.
4. **Contracts are additive and validated.** P5 envelope kinds, verifier and approvals are unchanged, apart from one new reply kind and two new terminal statuses.
5. **Deterministic tests.** A `FakeContainerRuntime` (`is_test_double = True`) is accepted only under pytest via `install_for_tests`. It emits real IPX1 frames through `frame.encode_frame`, so the decode path is exercised. It never executes code: it returns scripted frames.
6. **No new processes model.** No persistent workers, no fork server: one container per execution, as P3 designs it.

### 3.2 Components and data flow

```
Supervisor ─analysis_request→ DataAnalystAgent ─execute(code, host_csv_path, task_id)→ ContainerExecutionPort
   ContainerExecutionPort (multi_agent/execution.py, replaces the placeholder body):
     1. require bootstrap.mode() == HARDENED and the dispatcher's runtime.isolation == ROOTLESS_CONTAINER
     2. tuple = HardenedDispatcher(code, host_csv_path, task_id)
          ├─ V1 AST scan (unchanged)                         → blocked tuple, no container
          ├─ code size ≤ Limits.code_bytes
          ├─ [P3.6] lease → JobWorkspace → write_code → DatasetAuthorizer.authorize_into(host path) → AuthorizedDataset
          ├─ [P3.6] ExecutionRequest(run_id, task_id, code, limits, dataset=AuthorizedDataset, prelude=PreludeId)
          ├─ runtime.execute → frame → decode_frame → verify_attestation → ExecutionResult
          ├─ [P3.6] chart bytes → reencode_png → ChartStore.publish → chart_path (server-generated name)
          ├─ _check_result (run id, isolation, no scanner outcome, chart provenance)
          └─ workspace.cleanup() in finally (a failure raises SandboxUnavailableError)
     3. translate the V1 tuple + result metadata → P5 ExecutionOutcome (§3.3)
     4. three-way dataset check: AuthorizedDataset.sha256 == the profile's dataset_sha256 == the pin
```

The host path stops at `DatasetAuthorizer`. The runtime and the code see only `in/dataset.csv`, and the argv contains no path bytes (DS-13).

The P5 Verification Agent keeps recomputing from the host CSV, read independently. That copy and the one in the sandbox are guaranteed identical by the three-way SHA-256 check.

### 3.3 Result translation contract (P3 `ExecutionResult` → P5 `ExecutionOutcome`)

| P3 outcome | `ok` | `stdout` | `error` | `error_type` | `blocked_by_safety_scan` | Supervisor action |
|---|---|---|---|---|---|---|
| scanner violation (dispatcher, before the container) | False | "" | V1 violation text | `runtime_error` | **True** | failed analysis attempt; retry with feedback (unchanged) |
| `SUCCEEDED` | True | stdout without `CHART_SAVED:` lines, ≤ 1 MiB (P5 caps it again at 20,000 characters) | "" | "" | False | parse CLAIM lines → verification (unchanged) |
| `SUCCEEDED`, `stdout_truncated=True` | True | truncated stdout | "" | "" | False | the missing claims are reported as `missing` by the verifier → retry; add `stdout_truncated: true` to the execution dict |
| `CODE_ERROR` | False | "" | stderr head + tail (`display_text`, control characters removed) | `classify_error(stderr)` | False | retry with the last stderr line as feedback (unchanged) |
| `TIMED_OUT` | False | "" | "Execution timed out after 30s." | `runtime_error` | False | retry (V1 semantics) |
| `MEMORY_LIMIT` | False | "" | "Execution exceeded the memory limit (1024 MiB)." | `runtime_error` | False | retry with that feedback (new; V1 has no memory limit) |
| chart rejected (`chart_rejected_reason ∈` the 15 codes) | True | as above | "" | "" | False | claims verified as usual; `chart_path = ""`; the reason is recorded |

`ExecutionOutcome` contract changes:
- **`isolation`:** becomes the `IsolationLevel.value` string. The port refuses anything but `rootless_container`.
- **`backend`:** `"rootless-podman"`.
- **`seconds`:** `duration_ms / 1000`.
- **New optional fields:** `chart_rejected_reason` (fixed set), `stdout_truncated` and `stderr_truncated` (bool). `contracts.validate_analysis_result` must accept exactly these new keys and the fixed reason set.
- **`chart_path`:** accepted only if it equals `ChartStore` directory + `chart_filename(task_id, run_id)`.

### 3.4 Error translation contract (exceptions)

| Raised | Family | P5 handling (new) | Run status |
|---|---|---|---|
| `SandboxUnavailableError`, `SandboxProtocolError`, `V1ExecutorReached` (all `InsecureConfigurationError`) | infrastructure | the analyst node emits a new reply kind **`execution_infrastructure_failure`** (route `ANALYST → SUPERVISOR`; payload `{error_class ∈ fixed enum, message ≤ 500 characters, sanitized}`; the class is set by the trusted node wrapper, never by model output). The Supervisor **terminates immediately**: no retry, no further tasks, no insight call | **`isolation_unavailable`** (new terminal status) |
| `DatasetNotAuthorizedError` | policy | the same mechanism, `error_class = dataset_not_authorized` | **`execution_refused`** (new) |
| `ArtifactRejectedError` | policy (artifact) | never raised to P5: converted into `chart_rejected_reason` (§3.3) | unchanged |
| `UntrustedCodeRefused` | trust gate (P5) | raised at construction, before the graph starts (unchanged) | the run does not start |
| `IsolationUnavailable` (P5 placeholder) | infrastructure | **re-parented under `SandboxUnavailableError`**, so one family exists | `isolation_unavailable` |
| any other exception from the port | unknown | treated as infrastructure (fail closed): never retried | `isolation_unavailable` |

**Accepted claims on early termination.** Claims from tasks already *verified by independent recomputation* stay in `accepted_claims`, but the run status is non-success. **Reviewer decision R6:** keep them, or discard everything.

### 3.5 Fallback prevention

All of these must hold at the same time:

1. **`resolve_port`:** untrusted code, or `sandbox_hardened`, only ever gets `ContainerExecutionPort`. This already exists, and it is tested.
2. **Legacy-port constructors:** they raise `UntrustedCodeRefused` when `bootstrap.mode() is HARDENED`, regardless of `CodeTrust`. This is new; it closes C2 at the P5 layer.
3. **Tripwire, extended:** `binding.install()` also replaces `sandbox_v2.executor.execute_code` with the tripwire whenever the module is importable, and `verify()` / `repair()` cover it. This is new; it closes C2 at the P3 layer.
4. **The call-site census covers the whole repository** (excluding `tests/`), with an explicit allowlist. This is new; it closes C3. The allowlist:
   - `agent/nodes/executor.py`;
   - `multi_agent/execution.py`;
   - `sandbox_v2/executor.py`;
   - `evaluation/isolation.py`.
5. **Bootstrap flags:** `IP_FF_ORCHESTRATOR=v2` is allowed alongside hardening, and only alongside it. In V1-compatible mode the orchestrator flag stays refused by the launcher, as today. This is new; it closes C4.
6. **Runtime trust:** the port refuses runtimes with `is_test_double = True` outside pytest (it mirrors the bootstrap rule).

### 3.6 Preserving P4.1 and its evidence

- **The P4.1 evidence stays untouched:**
  - nothing in `sandbox_v2/` or `docs/v2/p4/` changes;
  - the variants `v1`, `v1-control` and `v2-lazy-seaborn` are unchanged;
  - `HARNESS_VERSION` stays `p4.1`;
  - the P4.1 A/B numbers remain claims about the **V1 subprocess** only.
- **In the container:**
  - The prelude belongs to the trusted, hash-pinned in-image runner, not to `code.py` (which stays verbatim model code).
  - `PreludeId ∈ {v1, lazy_seaborn}` is passed as an allowlisted argv enum.
  - The image's prelude text is generated at image-build time from `sandbox_v2.lazy_prelude.derive_lazy_wrapper_text`, with the container paths (`/in/dataset.csv`, `/out/chart.png`).
  - A parity test proves it byte-equal to the host derivation.
  - The image pins `seaborn==0.13.2`, the same as env B, so the colormap pre-registration data agree.
- **Charts may differ:** the image pins `pillow==13.0.0` against env B's `12.3.0`, so chart pixels can differ from the P4.1 references. Container chart checks use the P3 hermetic references, never the P4.1 pixel hashes.
- **Container benchmarks** get new variants (`container-v1`, `container-lazy-seaborn`) with their own workload fingerprint. They are compared only within one paired session, never against the P4.1 numbers.

### 3.7 Human approval and verification (unchanged)

- The plan-approval interrupt, the edit/reject semantics, the step budget, bounded retries, and the verifier's independence and tolerance all stay the same.
- The Verification Agent still has no LLM. It never receives code, stdout or the sandbox's copy.
- A new test asserts the interrupt still fires **before any `runtime.execute` call**: `FakeContainerRuntime.calls == 0` while the graph is paused.

---

## 4. Task 4: LangGraph compatibility (dependency NOT changed)

**What P5 uses:**
- `StateGraph`, `START` and `END`;
- `add_conditional_edges` and `Annotated` reducers;
- `compile(checkpointer=…)` and `recursion_limit`;
- `langgraph.types.interrupt` and `Command(resume=…)`;
- `langgraph.checkpoint.memory.InMemorySaver`;
- `get_graph().edges`.

P5's own interrupt test additionally reads `state["__interrupt__"]`.

**Method:** one throw-away venv per combination (Python 3.11.18), with the latest compatible `langchain-core` resolved. Each venv ran a probe with literal fixtures:
- 4 datasets (28 claims);
- fault, retry and recovery;
- interrupt, then approve, edit, reject and an invalid decision;
- the step budget;
- graph edges.

| langgraph | langgraph-checkpoint | langchain-core | Result |
|---|---|---|---|
| **0.2.0 (current floor in `requirements.txt`)** | 1.0.12 | 0.2.43 | **FAILS:** no `langgraph.types`; P5 cannot be imported |
| 0.2.20 | 1.0.12 | 0.3.86 | FAILS: no `langgraph.types` |
| 0.2.40 | 2.1.2 | 0.3.86 | FAILS: `Command` missing |
| 0.2.50, 0.2.53, 0.2.55, 0.2.56, 0.2.57, 0.2.58, 0.2.60, 0.2.70, 0.2.76, 0.3.0, 0.3.34 | 2.1.2 | 0.3.86 | all P5 behaviour passes; **the paused state has no `__interrupt__` key**, so P5's test `test_interactive_run_pauses_…` would fail. **0.2.55 prints the resume payloads (human decisions) to stdout** |
| 0.2.56 | 2.0.4 / 2.0.9 / 2.0.10 | 0.3.86 | FAILS: `InMemorySaver` is missing in these checkpoint versions, so the human gate cannot start |
| 0.3.34, 0.4.0 | 2.0.10 (allowed by their metadata) | 0.3.86 | FAILS to import (`EXCLUDED_METADATA_KEYS`): **the published lower bounds are not reliable** |
| 0.4.10 / 0.5.4 / 0.6.11 | 4.3.2 / 2.1.2 / 3.0.1 | 1.8.4 | all pass, including `__interrupt__` |
| 1.0.0, 1.0.10, 1.1.10, 1.2.0, 1.2.9, 1.2.24 | 2.1.2–4.3.2 | 1.8.4 | all pass, including `__interrupt__` |

**Recommendation (decision R7):**
- **Proposed floors:** replace `langgraph>=0.2.0` with `langgraph>=1.0,<2` and add `langgraph-checkpoint>=2.1.2,<5`.
- **Why `1.0`, not the lowest technically working version (0.2.50):**
  - It keeps one modern stack (`langchain-core` 1.x, as in env B).
  - It avoids a 0.2/0.3 world where the checkpoint floor and the `__interrupt__` behaviour differ.
  - Every 1.x release tested passed. The floor is unvalidated beyond the probe.
- **Before adoption:** run the full combined suite at `langgraph==1.0.0` with the oldest checkpoint that resolves, and at the env B pins (1.2.9 / 4.1.1). The probe covers only P5's LangGraph surface, not the V1, P0, P3 or P4 suites.
- **Branch scope:** this applies to the V2 branch only. The exam build (`main`, `exam-backup-2026-11-17`) keeps its own requirements.
- **Not chosen:** a `0.2.50` floor would also need `langgraph-checkpoint>=2.1.2` and a version-agnostic interrupt test using `get_state().tasks[*].interrupts`.

---

## 5. Task 5: acceptance-test matrix for P5.1 + P3.6/P3.7

**Environments:**
- **C:** offline, test doubles; the result is labelled *wiring-only*.
- **VE-1:** GitHub ubuntu-24.04, root-equivalent account; not a security claim.
- **VE-2:** reviewer-controlled unprivileged VM. A skip counts as a failure. `IP_TEST_REQUIRE_CONTAINER=1`.
- **★:** the AST scanner is bypassed, so the container alone must contain the code.

| # | Area | Tests (new unless marked) | Env |
|---|---|---|---|
| 1 | Container isolation and refusal | port refuses a runtime not reporting `ROOTLESS_CONTAINER`, a failing `preflight()` or a test double outside pytest; attestation mismatch → `isolation_unavailable` (reuses I-12 cases); I-01 env allowlist ★; I-03/04/05 host files, repository and `/proc/*/environ` unreadable ★; I-06 only `lo`, no host loopback, DNS or metadata IP ★; I-07/08 uid ≠ 0, CapEff 0, NoNewPrivs 1, Seccomp 2 ★; I-10/11/13/14 signals, ptrace, daemons, keyctl/unshare/mount ★ | C (refusals) + **VE-2** (★) |
| 2 | Untrusted-code routing | `UNTRUSTED` code → only `ContainerExecutionPort`; a provider-labelled LLM with any legacy port refused at construction and at run time (existing P5 tests kept); `sandbox_hardened` flags → container port for every `CodeTrust` | C |
| 3 | No legacy fallback | with `HARDENED` installed: constructing `LegacyV1SubprocessExecution`/`LazySeabornExecution` raises; calling `sandbox.executor.execute_code` **or `sandbox_v2.executor.execute_code`** hits the tripwire; `subprocess.run`/`Popen` patched to fail is never called by any P5 path; runtime raising → run status `isolation_unavailable` with **0 retries and 0 tripwire hits**; whole-repository call-site census with an allowlist; mutation checks: remove each guard → at least one test fails | C |
| 4 | Dataset integrity and authorization | only the 4 pinned demo datasets reach `in/dataset.csv`; a non-demo path, symlink, traversal or tampered copy → `execution_refused`, 0 runtime calls; three-way SHA-256 (pin = profile = copy) with a mismatch → refused; argv and frame contain no host-path bytes (DS-13); the verifier still recomputes from the host file (P5 tests kept) | C + VE-2 (ownership O-01…04) |
| 5 | Numerical verification | the 20 P5 fault cases through the container port with `FakeContainerRuntime` (scripted frames): same TP 12 / TN 131 / FP 0 / FN 0, 0 incorrect claims accepted, 28/28 on clean runs; then through the real container with the golden fixture code: 28/28 | C, then VE-2 |
| 6 | Chart and report preservation | a chart reaches the store only through `reencode_png` + `ChartStore` (name `<stem>-<run_id>.png`, never overwritten); a rejected chart → `chart_rejected_reason`, claims still verified; A-01…21 corpus through the port; container charts compared with the **hermetic** references (12/12); the P4.1 pixel evidence and P5 evidence files byte-unchanged (`git diff --exit-code docs/v2/p4 docs/v2/p5`) | C + VE-1/VE-2 |
| 7 | Human approval and bounded retries | interrupt fires before any `runtime.execute` (fake call count 0); approve, edit and reject unchanged; invalid decision → `approval_failed`; `CODE_ERROR`/`TIMED_OUT`/`MEMORY_LIMIT` each consume exactly one attempt (budget 1 + `max_retries`); infrastructure and policy errors consume none and terminate; step budget still terminates | C |
| 8 | Resource limits and cleanup | R-01…06 ★ (OOM → `MEMORY_LIMIT` message, PID limit, CPU loop → V1 timeout text, SIGTERM-ignoring child, nofile, fork bomb); S-01…05 ★ (tmpfs bytes and inodes ENOSPC, read-only paths, OOM with full tmpfs, < 1 MiB host growth); the workspace removed after success, refusal and exceptions; no container left (`podman ps -a --filter label=org.insightpilot.run=…` empty) after a crash and `--timeout`; `max_concurrent = 2` semaphore | **VE-2** (C for the workspace part) |
| 9 | Failure and adversarial scenarios | forged, oversized, truncated or duplicate-key frames → `SandboxProtocolError` → `isolation_unavailable`; a result for another run id; isolation downgraded in the result; a chart path outside the store; stdout flood (100 MB) bounded; CLAIM lines after the truncation point; the bypass corpus (5 P0 shapes) contained ★; the LLM writes `CHART_SAVED:/etc/passwd`, ignored; a prompt-injected "decision" in stdout has no effect on routing | C + VE-2 (★) |
| 10 | Regression baseline | the existing **551 + 13 xfail** (P5 branch) and the combined **1550 + 13 xfail** plus the reviewed C5 guard fix; the 13 strict xfails stay xfailed with **0 tripwire hits** (G-06); golden snapshots byte-identical; V1 paths unchanged against `1fd3af3c`, except the approved D1 file | C + VE-2 (as `ipval`) |

**Independent validation requirement.** A security claim needs the VE-2 evidence bundle:
- every ★ test executed and passed with 0 skips, as `ipval`;
- the cross-account probes passed as `ipprobe`;
- the vulnerability review of the image is done.

GitHub-hosted CI (VE-1) runs with a root-equivalent account. It can prove image build reproducibility and functional parity only, never isolation.

---

## 6. Task 6: architecture comparison plan (after secure execution exists; nothing run now)

### 6.1 Arms

| Arm | Pipeline | Executor |
|---|---|---|
| A | existing single-agent V1 graph (`agent/graph.py`), unchanged | the P3 container through `executor_node` |
| B | V1 graph + the P5 Verification Agent run on its printed claims after each task (no retries driven by the verifier) | the same |
| B′ (optional) | B + verifier-driven retry through the V1 retry loop | the same |
| C | P5 Supervisor multi-agent | the same, via `ContainerExecutionPort` |

### 6.2 Controls

- **Identical across arms:**
  - the 4 demo datasets;
  - one task set, defined as claim specs: the 28 golden claims plus a held-out set of 20 new claims written and recomputed *before* any run;
  - the same model ID and version, temperature 0 where supported, and the same system-prompt family;
  - the same container image ID, limits, retry budget, machine and session.
- **Prompts are equalised** so that A, B and C ask for the same claims: A's planner receives the claim list in its task description.
- **Interleaved, counterbalanced schedule** (`evaluation.harness.ab_schedule`).
- **Size:** at least 5 repetitions per dataset and arm; more if variance demands. Paired by (dataset, task, repetition).
- **Workload fingerprint:** it must match, including prompt hashes and image ID. Otherwise `paired.compare` refuses.
- **Model nondeterminism** is recorded, not hidden. Report run-to-run disagreement within an arm.

### 6.3 Metrics (per arm, with paired differences and CIs)

- **Accuracy:** final accepted claims correct against the independent reference (golden + held-out), per claim and per task.
- **Incorrect-result detection:**
  - share of wrong claims that are rejected;
  - **incorrect claims accepted** (the primary safety metric);
  - false rejections.

  This applies to arms B and C. Arm A has no detector, so its wrong-claim acceptance rate is the baseline.
- **Recovery rate:** share of tasks whose first attempt was wrong (execution error or refuted claim) but which ended verified within the budget.
- **Latency:** pipeline wall time; per node; per container execution (start-up separated from code time).
- **Provider-reported tokens** (`provider/provider_reported` class only), in and out, per agent and per skill.
- **Cost:** provider-reported tokens × configured list prices, labelled an estimate. **Reported only for the provider-reported class.**
- **Tool and LLM calls:** LLM calls per agent; executions; verification and insight-check calls; context characters per agent.
- **Safety and routing:** tripwire hits (must be 0); `isolation_unavailable` runs; scanner blocks; chart rejections.

### 6.4 Gates and budget

- **No live run without approval R10:** a maximum USD budget, model choice, data scope (demo only) and key handling via the environment's credential mechanism, never in code or logs.
- **Budget enforcement:** `IP_LIVE_LLM_BUDGET_USD` is enforced by the harness, which stops before exceeding it.
- **Pre-registration:** the analysis is pre-registered: metrics, primary endpoint "incorrect claims accepted" and the decision rule fixed before data collection.
- **What may be claimed:** only what the paired comparison reports. Offline scripted results are never quoted as model results.

---

## 7. Required files and changes (proposed; none made)

| Phase | Files | Change |
|---|---|---|
| 0 | new branch `feature/v2-p51-hardened-integration` from `1444491e`; `git merge --no-ff 4cd107e0` (P3 lineage incl. probe) | integration branch; no rebase, no history rewrite |
| 0 | `docs/v2/p3/ci_run4_evidence.md` (new) | preserve the CI run #4 report before log expiry |
| 0 | `tests/v2/p5/test_integration_p50.py` | C5: pin the approved `config/feature_flags.py` blob hash instead of the folder; keep the rest of the guard |
| 1 | `multi_agent/execution.py` | `ContainerExecutionPort` wrapping the bootstrap dispatcher; V1-tuple → `ExecutionOutcome` translation; legacy constructors refuse in `HARDENED`; `IsolationUnavailable` re-parented |
| 1 | `multi_agent/contracts.py`, `multi_agent/supervisor.py`, `multi_agent/agents.py` | reply kind `execution_infrastructure_failure`; statuses `isolation_unavailable` and `execution_refused`; optional execution fields; no retry for those families |
| 1 | `v2/execution/binding.py`, `v2/execution/bootstrap.py` | tripwire also on `sandbox_v2.executor.execute_code`; allow `orchestrator=v2` only with hardening |
| 1 | `tests/v2/p5/test_container_port_p51.py`, `tests/v2/p3/fakes.py` (extend), the census in `tests/v2/p3/test_bootstrap_p3.py` | `FakeContainerRuntime` emitting real frames; matrix rows 2, 3, 5 (C), 7, 9 (C), 10 |
| 2 (P3.6) | `v2/execution/container_runtime.py` (new), `v2/execution/runtime.py`, `v2/execution/dispatch.py`, `v2/execution/image/supervisor.py` (new), `Containerfile`, `image-expectations.json`, `scripts/p3/*` | real backend, in-image supervisor, request with dataset and prelude, chart wiring. **Sandbox-permission review gate** |
| 3 (P3.7) | the same, plus `v2/execution/limits.py` | enforced limits, read back; R-/S-tests |
| 4 | `requirements.txt` | LangGraph and checkpoint floors (R7), after the full-suite check |
| 4 | docs listed in §2.6 | refresh stale status text |

**Nothing changes in:** `agent/`, `sandbox/`, `utils/`, `ui/`, `rules/`, `skills/`, `data/`, `lessons/`, `sandbox_v2/`, `docs/v2/p4/`, `docs/v2/p5/` evidence, `main`, `exam-backup-2026-11-17`, `feature/insightpilot-v2`, or any P3/P4/P5 branch head.

---

## 8. Risks and rollback

### 8.1 Risks

| Risk | Level | Mitigation |
|---|---|---|
| Treating CI image evidence as isolation proof | High | claim rule: VE-2 evidence with 0 skips; every handoff says "TB3 isolation not verified; application not secure" |
| A P4.1 or P5 subprocess path is reachable in a hardened process (C2/C3) | High until Phase 1 | P5 constructor refusal + extended tripwire + whole-repository census + mutation checks |
| Infrastructure errors retried as code errors (C1) | Medium | the new terminal statuses; tests in rows 3 and 7 |
| Kernel or runtime escape (a shared kernel) | Residual High | out of P3 scope; optional gVisor later; patching policy |
| zlib/Pillow memory-safety bugs in chart handling | Residual Medium | P3 two-stage validation; keep the libraries patched; vulnerability review |
| Dependency floor breaks V1 or the exam build | Medium | V2 branch only; full combined suite at the floor before adoption; the exam branches untouched |
| Merge brings 1000 P3 tests plus a CI workflow into the P5 lineage | Low | clean merge-tree; combined suite measured; the workflow triggers only on pushes to the probe branch |
| GitHub log expiry loses the only VE-1 evidence | Medium | Phase 0 preservation |

### 8.2 Rollback

- **Phase 0:** delete the integration branch; nothing else changed.
- **Phases 1–4:** `git revert` the stage commit on the integration branch; no history rewrite.
- **Operational:** unset `IP_FF_SANDBOX_HARDENED` / `IP_FF_ORCHESTRATOR`. Production already refuses hardened mode without a validated runtime, and V1 (`streamlit run ui/app.py`) never imports `v2` or `multi_agent`.
- **VE artefacts:** `podman system reset --force` as `ipval`; delete the VM.

---

## 9. Proposed phases (each stops for review)

| Phase | Scope | Environment | Exit criteria |
|---|---|---|---|
| **P5.1-0** | integration branch (merge), CI evidence preserved, C5 guard fix | C | combined suite 1551 + 13 xfail; protected refs unchanged |
| **P5.1-1** | port, error translation, routing guards, `FakeContainerRuntime` (production still fails closed) | C | rows 2, 3, 5 (C), 7, 9 (C) and 10 green; mutation checks on every guard; no subprocess from any P5 path in hardened mode |
| **P3.6** | real `ContainerRuntime`, in-image supervisor, dataset and chart wiring | C unit + **VE-2** | rows 1, 4 and 6 on VE-2, 0 skips; sandbox-permission review |
| **P3.7** | limits enforced and read back | **VE-2** | row 8, 0 skips |
| **P5.1-2** | P5 golden and fault suite through the real container; container benchmark variants | VE-2 | 28/28; TP/TN/FP/FN equal to the doubles; latency recorded (no claims against P4.1) |
| **P5.2 / P6** | architecture comparison (§6), live model | VE-2 + approved budget | pre-registered analysis; R10 |

---

## 10. Change proposals (agent/change_approval.py protocol)

Each proposal is classified with `agent.change_approval.classify_change`. Where CLAUDE.md's human-review gates are stricter, the gate decides; this is marked ⇑.

| ID | Proposed change | Classifier (type / risk) | Effective risk | Approval |
|---|---|---|---|---|
| A | integration branch from `1444491e` + `git merge --no-ff 4cd107e0` | core_feature / Medium | **High** ⇑ (merging branches; P3 flag semantics enter the P5 lineage) | required |
| B | C5 guard-test fix (blob-pinned `config/feature_flags.py`) | docs_or_tests / Low (auto) | **Medium** ⇑ (changes a safety guard; CLAUDE.md "never weaken checks") | required |
| C | P5 `ContainerExecutionPort`, error translation, new reply kind and statuses (test doubles only) | core_feature / Medium | Medium | required |
| D | tripwire on `sandbox_v2` + bootstrap `orchestrator=v2` + whole-repository census | core_feature / Medium | **High** ⇑ (gate 2: execution-routing safety) | required |
| E | P3.6 container backend + in-image supervisor + request and chart wiring | core_feature / Medium (`v2/` is not in the classifier table); architecture / High if described as a new runtime directory | **High** ⇑ (gate 2: sandbox permissions and isolation) | required |
| F | LangGraph/checkpoint floors in `requirements.txt` | core_feature / Medium | Medium (gate 4: dependency change) | required |
| G | stale-documentation refresh (§2.6) | core_feature / Medium (`docs/` is not in the table, so it is classified as unknown) | Low | required by the classifier |

---

## 11. Decisions required from the reviewer

- **R1:** approve Phase P5.1-0: create `feature/v2-p51-hardened-integration` from `1444491e` and merge `4cd107e0` into it (proposal A).
- **R2:** approve the C5 guard fix as blob-pinning (B), or require a different form.
- **R3:** approve the "one dispatcher, two callers" design: P5 uses the bootstrap's `HardenedDispatcher`, with no separate P5 hardened path.
- **R4:** approve the new reply kind `execution_infrastructure_failure` and the terminal statuses `isolation_unavailable` and `execution_refused`, with **no retry** for infrastructure and policy errors (C).
- **R5:** approve the routing guards (D): legacy ports refused in `HARDENED`, a tripwire on `sandbox_v2`, `orchestrator=v2` allowed only with hardening, and the whole-repository census.
- **R6:** on early termination, keep the independently verified claims of completed tasks (recommended), or discard all.
- **R7:** the dependency floors `langgraph>=1.0,<2` and `langgraph-checkpoint>=2.1.2,<5`, after a full-suite check at the floor (F); or keep the floor unchanged for now.
- **R8:** where the container prelude lives: the trusted in-image runner with a `PreludeId` enum (recommended), against host-side wrapping into `code.py`.
- **R9:** VE-2. A reviewer-controlled Ubuntu 24.04 VM, operated by you, is required before any isolation claim. Confirm who runs it, and approve the vulnerability-scanner source.
- **R10:** no live-model evaluation until it is separately approved, with a budget cap, a model and data scope (§6.4).
- **R11:** approve a separate P3.6 stage (E) with the sandbox-permission review, after P5.1-1.

**Stopped here:** no implementation, merge, PR, dependency change or default-pipeline change has been made.
