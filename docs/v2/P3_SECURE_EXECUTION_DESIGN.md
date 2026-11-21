# InsightPilot V2 — P3 Secure Code Execution: Design (rev. 2.1)

**Document status:** consolidated, self-contained design. It merges the P3.0 proposal (rev. 1), the revised architecture (rev. 2) and the 11 corrections of rev. 2.1. It does not depend on any other document or conversation, apart from the P0 and P1 documents it cites (`docs/v2/BASELINE.md`, `docs/v2/FEATURE_FLAGS.md`, `docs/security/*`).

**Review status:** the reviewer has conditionally accepted the architecture. **P3.1, P3.2a and P3.2b (all accepted after review) and P3.3 are implemented.** Everything else is a plan and needs separate approval for each stage. **No container backend exists: hardened execution cannot start outside the offline test suite.**

**Status labels used throughout:**

| Label | Meaning |
|---|---|
| **IMPLEMENTED (P3.1)** | Exists on `feature/v2-p3-secure-execution` and is covered by passing tests |
| **PLANNED (P3.n)** | Designed; not implemented; needs stage approval |
| **REQUIRES RUNTIME VALIDATION** | An assumption about Podman, Docker, the kernel or cgroups that must be proven on a real rootless Linux runtime (§14) before it is relied on |
| **UNRESOLVED** | An open engineering question; this document states a position and the reviewer decides (§20) |

---

## 1. Scope and security claims

P3 addresses **one** trust boundary: **TB3, between the app and the generated code** (`docs/security/threat_model.md` §2). It does not make InsightPilot secure as a whole.

- **TB1, TB2, TB4 and TB5 are unchanged until P2 and P5:** uploads, prompts, HTML rendering, the lesson store and exports.
- **Approval is enforced only by the UI until P8** (R-08, R-09).
- **The secure deployment profile stays refused until P2 guardrails exist and are validated** (§7.5). No P3 document, test or handoff may describe the application as secure.

### Claims ledger

| Control | Role | It does **not** prove |
|---|---|---|
| Rootless container: user/mount/PID/IPC/UTS/cgroup namespaces, no network, read-only root, no capabilities, seccomp, cgroup v2 limits | **The security boundary for TB3** (PLANNED) | The kernel is shared, so a kernel exploit escapes. gVisor is a possible later layer. |
| V1 AST scanner (`agent/rules_engine.py:scan_code_safety`, unchanged) | Defence in depth; keeps V1 retry semantics | Bypassable (R-03). Never cited as containment. |
| Minimal environment, timeouts | Defence in depth, availability | Not confinement |
| Feature-flag rules (`config/feature_flags.py`) | Configuration correctness (IMPLEMENTED) | Isolation. The flags only decide which executor is allowed. |

**Testing rule:** isolation tests assert the **effect** (secret not read, file untouched, no connection received, limit enforced). They run with the AST scanner bypassed, so the container alone must contain the bypass corpus.

---

## 2. Verified facts the design is based on

### 2.1 Repository state

Verified for P3.0 and again at the start of P3.1 (2026-11-21).

| Ref | SHA |
|---|---|
| `main` (also the remote default `HEAD`) | `fd5783c6202e3ea506a69e607160617a23eed807`, unchanged |
| `exam-backup-2026-11-17` | `fd5783c6202e3ea506a69e607160617a23eed807`, unchanged |
| `feature/insightpilot-v2` (P3 base) | `1fd3af3c05ba9118b0f03c107f30c0f68659ad51`: merge of P1 (PR #2), which follows the merge of P0 (PR #1) |
| `feature/v2-p3-secure-execution` | Created from `1fd3af3c`; P3.1 commits only |

`git diff fd5783c6..1fd3af3c -- agent sandbox utils ui rules skills data requirements.txt README.md` is **empty**: the base contains no application code changes since the examination snapshot.

### 2.2 Code facts

1. **Both execution paths share one binding point.**
   - **Path 1:** the LangGraph `execute` node (`agent/graph.py:75`), reached through `analyst_graph.stream` (`ui/app.py:948`).
   - **Path 2:** the UI post-approval loop calls `executor_node` directly (`ui/app.py:1045-1074`).
   - Both call `execute_code`, which is resolved from the module globals of `agent.nodes.executor` at call time (imported at line 11, called at line 36).
   - Follow-up chat and replanning execute no code. The exported notebook (R-15) runs outside the app.
2. **The 13 V1 strict-xfail tests call `sandbox.executor.execute_code` directly.** A V2 binding of `agent.nodes.executor.execute_code` therefore leaves them characterising V1.
3. **`tests/v2/conftest.py:isolated_outputs` monkeypatches `sandbox.executor.CHARTS_DIR`.** Hardened code must read it at call time.
4. **The report embeds charts by basename from `reports/charts/`** (`agent/nodes/report_generator.py:78-80, 198-203`).
5. **Reserved prefixes are strict.** An unregistered `IP_FF_*`, `IP_SANDBOX_*` or `IP_HITL_*` variable makes `load_flags()` raise. P3 adds no new variable.
6. **`rules/*.md` files are injected into LLM prompts**, so P3 documents rule alignment (R-24) in `docs/`, never in `rules/`.
7. **V1 returns stale charts.** When the current run made none, V1 still returns an existing `{task_id}.png` (`sandbox/executor.py:134-136`). The hardened path will not do this.
8. **Container CLIs leak host settings by default.** Podman's `--http-proxy` defaults to true. Docker copies `~/.docker/config.json` `proxies` into containers. `containers.conf` can add environment variables and mounts. The effective configuration must therefore be **verified from inside** the container (§8.6 attestation).
9. **`ui/app.py` does not rely on Streamlit "magic".** *Corrected in P3.2a:* the earlier claim that magic leaves the file untouched was not exact. Streamlit 1.59's `magic.add_magic` **always inserts one import** (`import streamlit.runtime.scriptrunner.magic_funcs as __streamlitmagic__`) into the main script. In `ui/app.py` it wraps **no** expression: the 8 non-call expression statements are docstrings (lines 1, 66, 620, 637, 648, 656, 684, 725) and the 109 bare call statements are left alone. Magic applies only to the main script, so under the launcher `ui/app.py` runs without that one unused import, which renders identically. `test_streamlit_magic_does_not_change_the_script` pins exactly this: the transformed AST minus that import equals the original AST.
10. **Uploads.** `ui/app.py:801-807` writes uploads to `/tmp/{uploaded.name}`. Demo datasets are `ROOT/data/{sales_data,attendance_data,student_data,ecommerce_timeseries}.csv` (`ui/app.py:737-742, 809`). Their full SHA-256 hashes are pinned in `tests/v2/golden/expected/*.json`.
11. **Missing key.** `utils/llm.py:13` calls `load_dotenv()`. `utils/llm.py:96-100` raises `ValueError("GOOGLE_API_KEY not set…")` when the key is absent.

### 2.3 Container runtime in the design and P3.1 session (cloud container)

- The Docker 29.10.0 client, `dockerd`, `containerd` and `runc` 1.5.3 binaries are present, but **no daemon is running and there is no socket**. None was started.
- Podman, crun, gVisor, bubblewrap and nsjail are absent.
- The session runs as uid 0 inside a Firecracker microVM with a **cgroup v1/hybrid** hierarchy. The kernel supports seccomp.

**Conclusion:** this environment **cannot** validate rootless isolation or cgroup v2 limits. No container claim may rest on it.

### 2.4 Baseline (P3.1)

Re-established in an isolated scratch virtualenv **outside the repository**. Nothing was installed on the examination laptop, and no system or repository packages changed.

| Item | Value |
|---|---|
| Interpreter | CPython 3.11.18 (`/usr/bin/python3.11`), virtualenv created with `uv` |
| Packages | The 100 pins of environment B, derived from the `*.dist-info` names in the committed `venv/` (excluding pip, setuptools and wheel), installed exactly. One extra package was installed: **`watchdog 6.0.0`**, which Streamlit requires on Linux but not on macOS (so the committed macOS `venv/` lacks it). |
| Command | `env -u GOOGLE_API_KEY PYTHONDONTWRITEBYTECODE=1 <venv>/bin/python -m pytest tests -q -p no:cacheprovider -rxXfE` |
| Base `1fd3af3c` | **270 passed, 13 xfailed, 0 failed, 0 xpassed, 0 skipped**, 1 known `UserWarning` (the Streamlit 1.59.2 AppTest workaround, `docs/v2/BASELINE.md`) |
| macOS (environment C) | Still **not verified**. Use the checklist in `docs/v2/BASELINE.md` on the separate V2 checkout only. |

---

## 3. Trust model and boundaries

| Zone | Contents | Trust |
|---|---|---|
| Z0 Host TCB | Host kernel, rootless runtime, pinned image | Trusted; kernel escape is a residual risk |
| Z1 App process | Streamlit, LangGraph, V2 dispatcher and validators, `GOOGLE_API_KEY` | Trusted; holds secrets and must be unreachable from Z3 |
| Z2 Job inputs | Per-run host directory containing `in/code.py` and `in/dataset.csv` (host-written copies) | Written by Z1 only; read-only to Z3 |
| Z3a Supervisor | PID 1 in the container, code from the image | Trusted code in an untrusted environment |
| Z3b Child | `runner.py` plus the generated code | **Untrusted** |
| External | Dataset bytes and name, LLM output (code, task ids) | Untrusted |

**Statements:**

- Access to a rootful Docker socket is equivalent to host root. Rootful runtimes and `euid 0` are therefore rejected.
- **No file written inside the container is ever opened on the host filesystem.** Results leave only as a bounded byte frame (§8.5).
- The frame itself is **untrusted**, because a compromised container could forge it. The host revalidates everything.

```mermaid
flowchart TB
  subgraph EXT["Untrusted inputs"]
    CSV["Dataset bytes + file name"]
    LLM["LLM: code, task_id"]
  end
  subgraph Z1["Z1 App process (trusted, holds secrets)"]
    D["Dispatcher, authorizer, validators"]
  end
  subgraph Z0["Z0 Host kernel + rootless runtime"]
    IN["Z2 jobs/run/in (0755/0644 inside 0700 parents), host-written"]
    subgraph CT["Container: userns, netns none, ro rootfs, no caps, seccomp, cgroup v2"]
      SV["Z3a PID 1 supervisor (trusted)"]
      CH["Z3b child: runner + generated code (untrusted)"]
      TM["tmpfs /out 32 MiB, /tmp 64 MiB"]
    end
  end
  CSV --> D
  LLM --> D
  D -->|"TB3a authorised copy, verbatim code"| IN
  IN -->|"ro bind /in"| CH
  SV --> CH --> TM
  SV -->|"TB3d bounded frame after all children are dead"| D
  CH -. "denied: env, host FS, repo, network, host loopback, other jobs" .-x Z1
```

| Boundary | Enforcement |
|---|---|
| TB3a host → inputs | Demo-only dataset authorisation (§9); argv grammar allowlist; no untrusted bytes in argv, mount specs or wrapper source |
| TB3b container | Namespaces, `--network=none`, read-only root, `--cap-drop=ALL`, `no-new-privileges`, seccomp, cgroup v2, tmpfs-only writes |
| TB3c supervisor ↔ child | PID-namespace-init signal semantics; `PR_SET_DUMPABLE=0`; extraction only after `kill(-1, SIGKILL)` and full reaping (§8.4) |
| TB3d result → host | Strict frame decoder with per-section caps; attestation check; host revalidation and re-encoding of charts |
| TB-L launcher → V1 | Identity-verified bindings plus a V1-executor tripwire (§7) |

---

## 4. Architecture overview (PLANNED except where marked)

```mermaid
flowchart LR
  subgraph HOST["Host OS user (trusted)"]
    LA["v2/launch_app.py (P3.2)"]
    BS["bootstrap: flags, preflight, lease, bindings (P3.2)"]
    APP["ui/app.py (unchanged, via runpy)"]
    P1["Path 1: graph execute node"]
    P2["Path 2: UI approve loop"]
    EN["executor_node (unchanged)"]
    DS["dispatch.hardened_execute_code (P3.6)"]
    TW["tripwire on sandbox.executor.execute_code (P3.2)"]
    AU["DatasetAuthorizer, demo-only (P3.3)"]
    WS["JobWorkspace (P3.3)"]
    CR["ContainerRuntime (P3.6)"]
    AV["Artifact validator (P3.4)"]
    CHD[("CHARTS_DIR")]
  end
  subgraph CT["Rootless container"]
    SV["PID 1 supervisor (P3.4/P3.5)"]
    RN["child: runner + generated code"]
  end
  LA --> BS --> APP
  APP --> P1 --> EN
  APP --> P2 --> EN
  EN -->|"execute_code (rebound)"| DS --> AU --> WS --> CR -->|"fixed argv"| SV --> RN
  SV -->|"bounded frame"| CR --> AV --> CHD
  EN -. "stale reference" .-> TW
```

```mermaid
sequenceDiagram
  participant N as executor_node
  participant D as Dispatcher
  participant A as DatasetAuthorizer
  participant R as ContainerRuntime
  participant S as Supervisor (PID 1)
  participant C as Child
  N->>D: execute_code(code, csv_path, task_id)
  D->>D: V1 scan_code_safety (same verdict and message as V1)
  D->>A: authorize_and_copy(csv_path) to jobs/run/in/dataset.csv
  D->>R: run(fixed argv)
  R->>S: start (uid 10001, no caps, no net)
  S->>S: reset signals, set non-dumpable, attest
  S->>C: spawn runner (own stdout/stderr pipes)
  C-->>S: capped stdout / stderr head+tail
  S->>S: child exits or deadline: kill(-1, SIGKILL), reap, check /proc
  S->>S: stage-A chart checks and re-encode
  S-->>R: one frame on container stdout
  R-->>D: frame (host reads at most max_frame_bytes)
  D->>D: decode strictly, verify attestation, stage-B validate, host re-encode, store
  D-->>N: (success, output, chart_path, error_type), V1 tuple
```

---

## 5. Feature-flag semantics — IMPLEMENTED (P3.1)

### 5.1 Before and after

| `IP_FF_SANDBOX_HARDENED` | `IP_SANDBOX_BACKEND` | Profile | Before P3.1 | After P3.1 |
|---|---|---|---|---|
| 0 (default) | `subprocess` (default) | exam | V1 (`resolve` → `"legacy"`) | **Unchanged:** V1 (`"legacy"`) |
| 0 | `container` | any | `FeatureFlagError` | Unchanged |
| **1** | **`subprocess` or unset** | **exam** | **Accepted.** `resolve` returned `"subprocess"` if the availability callback said so, which implied a hardened subprocess mode existed. | **`InsecureConfigurationError`:** "there is no hardened subprocess mode" |
| 1 | `subprocess` | secure | `InsecureConfigurationError` (secure-profile block) | `InsecureConfigurationError` (new rule, checked earlier) |
| 1 | `container` | exam | Valid; `resolve` → `"container"` if available, else `InsecureConfigurationError` | Unchanged |
| 1 | `container` | secure (+ guardrails `enforce`) | Valid | Unchanged at the flag level. The planned launcher refuses the secure profile until P2 exists (§7.5). |

### 5.2 Change made

`config/feature_flags.py`:

- **`validate()`:** new rule placed after the container-requires-hardened rule and before the secure-profile block:
  ```python
  if flags.sandbox_hardened and flags.sandbox_backend != "container":
      raise InsecureConfigurationError(
          "IP_FF_SANDBOX_HARDENED=1 requires IP_SANDBOX_BACKEND=container: "
          "there is no hardened subprocess mode; the V1 subprocess runs only "
          "when hardening is off")
  ```
- **Docstrings and comments:** the module docstring, the `sandbox_backend` comment and the `resolve_sandbox_backend` docstring now say that hardened execution is container-only.

`docs/v2/FEATURE_FLAGS.md`:

- the "No unsafe fallback" rule now states the container requirement;
- a new "Launch command" rule says `streamlit run ui/app.py` is pure V1, and that the launcher is planned, not implemented;
- the two flag-table rows are updated;
- "In P0 nothing reads these flags" now reads "As of P3.1".

No new variable or value was added, and no other logic changed.

### 5.3 The 28 P0 feature-flag tests (`tests/v2/test_feature_flags.py`, unmodified)

| # | Test (cases) | Path through the new `validate()` | Result |
|---|---|---|---|
| 1 | `test_defaults_are_v1` (1) | hardened False; rule not reached | pass |
| 2 | `test_blank_values_mean_default` (1) | defaults | pass |
| 3 | `test_unrelated_environment_is_ignored` (1) | defaults | pass |
| 4 | `test_bool_parsing` (5) | `IP_FF_TRACING` only | pass |
| 5 | `test_invalid_values_raise_instead_of_defaulting` (6) | raises in `_parse` before `validate` | pass |
| 6 | `test_unknown_reserved_flag_is_rejected` (1) | raises before `validate` | pass |
| 7 | `test_container_requires_hardened` (1) | the existing first rule raises `FeatureFlagError` | pass |
| 8 | `test_dependent_flags_require_parent` (1) | HITL and parallel rules | pass |
| 9 | `test_secure_profile_accepts_isolated_configuration` (1) | hardened + container passes | pass |
| 10 | `test_secure_profile_rejects_weakened_configuration` (4) | case `[backend subprocess]` is now rejected by the new rule; still `InsecureConfigurationError`, and the test does not match messages. The other 3 cases are rejected by the secure block. | pass |
| 11 | `test_legacy_executor_only_when_hardening_not_requested` (1) | `"legacy"` | pass |
| 12 | `test_no_silent_fallback_when_hardened_backend_unavailable` (1) | Assertion 1 (`hardened=True, backend="subprocess"`) now raises in `validate()` inside `resolve` (still `InsecureConfigurationError`). Assertions 2 and 3 unchanged. | pass |
| 13 | `test_validate_rejects_directly_constructed_insecure_flags` (1) | secure block | pass |
| 14 | `test_every_flag_documented` (1) | no new variable | pass |
| 15 | `test_no_v1_module_imports_feature_flags` (1) | no V1 file changed; `v2/` is outside the scanned folders | pass |
| 16 | `test_importing_app_modules_does_not_load_flags` (1) | V1 modules never import `v2` or flags | pass |

**Mutation check (P3.1):** in a throw-away copy of the tree with the new rule removed, **8 of the new P3.1 flag tests fail and the 28 P0 tests still pass**. The P0 tests do not depend on this rule; the new tests guard it.

---

## 6. Execution contracts — IMPLEMENTED (P3.1)

Package `v2/` lives outside the folders the P0 tests scan. Types and interfaces only.

| Module | Contents |
|---|---|
| `v2/__init__.py` | Package docstring (V1 never imports V2) |
| `v2/execution/__init__.py` | Re-exports |
| `v2/execution/isolation.py` | `IsolationLevel` = {`V1_UNISOLATED`, `ROOTLESS_CONTAINER`}. **No process tier.** `.os_isolated` is true only for the container. `isolation_for_backend()` maps `resolve_sandbox_backend()` output (`"legacy"`, `"container"`) and rejects anything else, including `"subprocess"`. |
| `v2/execution/limits.py` | Frozen `Limits` with the **provisional** values of §8.3; validation of positive values (`cpus` must also be finite, so NaN and ±infinity are rejected (P3.2a)) and consistency (chart pixels ≤ side², chart ≤ `/out`, tmpfs sizes < memory); derived `stderr_bytes`, `max_frame_bytes`, `host_deadline_seconds`; `DEFAULT_LIMITS` |
| `v2/execution/errors.py` | **Policy:** `SandboxPolicyError(RuntimeError)` → `DatasetNotAuthorizedError`, `ArtifactRejectedError(reason)` with a fixed `ARTIFACT_REJECT_REASONS` set (never attacker text). **Infrastructure:** `SandboxUnavailableError(InsecureConfigurationError)` → `SandboxProtocolError` (correction B-3) |
| `v2/execution/results.py` | `ExecutionOutcome` = {`SUCCEEDED`, `CODE_ERROR`, `TIMED_OUT`, `MEMORY_LIMIT`, `BLOCKED_BY_SCANNER`}; frozen `ExecutionResult` with invariants: run id is 32 lowercase hex; a chart only on success; chart path and reject reason are mutually exclusive; known reasons only; scanner-blocked runs have zero duration; `is_valid_run_id()` |

**Purity, enforced by `tests/v2/p3/test_contract_purity.py`:**

- the contract modules import only `dataclasses`, `enum`, `typing`, `re`, `config.feature_flags` and `v2`;
- they make no `eval`, `exec`, `compile`, `open` or `__import__` calls, and no `system`, `popen`, `spawn`, `fork`, `exec*` or `run_path` attributes;
- importing `v2.execution` in a fresh interpreter loads no subprocess, socket, ssl, http, urllib, multiprocessing, ctypes, runpy, threading, Streamlit, LangChain/LangGraph, Google, dotenv, pandas or V1 application module;
- no V1 module imports `v2`;
- every `.py` file under `v2/` is one of the six reviewed contract modules or is listed in `REVIEWED_NON_CONTRACT_MODULES`. This scope guard must be updated deliberately in later stages. P3.2a added exactly one entry, `v2/launch_app.py`, and the fresh-interpreter check also asserts that importing the contracts never imports the launcher.

Run ids are only *validated* here. Generating them is planned for the dispatcher, using `secrets.token_hex(16)`. `uuid` was avoided because it imports `platform` on Python 3.11.

---

## 7. Launcher integration — P3.2a and P3.2b IMPLEMENTED (no backend yet; hardened mode fails closed)

### 7.1 Entrypoint — IMPLEMENTED (P3.2a)

`v2/launch_app.py` (run with `streamlit run v2/launch_app.py`) does three things on **every** script run, including reruns:

1. **`prepare_sys_path()`.** `streamlit run X` puts X's folder first on `sys.path`: `v2/` for the launcher, `ui/` for the original command. The launcher removes the `v2/` entry, adds `ui/`, and makes the repository root importable (for `config.feature_flags`). It is idempotent, so reruns do not grow `sys.path`. Import resolution of `agent`, `sandbox`, `utils`, `config`, `app`, `streamlit`, `pandas`, `v2` and `tests`, and the de-duplicated directory order, equal those of `streamlit run ui/app.py` (tested). Top-level names such as `execution` and `launch_app` do not become importable.
2. **`check_flags()`.** `load_flags()` with strict parsing. The app starts **only if `FeatureFlags.is_v1_default`**. Any other value raises `LaunchRefused`, and the launcher renders one `st.error` naming the variables, then calls `st.stop()`. This covers hardened execution, the secure profile, guardrails, tracing, verifier, HITL v2, orchestrator v2, parallel tasks, a live LLM budget, and any invalid or unknown flag. **`ui/app.py` is never executed for such a request, and the V1 executor is never a fallback.**
3. **`run_app()`.** `runpy.run_path(APP_PATH, run_name="__main__")`, where `APP_PATH` is the fixed path `<root>/ui/app.py`. No environment variable or argument can change it.

What it does **not** do in P3.2a: rebind or replace any function, add a tripwire, read or load secrets, start processes or containers, or import `v2.execution`.

**Execution semantics, compared with Streamlit's own main-script execution by probe scripts (tested):**

| Property | Streamlit runs `ui/app.py` directly | Launcher + `runpy` |
|---|---|---|
| `__name__` | `"__main__"` | `"__main__"` |
| `__file__` | the script path | absolute `ui/app.py` |
| `sys.modules["__main__"]` is the script's globals | yes | yes (runpy's temporary module; restored afterwards) |
| `sys.argv[0]` | the script path | `ui/app.py` while it runs (runpy) |
| Working directory | unchanged | unchanged (the launcher never changes directory) |
| `st.session_state` | shared per session | shared per session |
| `st.stop()` / `st.rerun()` | Streamlit control exceptions | propagate through `run_path` unchanged |
| Magic | applied (adds one unused import) | not applied to `ui/app.py` (§2.2 item 9) |
| `pages/` discovery | next to the main script | neither `ui/pages` nor `v2/pages` exists (tested) |

**Known differences, none of which affects behaviour:**

- tracebacks of *uncaught* exceptions contain two extra frames (launcher and runpy);
- Streamlit's file watcher watches `v2/` instead of `ui/` (development-time "rerun on save" only);
- widget IDs are internal and may differ, so they are excluded from comparison; keyed widgets and `session_state` keys are identical.

### 7.1a How the equivalence is verified — IMPLEMENTED (P3.2a)

`tests/v2/p3/test_launcher_p3.py` drives **both** `ui/app.py` and `v2/launch_app.py` with Streamlit `AppTest`, in independent runs. Each run gets its own output folders, lesson store and token counters; one scripted mock LLM is used, and its call log is cleared between runs.

**What is compared after every step:**

- the **entire element tree**: every element's full protobuf, in tree order;
- the exceptions shown;
- LLM calls per skill;
- the app's `session_state` keys, with `agent_state` reduced through `normalize_run` from the golden harness.

**Normalisations, all minimal and justified:**

- the per-run output folder is replaced by `<OUT>`;
- wall-clock timestamps are masked;
- widget `id`/`formId` are dropped;
- the media URLs of exactly the three report download buttons are masked, because those files embed generation timestamps and random notebook cell ids that differ between *any* two runs. The report files themselves are then compared: Markdown and notebook text after the same normalisation, and the PDF by header and page count, since its compressed streams contain the timestamp.

Chart images are compared **byte for byte**, and chart media URLs are not masked.

**Flows:**

- demo selection → planning → approval → execution with retries (sales_t1 recovers; sales_t4 is blocked by the V1 scanner four times) → reports → all 7 results pages → follow-up chat;
- plan edit → replanning;
- discard;
- safe mode off (the LangGraph graph path, Path 1).

**Golden parity through the launcher:** the approve flow reproduces the committed `sales_ui_approve_safe_mode_on` snapshot, and the safe-mode-off flow (retries set to 2) reproduces the committed `sales_graph_safe_mode_off` snapshot. Snapshots are compared with `IP_UPDATE_GOLDEN` forcibly unset and are never regenerated.

**Mutation checks** (throw-away copies of the tree, P3.2a):

- one extra element rendered by the launcher fails equivalence at the first step;
- ignoring non-default flags fails all 17 unsupported-feature refusal tests;
- keeping `v2/` on `sys.path` fails the import-resolution test.

### 7.1b What AppTest cannot verify, and the proposed real-browser test

AppTest runs scripts in-process. It does **not** exercise:

- the real `streamlit run` bootstrap (`_fix_sys_path`, CLI argument handling, config loading from the working directory);
- the Tornado/WebSocket server and real browser sessions, including several concurrent tabs;
- the file watcher;
- real page-config and HTML/CSS rendering in a browser;
- the Streamlit 1.59 AppTest stale-widget defect, which the tests work around exactly like the P0 UI tests, with a visible warning.

The `sys.path` behaviour of `streamlit run` is therefore verified by simulating `_fix_sys_path` in a test, not by a real server.

**Real-browser check (L-12a): run once in P3.2a as uncommitted evidence; committing it as a test needs approval:**

- Playwright with headless Chromium starts `streamlit run ui/app.py` and `streamlit run v2/launch_app.py --server.headless true --server.address 127.0.0.1` on two free ports, with `GOOGLE_API_KEY` unset and no `.env`.
- It compares the rendered page text and structure for: initial load, demo-dataset selection and preview, and "Run autonomous analysis", which ends in the identical missing-key error **before** any LLM call (`utils/llm.py:96-100`), so no Gemini call is possible.
- It also starts the launcher with `IP_FF_SANDBOX_HARDENED=1` and `IP_SANDBOX_BACKEND=container` and asserts the refusal page.
- The approval and execution flows in a real browser need the test-only mock-LLM launcher (decision D13).

**One-off result (2026-11-21, this cloud container, Streamlit 1.59.2, headless Chromium 1243 via Playwright, script kept outside the repository):**

| Step | Visible page text | Count of each `data-testid` element | Page title |
|---|---|---|---|
| initial load | identical (984 characters) | identical | `InsightPilot`, both |
| demo dataset selected | identical (1,115 characters) | identical | identical |
| "Run autonomous analysis" with no key | identical (2,942 characters, including the missing-key error and its traceback) | identical | identical |
| launcher with `IP_FF_SANDBOX_HARDENED=1`, `IP_SANDBOX_BACKEND=container` | refusal message naming both variables; **no app content rendered**; the refusal also printed to the server's stderr | — | — |

This is evidence from one run, not a committed test.

### 7.1c Bootstrap, binding and dispatcher — IMPLEMENTED (P3.2b)

**Modules:**

| Module | Kind | Role |
|---|---|---|
| `v2/execution/runtime.py` | pure contract | `ExecutionRequest` (run id, task id, code, limits; **no dataset path until P3.3**) and the `ExecutionRuntime` protocol (`name`, `isolation`, `is_test_double`, `preflight()`, `execute()`) |
| `v2/execution/bootstrap.py` | reviewed non-contract | The process-wide, thread-safe (`RLock`) decision point. Modes: `UNINITIALIZED` → `V1_COMPATIBLE` / `HARDENED` / `FAILED` |
| `v2/execution/binding.py` | reviewed non-contract | The two recorded, reversible rebindings (§7.2) and the V1 tripwire |
| `v2/execution/dispatch.py` | reviewed non-contract | `HardenedDispatcher`: V1-compatible tuple contract; V1 AST scanner first; injected runtime; fail-closed error handling |
| `v2/launch_app.py` | reviewed non-contract | `admit()`. With default flags and no initialised bootstrap it takes the exact P3.2a path (nothing V2 imported). Otherwise it calls `bootstrap.ensure_ready()`; once the bootstrap is initialised it stays authoritative for the process. |

**Production decision** (`bootstrap.ensure_ready()`; no runtime parameter; no flag, environment variable or setting can supply one):

| Flags | Result |
|---|---|
| All defaults | `V1_COMPATIBLE`: nothing bound; `ui/app.py` runs as in P3.2a |
| Invalid or unknown | `FAILED`: "Invalid V2 feature-flag configuration" |
| Hardened + container (exam) | **`FAILED` after every prerequisite is checked and reported**: Streamlit `server.fileWatcherType == "none"`, `server.runOnSave == False`, and a validated container backend. The last one never exists in this build, so the request always fails closed. |
| Secure profile | `FAILED`: needs the P2 guardrails (`P2_GUARDRAILS_IMPLEMENTED = False`); "a hardened backend alone does not make the application secure" |
| Any other V2 feature | `FAILED`: "not integrated yet: …" |

**Per run** (every Streamlit rerun, every session):

- `FAILED` is sticky ("restart the application").
- The flags are re-read and must equal the frozen ones, so **the mode never switches**: `V1_COMPATIBLE` never becomes hardened, and `HARDENED` never silently becomes V1. A `FAILED` state reached from `HARDENED` keeps the binding installed, so the V1 executor stays closed for anything still running.
- In `HARDENED`: the Streamlit settings are re-checked, and `binding.verify()` runs. Drift is repaired, logged and counted; if repair fails, the process fails.

**Test-only entry points** (`install_for_tests`, `_reset_for_tests`):

- They refuse to run unless `pytest` is loaded.
- They accept only runtimes declaring `is_test_double = True`.
- They run the same prerequisite checks as production, plus the runtime's `preflight()`.
- Static tests assert that they are called nowhere outside `tests/`, and that `binding.install` is reachable only from `install_for_tests`.
- The only test double is `tests/v2/p3/fakes.FakeRuntime`, which **never executes code**; production code contains no fake runtime.

**Dispatcher contract** (§7.3 table, now IMPLEMENTED for the outcomes a runtime can report):

- Scanner violation → the identical V1 message and tuple; the runtime is not called.
- Oversized code → failure tuple; the runtime is not called.
- `SUCCEEDED` → `(True, stdout without CHART_SAVED lines or the V1 default text, None, None)`.
- `CODE_ERROR` → `(False, stderr, None, V1 classify_error)`.
- `TIMED_OUT` → V1's exact timeout message.
- `MEMORY_LIMIT` → "Execution exceeded the memory limit (1024 MiB)."
- **Any runtime exception** → `SandboxUnavailableError`.
- Wrong run id, unisolated result, a claimed scanner block, or a chart (artifact extraction does not exist yet) → `SandboxProtocolError`.
- Nothing ever falls back to V1. The dispatcher source references neither `execute_code` nor `subprocess`.

**Not implemented in P3.2b (deliberately):**

- the concurrency semaphore (belongs to the container backend, P3.6);
- the instance lease and `atexit` cleanup (P3.3);
- the dataset authoriser (P3.3);
- artifacts (P3.4);
- the deployment-unit invariants beyond the file-watcher settings and the secure-profile gate. Q1 is still open, so `euid`, `.env`, key-inheritance and secrets-file checks are **not** implemented.

**Leak guard** (`tests/v2/p3/leak_guard.py`, autouse for every P3 test):

- `tests/v2/p3` is collected before the P0 tests (including the 13 strict xfails) in the same process.
- Before and after every P3 test, the guard first undoes the test's own `monkeypatch` changes. It then resets the bootstrap and, if a binding is still installed, the tripwire has unacknowledged hits, the V1 executor identity changed, or the Streamlit server options changed, it **repairs that and fails the test**.
- Subprocess tests prove three things:
  1. a deliberate leak is caught and repaired before a following V1 test, which then passes;
  2. **without** the guard the same leak breaks that V1 test;
  3. a runtime that "falls back" to the V1 executor fails.

**This is routing, not a security boundary.** Rebinding a Python name decides which function `executor_node` calls; it isolates nothing. Isolation will come only from the container backend (P3.5–P3.7) and must be proven in the validation environment.

### 7.2 Bindings (process-global by design) — IMPLEMENTED (P3.2b; installed only in test mode, since production cannot reach `HARDENED`)

| Binding | Purpose |
|---|---|
| `agent.nodes.executor.execute_code ← dispatch.hardened_execute_code` | One rebinding covers Path 1 and Path 2 (§2.2 item 1) |
| `sandbox.executor.execute_code ← tripwire` | Any reach to the V1 executor in hardened mode (module reload, future call site, stale reference) raises `InsecureConfigurationError` |
| (`streamlit.file_uploader` observer) | **Not planned.** Needs separate approval (§9.3) |

**Rules:**

- **Bootstrap state machine.** States are `UNINIT → OFF | READY | FAILED`, under a `threading.Lock`.
  - Flags are read **once per process**.
  - `FAILED` is sticky: every rerun shows the error, and a restart is needed.
  - Tests reset state only through a fixture calling `bootstrap._reset_for_tests()` (correction B-8). A test asserts nothing outside `tests/` calls it.
- **Reruns.** Bindings persist in `sys.modules`. `binding.verify()` checks identity before each `run_path`. On a mismatch it rebinds, logs a WARNING and increments a counter.
- **Stale imports and reloads.** Hardened mode requires `server.fileWatcherType=none` and `server.runOnSave=false`; bootstrap refuses otherwise.
  - A reloaded `agent.nodes.executor` re-imports `execute_code` from `sandbox.executor`, which is the tripwire, so it fails closed.
  - A reloaded `sandbox.executor` is caught by `verify()` on the next rerun, before app code runs.
  - An AST census test asserts that only `executor_node` calls `execute_code`.
- **Sessions.** One server process means one binding, so all sessions are hardened (all or nothing per process).
- **Concurrency.**
  - The dispatcher is re-entrant; each call has its own run id, workspace and container.
  - A process-wide `BoundedSemaphore(2)` limits live containers, with a 120 s wait, after which it raises `SandboxUnavailableError("busy")`.
  - The dispatcher never relies on Streamlit's `ScriptRunContext`, because LangGraph may run nodes on worker threads.
- **Cleanup.** `atexit` releases the instance lease. In-flight containers end at their own deadline (§11). Bindings are never uninstalled in production; `binding.uninstall()` exists for tests only.

### 7.3 Failure mapping (V1 tuple preserved)

| Condition | Returned or raised |
|---|---|
| V1 scanner violation | `(False, <identical V1 message>, None, RUNTIME)`, so retries are identical |
| Child non-zero exit | `(False, stderr head+tail, None, classify_error(stderr))`, using V1's classifier |
| Code timeout | `(False, "Execution timed out after 30s.", None, RUNTIME)`, identical to V1 |
| OOM | `(False, "Execution exceeded the memory limit (1024 MiB).", None, RUNTIME)` |
| Success | `(True, stdout or "Execution completed successfully.", chart_path or None, None)` |
| Chart rejected | Success with `chart_path=None` and a WARNING log; output text not altered (D18) |
| Dataset not authorised | **raise** `DatasetNotAuthorizedError`: run stops, no LLM retries |
| Runtime unavailable, attestation mismatch, protocol error, busy | **raise** `SandboxUnavailableError` or `SandboxProtocolError`: no fallback |

### 7.4 Direct `ui/app.py` launch

**`streamlit run ui/app.py` bypasses all V2 hardening, now and after P3.** It never imports the launcher or the bootstrap, ignores every `IP_*` variable, and runs generated code in the unisolated V1 subprocess. It is an **unprotected legacy application** and must never be presented as secure, whatever flags are set. V1 cannot detect this without code changes, and V1 code will not be changed.

**Deployment boundary as of P3.2b:**

- The only V2 entry point is `streamlit run v2/launch_app.py`.
- The hardened launch command is `streamlit run v2/launch_app.py --server.fileWatcherType none --server.runOnSave false --server.address 127.0.0.1`, with `IP_FF_SANDBOX_HARDENED=1` and `IP_SANDBOX_BACKEND=container`. **In this build it always refuses before `ui/app.py` runs**, because no validated container backend exists.
- No deployment is secure before P2, P5 and P8. See §20 Q1 for the proposed deployment unit (still undecided).

### 7.5 Secure-profile gate

**IMPLEMENTED (P3.2b).** The bootstrap holds `P2_GUARDRAILS_IMPLEMENTED = False` and refuses `IP_DEPLOYMENT_PROFILE=secure`, in production and in test mode alike, even with an injected runtime, until P2 lands and is validated. The refusal is a `BootstrapRefused`, shown by the launcher before `ui/app.py` runs. P3 testing uses the **P3 isolation configuration**: `exam` profile + `IP_FF_SANDBOX_HARDENED=1` + `IP_SANDBOX_BACKEND=container`.

---

## 8. Container specification — PLANNED (P3.5–P3.7), REQUIRES RUNTIME VALIDATION

### 8.1 Run command (Podman reference; the Docker-rootless adapter is equivalent)

```
podman run --rm --name ip-<inst8>-<run12>
  --label org.insightpilot.instance=<instance_id> --label org.insightpilot.run=<run_id>
  --label org.insightpilot.schema=1
  --pull=never --image-volume=ignore --read-only --read-only-tmpfs=false
  --network=none --dns=none --no-hosts --http-proxy=false
  --ipc=none --cgroupns=private --log-driver=passthrough --timeout=<wall+grace>
  --user 10001:10001 --cap-drop=ALL --security-opt=no-new-privileges
  --memory=1g --memory-swap=1g --cpus=1 --pids-limit=64
  --ulimit nofile=256:256 --ulimit core=0
  --tmpfs /tmp:rw,size=64m,nr_inodes=4096,mode=1777,noexec,nosuid,nodev
  --tmpfs /out:rw,size=32m,nr_inodes=64,mode=1777,noexec,nosuid,nodev
  --mount type=bind,source=<instance>/jobs/<run>/in,target=/in,readonly[,relabel=private on SELinux]
  --env MPLBACKEND=Agg --env HOME=/tmp --env MPLCONFIGDIR=/opt/ip/mpl
  --env OPENBLAS_NUM_THREADS=1 --env OMP_NUM_THREADS=1 --env LANG=C.UTF-8 --env TZ=UTC
  localhost/insightpilot-sandbox@<image-id>
  python -I -B /opt/ip/supervisor.py --wall 30 --stdout-cap 1048576 ...   # integers only
```

- **Argv safety.** The argv is a list (never a shell). Every element must match an allowlist grammar. The instance base path must match `^[A-Za-z0-9/._-]+$`, otherwise fail closed (prevents `--mount` option injection).
- **CLI environment.** The CLI subprocess gets `PATH`, `HOME`, `XDG_RUNTIME_DIR` and `LANG` only.
- **Rejected at preflight:** rootful runtimes, `euid 0`, cgroup v1, and missing `cpu`/`memory`/`pids` controllers.
- **Docker differences:** `--log-driver=none` while attached; no `--timeout` (the supervisor deadline plus a host kill instead); `--read-only` adds no implicit tmpfs.
- **Must be validated on the runtime** (correction B-9 extended this list):
  - `--log-driver=passthrough` with pipe stdio;
  - `nr_inodes` passed through for Podman and Docker `--tmpfs`;
  - `--ipc=none` leaves no `/dev/shm`;
  - `--dns=none` together with `--network=none`;
  - Docker attach behaviour with `--log-driver=none`.

### 8.2 Mount ownership

Under rootless Podman's default mapping, host user U becomes container uid 0, and container uid 10001 becomes subuid S+10000.

| Option | Mechanism | Verdict |
|---|---|---|
| **R1 (chosen)** | The host writes `in/` (0755), `code.py` and `dataset.csv` (0644) inside `jobs/<run>/` (0700) inside `instances/<id>/` (0700). The OCI runtime resolves the bind source as mapped root (U) and can traverse the 0700 parents. The child reaches `/in` only through the mount point and reads as "other". The `readonly` mount gives EROFS; root-owned 0644 files would also refuse writes from 10001. Outputs never touch the host (tmpfs). | No widening of host access: other host accounts are stopped by the 0700 parents |
| R2: chown to the subuid | Host user loses direct access; cleanup needs `podman unshare` | Rejected |
| R3: idmapped mount | Needs kernel ≥ 5.12 and filesystem support; rootless support varies | Deferred |
| R4: `--userns=keep-id` | The child runs as the host uid at kernel level | Rejected (weaker) |
| R5: host-visible writable `/out` | Disk exhaustion, link attacks | Rejected |

**Tests:** O-01 (host modes and owners, ancestors unchanged), O-02 (in-container access matrix), O-03 (a second host account cannot read job files or `/proc/<pid>/root`), O-04 (no non-euid-owned file remains on the host). See §16.

### 8.3 Storage and resource limits

All values are provisional and are the same as `v2.execution.limits.DEFAULT_LIMITS`.

| Location or resource | Enforcement during execution | Host storage touched? |
|---|---|---|
| Root filesystem | `--read-only`, `--read-only-tmpfs=false` (no implicit `/run`, `/var/tmp` or `/tmp`) | no |
| `/out` | tmpfs 32 MiB, 64 inodes | no (RAM) |
| `/tmp` | tmpfs 64 MiB, 4096 inodes | no (RAM) |
| `/dev/shm`, mqueue | `--ipc=none` | no |
| `/dev` | runtime tmpfs, not writable by 10001 (attested) | no |
| Logs | `passthrough` / `none` | no |
| Memory | `memory.max` 1 GiB, swap 0. tmpfs pages are charged to this cgroup, so RSS + tmpfs ≤ 1 GiB (**proven by S-04**) | — |
| CPU, PIDs, files | 1 CPU; 64 PIDs (BLAS threads pinned to 1); `nofile` 256; `core` 0 | — |
| Host-written inputs | Trusted host only: code ≤ 1 MiB, dataset ≤ 256 MiB, at most 2 concurrent jobs | bounded |
| Time | Code 30 s (= V1 `MAX_EXECUTION_TIME`) + 15 s host grace | — |

### 8.4 PID 1 and the extraction protocol

See also §20 Q4.

1. **Supervisor start (before the child):**
   - resets **every catchable signal to `SIG_DFL`**, except `SIGCHLD` (handled, to reap) and `SIGPIPE` (ignored). This is correction B-2: CPython otherwise installs a SIGINT handler, which would let the child deliver SIGINT to PID 1;
   - sets `PR_SET_DUMPABLE=0`;
   - records the attestation (§8.6).
2. **Child:** `python -I -B /opt/ip/runner.py` with stdin `/dev/null`, its own stdout and stderr pipes, `close_fds`, and a new session. The runner provides V1's prelude names (`pd`, `np`, `matplotlib`, `plt`, `mticker`, `sns`, `warnings`, `df` from `/in/dataset.csv`, `CHART_PATH="/out/chart.png"`, the same style and rcParams), then runs `exec(compile(src, "<generated>", "exec"))`, then autosaves open figures.
3. **While the child runs**, the supervisor drains both pipes: stdout head up to 1 MiB, stderr head 128 KiB plus tail 128 KiB. The rest is discarded, and truncation flags are kept.
4. **When the child exits or the deadline passes:** `kill(-1, SIGKILL)`, then reap until `ECHILD`, then confirm that `/proc` lists only PID 1. Daemons the child forked or spawned with `setsid` are gone.
5. Stage-A chart handling (§10), and `oom_kill` read from `/sys/fs/cgroup/memory.events`.
6. **One frame** is written to the container's stdout, then exit 0.

**Signal facts the design relies on (REQUIRES RUNTIME VALIDATION):**

- Signals sent to a PID-namespace init from inside the namespace are delivered only if init has a handler; SIGKILL and SIGSTOP from inside are never delivered.
- From an ancestor namespace, only SIGKILL and SIGSTOP are forced. **The host must therefore stop containers with SIGKILL** (`podman kill -s KILL` / `rm -f -t 0`); SIGTERM is ignored by design.

### 8.5 Result frame and byte budgets

See also §20 Q3.

- **Two streams, never mixed:**
  - The child's stdout and stderr go **only** to the supervisor's pipes. They are user-visible text with their own caps (`stdout_bytes`, `stderr_head_bytes + stderr_tail_bytes`).
  - The container's stdout carries **only** the frame, written once by PID 1 after all child processes are dead. The runtime CLI's own stderr is diagnostics, capped at 64 KiB, logged but never shown to users.
- **Frame layout:** `magic "IPX1"` | `u32 header_len` (≤ `frame_header_bytes`) | UTF-8 JSON header | sections in fixed order: stdout, stderr, chart.
- **Header fields:** schema version, attestation, child exit code and signal, timed-out flag, OOM count, each section's declared length and SHA-256, truncation flags, chart reject reason, count of other `/out` entries.
- **Bounds:**
  - each declared length must be ≤ its own cap, even when the total would fit;
  - `max_frame_bytes = frame_header_bytes + stdout_bytes + stderr_bytes + chart_bytes`, derived in `Limits` (IMPLEMENTED);
  - the host reads at most `max_frame_bytes + 1` bytes; one byte more kills the container and raises `SandboxProtocolError`;
  - SHA-256 is checked for each section; no trailing bytes are allowed.
- **Memory:** the supervisor never holds more than the caps, because it caps while collecting, not when framing.

### 8.6 Per-run attestation

Recorded by the supervisor **before** the child starts:

- uid/gid;
- `CapEff`, `CapPrm` and `CapBnd` all 0, `NoNewPrivs` = 1, `Seccomp` = 2;
- network interfaces = {`lo`};
- writable mounts = {`/out`, `/tmp`};
- `/dev` not writable;
- enforced limits read from cgroupfs (`memory.max`, `memory.swap.max`, `pids.max`, `cpu.max`).

The host compares this with the expected values. Any mismatch raises `SandboxUnavailableError`. This catches drift in `containers.conf` or the runtime on **every** run.

### 8.7 Runtime image (PLANNED P3.5)

- **Base:** `python:3.11-slim-bookworm@sha256:<digest>`.
- **Packages:** only the rules allowlist (pandas, numpy, matplotlib, seaborn, scipy, scikit-learn) at environment-B versions, installed from `requirements.lock` with `pip --require-hashes`.
- **Final stage:**
  - no pip, setuptools, compilers, setuid bits or `VOLUME` directives;
  - user 10001;
  - a pre-built read-only matplotlib font cache;
  - supervisor, runner and canonicaliser at `/opt/ip/`.
- **Build context:** staged by `scripts/p3_build_image.sh` from an explicit file list (`v2/execution/sandbox_side/`, `v2/execution/image/`). It is never the repository root, so `.env` can never reach the image.
- **Provenance:** the image carries a label with the lock hash; the backend refuses a mismatch; the image id is recorded in every run.
- **Claims:** inputs are pinned and a rebuild yields an identical package set. A bit-identical image digest is **not** claimed.

---

## 9. Dataset authorization — IMPLEMENTED (P3.3), demo-only; not yet wired into the dispatcher

**P3.3 implementation** (`v2/execution/datasets.py`, `DatasetAuthorizer`):

- It follows §9.2 below exactly, with these decisions made during implementation:
  - The path is also refused if it is not already normalised (`os.path.normpath(p) != p`), so `.`, `..`, `//` and a trailing `/` are all rejected.
  - Control characters include C1 and the Unicode line and paragraph separators.
  - The approved **directory itself** must be owned by this user and not group- or world-writable.
  - The metadata recheck after the copy compares device, inode, size, **mtime and ctime**. A rename or in-place rewrite during the copy, even one that restores mtime, is refused.
  - The copy is `fsync`ed.
  - I/O failures (for example a full disk) raise `SandboxUnavailableError`.
  - Policy failures raise `DatasetNotAuthorizedError`.
  - In both cases the partial copy is removed by its fixed name, relative to the workspace descriptor.
  - A pre-existing (planted) `dataset.csv` in the workspace is never followed or deleted by the authoriser; the workspace cleanup removes the entry only.
- **The bytes are never read through the requested path:** they come from the trusted approved directory plus the allowlisted name. The requested path only has to *name* that directory.
- The result (`AuthorizedDataset`) carries the approved name, the verified digest, the size and the fixed workspace name. It deliberately has **no path field**.
- **Not wired in:** the production dispatcher does not call it yet, and `ExecutionRequest` still carries no dataset.
- **Platform:** verified on Linux (descriptor-relative `open`, `mkdir` and `unlink`; `O_NOFOLLOW`, `O_DIRECTORY`, `O_CLOEXEC`). macOS offers the same calls, but **no equivalent guarantee is claimed until it is tested there**.
- **Ownership checks:** the demo files and the data directory must belong to the user running the app. A checkout owned by another account is refused, which is fail closed.
- **Non-root validation — partly done in the cloud test container (P3.3 correction); still MANDATORY on VE-2.**
  - **What root-only tests can show.** The main suite runs as **uid 0**, and root bypasses discretionary access control. Those runs prove the descriptor-relative logic and the symlink, file-type, link-count and ownership comparisons. They do **not** establish permission-denied behaviour.
  - **What was run unprivileged** (2026-11-21, cloud test container; no account created; a throw-away pytest-only virtualenv, removed afterwards):
    - The three P3.3 test files ran as the pre-existing system account `nobody` (uid 65534) via `setpriv --reuid=65534 --regid=65534 --clear-groups`, with `--noconftest`.
    - Result: **104 passed, 3 skipped (root-only fixtures), 13 failed**.
    - **All 13 failures are the same deliberate fail-closed refusal**: "the approved data directory is not owned by this user". The repository's `data/` belongs to root, and the authoriser requires the running user to own the demo data. Two of the 13 surface as a different assertion, because that refusal pre-empts the oversize check and the concurrency check.
  - **Cross-account probe** with two unprivileged identities: the owner as uid 65534, and an intruder as uid 65533, an unnamed uid reached via `setpriv` with no account created.
    - **Under a hostile `umask 000`, the owner** authorised a demo dataset from its own copy, and the modes were exact: root, instance and run `0700`; `in/` `0755`; `code.py` and `dataset.csv` `0644`.
    - **The intruder** got `EACCES` listing the owner's root, entering the run directory and reading `code.py`. `inspect_instances()` and `InstanceLease.acquire()` on the owner's root were refused.
    - **A root pre-created ("squatted") by the intruder was refused by the owner.**
  - **Still required on VE-2:**
    - the real `ipval`/`ipprobe` accounts on a reproducible Ubuntu VM, running the **full** suite as `ipval`;
    - unprivileged equivalents for the root-only fixtures (device node, foreign owner);
    - the default root under the real shared `/tmp`.

    The cloud-container results are early evidence, not the evidence of record.

### 9.1 Principle

The only bytes that may enter a container are **byte-identical pinned demo datasets**. Uploads are refused in hardened mode until an upload registry is separately approved (§9.3). V1 upload behaviour is unchanged.

### 9.2 Demo policy

1. `realpath(dirname(csv_path))` must equal `realpath(ROOT/data)`, which is computed once from trusted code (correction B-5). The basename must be exactly one of the four demo names.
2. `dir_fd = open(realpath(ROOT/data), O_DIRECTORY|O_NOFOLLOW)`; `fd = openat(dir_fd, name, O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC)`. A symlinked final component gives ELOOP; a FIFO does not block.
3. `fstat(fd)`: `S_ISREG`, `st_nlink == 1`, `st_uid == geteuid()`, size ≤ `dataset_bytes`.
4. Stream into `in/dataset.csv` (opened with `O_CREAT|O_EXCL|O_NOFOLLOW` relative to the job directory fd), stopping at cap + 1 and computing SHA-256 while copying.
5. `fstat` again (dev, ino, size, `mtime_ns` unchanged), and the digest must equal the pinned SHA-256. The pins in `v2` are cross-checked against `tests/v2/golden/expected/*.json` by a test, so they cannot drift.
6. On any failure: delete the partial copy and raise `DatasetNotAuthorizedError`. The container mounts **only the copy**.

What was hashed is exactly what was copied, through one stable handle, so there is no TOCTOU window. Rejected: traversal, relative paths, NUL and control characters, subdirectories, symlinks, FIFOs, sockets, devices, hardlinks, foreign owners, oversize files, changed content, and the V1 S3 crafted path.

### 9.3 Uploads (deferred; needs separate approval)

The proposed mechanism is an observer wrapper on `streamlit.file_uploader` that records `(name, sha256(getbuffer()), file_id)`, plus the same fd-based checks with the root at `realpath("/tmp")` and a hash match against the registry. It is **not** to be implemented without separate approval, because it monkeypatches a public Streamlit API process-wide. If it is approved later, it must:

- return the identical object;
- never raise its own errors;
- handle `None`, a single file and lists (correction B-7);
- be LRU-bounded.

### 9.4 Remaining for P2

- V1's own upload write: it follows symlinks and accepts traversal names (R-07).
- Per-session upload directories with server-generated names.
- A session-scoped registry.
- Upload size limits at the Streamlit layer.
- CSV content and prompt-injection guardrails.
- Display-name escaping.
- Report and notebook path confinement.
- The shared `/tmp` namespace (R-18).

---

## 10. Artifact validation — PLANNED (P3.4 host side, P3.5 in-container)

**Stage A, in the container, after every untrusted process is dead:**

1. `lstat('/out/chart.png')`: must be a regular file (not a symlink, directory, FIFO, socket or device), with `st_nlink == 1`, `0 < size ≤ chart_bytes`.
2. Open with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`; the `fstat` must match the `lstat`; read ≤ cap.
3. Pillow with `MAX_IMAGE_PIXELS = chart_max_pixels`, and `DecompressionBombWarning` raised as an error:
   - `format == "PNG"` → `verify()` → reopen → `load()`;
   - sides ≤ `chart_max_side`;
   - convert to RGB or RGBA;
   - **re-encode with no metadata and no `dpi`**.
4. Report a reason code only. Other `/out` entries are reported as a count; names are never forwarded.

**Stage B, on the host, assuming stage A may be compromised:**

1. A standard-library chunk walker:
   - signature checked;
   - every chunk length ≤ remaining bytes;
   - CRC-32 verified on every chunk;
   - **only IHDR (first, once), contiguous IDAT (≥ 1), and IEND (last, empty), with no trailing bytes.**
2. IHDR checks: 1 ≤ w, h ≤ 8192; w·h ≤ 16.8 Mpx; bit depth 8; colour type 2 or 6; compression, filter and interlace all 0.
3. Bounded inflate:
   - expected = h·(1 + w·channels);
   - `decompressobj().decompress(chunk, max_length)` in increments;
   - the output must be exactly the expected length, `eof=True` and `unused_data == b""`;
   - every row filter byte must be in {0…4}.
4. Host Pillow decode under the same limits; size and mode must match IHDR. Then **host re-encode**, so the stored bytes come from the host encoder.
5. **Store:**
   - stem = `task_id` if it matches `^[A-Za-z0-9_-]{1,40}$`, else `task-<sha256(task_id)[:12]>`;
   - name `<stem>-<run8>.png` (unique; D8);
   - write a temporary file with `O_CREAT|O_EXCL|O_NOFOLLOW` relative to the `CHARTS_DIR` directory fd (read at call time), `fsync`, `os.replace`;
   - the final path must be a direct child of `realpath(CHARTS_DIR)`.
6. `CHART_SAVED:` lines are removed from the displayed stdout and never used as a path (R-20). There is no stale-chart fallback.

**Correction B-6.** Matplotlib's raw output contains `tEXt` and `pHYs` chunks, which stage B rejects. That is intended: stage B only ever sees stage-A output. Test A-01 uses a real Matplotlib chart passed through stage A; test A-21 checks that a raw Matplotlib PNG reaching stage B (simulating a compromised stage A) is rejected.

**Text outputs:** decode UTF-8 with `errors="replace"`; strip NUL and C0 controls except `\n` and `\t`; apply caps; stderr keeps head and tail so `classify_error` still sees the exception line.

---

## 11. Concurrency-safe cleanup — lease and workspace IMPLEMENTED (P3.3); containers PLANNED (P3.6)

**P3.3 implementation** (`v2/execution/lease.py`, `v2/execution/workspace.py`; not wired into production yet):

- **Private root.** The default root is `<tempdir>/insightpilot-v2-<euid>`; tests use temporary roots.
  - It is created `0700` and opened `O_DIRECTORY|O_NOFOLLOW`.
  - It must be a directory owned by this user with no group or other permissions. Otherwise it is refused (`SandboxUnavailableError`): a symlink, a file, a shared mode or a foreign owner. Every level below it is checked the same way.
- **`InstanceLease.acquire()`:**
  - instance id = 128 random bits;
  - `instances/<id>/` is created exclusively (`0700`);
  - `lease.lock` (`0600`, exclusive create) is held with `flock(LOCK_EX)` for the lease's lifetime;
  - `lease.json` (`0600`) holds only `schema`, `instance_id`, `pid`, `boot_id`, `created_unix`: no credentials, no environment, no paths;
  - `runs/` (`0700`).
- **`release()`** (corrected in the P3.3 security correction):
  - **Serialised with run creation** by a per-lease lock.
    - Once `release()` starts, the lease is "releasing" and `JobWorkspace.create()` is refused.
    - A creation already in progress finishes first; its run is reported in `remaining_runs` and left in place.
  - It removes **its own** instance directory only if no runs remain, in this order: `runs/` (fails, removing nothing else, if a run exists), `lease.lock`, `lease.json`, the directory. So `lease.json` survives until the last file, and an incomplete removal stays inspectable.
  - It **always** drops the flock and closes all five owned descriptors (root, `instances/`, instance, lock, `runs/`), even when a removal step or the unlock fails.
  - **A failed removal is reported, never as success:** `{"released": True, "removed": False, "error": "<type>: <message>", ...}`.
  - It never touches another instance.
- **`inspect_instances()`: report only.** It never deletes, never blocks, and never touches containers.
  - It tries a **non-blocking** lock on each other instance's `lease.lock`.
  - States:

    | State | Meaning |
    |---|---|
    | `live` | the lock is held |
    | `stale` | the lock is free, or `boot_id` differs |
    | `indeterminate` | the lock is free and `lease.json` is missing (possibly still starting) |
    | `orphan` | no lock file, an unusable directory, or a mismatched id |
    | `unexpected` | the name is not an instance id |
    | `self` | this process holds the lease |

  - Liveness comes from the kernel lock, so PID reuse cannot fake it (tested). flock locks belong to an open file description, so two leases in one process are distinguishable.
- **`reconcile_stale_instances()` is disabled:** it raises until destructive reconciliation is separately reviewed.
- **`JobWorkspace`:**
  - `runs/<run_id>/` (`0700`, exclusive) holds `in/` (`0755`) with exactly two possible inputs, `code.py` and `dataset.csv` (`0644`, `O_CREAT|O_EXCL|O_NOFOLLOW` relative to the `in/` descriptor).
  - Generated code is size-checked **before** anything is written, then written verbatim.
  - There is **no output directory**: untrusted container output will never land on the host filesystem (§8.5).
  - `create()` removes a run directory on failure **only if this very call created it**. A run-id collision raises "run identifier collision; the existing run was left untouched" (correction).
  - Each workspace owns a **duplicate of the `runs/` descriptor**, so its cleanup works after the lease is released and never resolves a path relative to the current directory. `_remove_run_dir` refuses a missing descriptor (correction).
  - `cleanup()` removes exactly `runs/<run_id>` relative to that descriptor, using `shutil.rmtree`'s symlink-attack-resistant descriptor implementation (`avoids_symlink_attacks` is True on this Linux). Symlinks inside are unlinked, not followed. A run directory replaced by a symlink is removed as an entry only, and an error is raised.
  - **Retryable (correction):**
    - When cleanup starts, the workspace becomes `closing` permanently, so no input can be written again.
    - `cleaned` becomes True only after the deletion is verified.
    - A failed deletion raises `SandboxUnavailableError("workspace cleanup failed …; call cleanup() again")` and keeps the `runs/` descriptor for the retry.
    - Repeated successful cleanup is a no-op.
  - On platforms without the safe `rmtree`, only the known names are removed; anything unexpected is left in place and reported.
  - As a context manager it cleans up on success, refusal and exceptions.

- **Non-root validation — partly done in the cloud test container (P3.3 correction); still MANDATORY on VE-2.**
  - **What root-only tests can show.** The main suite runs as **uid 0**, and root bypasses discretionary access control. Those runs prove the descriptor-relative logic and the symlink, file-type, link-count and ownership comparisons. They do **not** establish permission-denied behaviour.
  - **What was run unprivileged** (2026-11-21, cloud test container; no account created; a throw-away pytest-only virtualenv, removed afterwards):
    - The three P3.3 test files ran as the pre-existing system account `nobody` (uid 65534) via `setpriv --reuid=65534 --regid=65534 --clear-groups`, with `--noconftest`.
    - Result: **104 passed, 3 skipped (root-only fixtures), 13 failed**.
    - **All 13 failures are the same deliberate fail-closed refusal**: "the approved data directory is not owned by this user". The repository's `data/` belongs to root, and the authoriser requires the running user to own the demo data. Two of the 13 surface as a different assertion, because that refusal pre-empts the oversize check and the concurrency check.
  - **Cross-account probe** with two unprivileged identities: the owner as uid 65534, and an intruder as uid 65533, an unnamed uid reached via `setpriv` with no account created.
    - **Under a hostile `umask 000`, the owner** authorised a demo dataset from its own copy, and the modes were exact: root, instance and run `0700`; `in/` `0755`; `code.py` and `dataset.csv` `0644`.
    - **The intruder** got `EACCES` listing the owner's root, entering the run directory and reading `code.py`. `inspect_instances()` and `InstanceLease.acquire()` on the owner's root were refused.
    - **A root pre-created ("squatted") by the intruder was refused by the owner.**
  - **Still required on VE-2:**
    - the real `ipval`/`ipprobe` accounts on a reproducible Ubuntu VM, running the **full** suite as `ipval`;
    - unprivileged equivalents for the root-only fixtures (device node, foreign owner);
    - the default root under the real shared `/tmp`.

    The cloud-container results are early evidence, not the evidence of record.

Original plan text (kept for the later container stages):

- **Instance lease:** `instances/<instance_id>/` (0700) contains `lease.json` (pid, process start time, `boot_id`; diagnostics only) and `lease.lock`, which is held with `flock` for the process lifetime.
- **Per run:**
  - run id = `secrets.token_hex(16)`;
  - container name `ip-<inst8>-<run12>`;
  - labels for instance and run;
  - `finally:` remove by **exact name**, remove the job directory, verify both are gone.
- **Stale detection at startup:**
  - for every *other* instance directory: if its `flock` can be acquired (owner dead; PID reuse cannot fool this) or its `boot_id` differs, remove only containers labelled with **that exact instance id**, then the directory;
  - a held lock means the instance is live and is never touched;
  - a container carrying the schema label but with no lease directory is **reported, never deleted** (`scripts/p3_cleanup.sh --orphans`, manual).
- **Self-termination:** the supervisor deadline, the host deadline and `podman --timeout` together with `--rm` mean a crashed app leaves no long-lived containers.
- No generic project-wide label filter is ever used for deletion.

---

## 12. Validation environment and claim rule — PLANNED

- **Runtimes:** rootless Podman (preferred), rootless Docker (alternative adapter, same tests). Rootful runtimes are refused.
- **VE-1: GitHub Actions `ubuntu-24.04`, usable only if its capability probe passes.**
  - Probe: runner user ≠ root; `podman info` reports rootless, cgroup v2 and controllers ⊇ {cpu, memory, pids}; a 64 MiB container allocating 128 MiB is OOM-killed; a PID-limit probe is enforced.
  - If any check fails, the job **fails** with "environment unsuitable". It never reports tests as passed or skipped.
  - Feature-branch workflow only; `push` and `workflow_dispatch`; no secrets; no `pull_request_target`; read-only token.
- **VE-2: reproducible Ubuntu 24.04 VM** (`validation/vm/user-data.yaml`, planned):
  - pinned cloud image SHA-256;
  - apt packages from a fixed `snapshot.ubuntu.com` timestamp (podman, crun, uidmap, passt);
  - unprivileged `ipval` user with subuid/subgid ranges, linger enabled, and delegation verified (`user@<uid>.service/cgroup.controllers ⊇ {cpu, memory, pids}`);
  - second account `ipprobe`;
  - optional Docker-rootless profile.
  - It runs on a reviewer-controlled Linux host: never the examination laptop, never the cloud design session.
- **`scripts/p3_validate.sh`** (planned) writes JUnit XML plus `evidence.json`: git SHA, kernel, runtime and version, rootless and cgroup facts, image id, lock hash, per-test outcomes with skip counts.
- **Claim rule:** "P3 isolation verified" requires an evidence report in which every `@container` test **executed and passed, with 0 skips** (`IP_TEST_REQUIRE_CONTAINER=1` turns a missing runtime into a failure). **Skipped tests are not proof.**

---

## 13. Performance — PROVISIONAL

- **Added per-run cost:** container create and teardown, supervisor, a frame of up to about 11.3 MiB, two PNG decode/re-encode passes, the dataset copy. **One-time cost:** the preflight self-test.
- **Harness** (`scripts/bench_sandbox.py`, planned, run in the VE):
  - workloads `print(1)`, the golden sales task, and the 15,600-row attendance groupby plus chart;
  - legacy vs container, N = 30 each, interleaved, cold and warm;
  - reported: p50, p95, max, stage split, `memory.peak`, image size, environment fingerprint.
- **Provisional targets, to be confirmed by measurement:** container p95 overhead ≤ 1.5 s per execution; golden end-to-end ≤ 2× V1 wall time.
- **Tokens and cost:** no prompt changes; LLM calls per skill must equal the golden snapshots. Live budget is $0.

---

## 14. Assumptions that require real rootless-runtime validation

| Assumption | Test |
|---|---|
| Rootless default mapping (host U → container 0; 10001 → subuid) and R1 read access through 0700 parents | O-01…O-04 |
| `readonly` bind gives EROFS; nothing outside `/out` and `/tmp` is writable | O-02, S-03 |
| tmpfs `size=` and `nr_inodes=` are enforced (Podman and Docker) | S-01, S-02 |
| tmpfs pages are charged to the container memory cgroup | S-04 |
| `--ipc=none` removes `/dev/shm`; `--read-only-tmpfs=false` adds nothing | S-03 |
| `passthrough` / `none` logging writes no host log files | S-05 |
| `--network=none` + `--dns=none`: only `lo`; host loopback and abstract sockets unreachable | I-06 |
| `--cap-drop=ALL`, `no-new-privileges`, default seccomp active (`Seccomp` = 2) | I-07, I-13 |
| cgroup v2 limits actually applied (`memory.max`, `pids.max`, `cpu.max` read back) | preflight self-test, R-01…R-06 |
| PID-1 signal semantics with a Python supervisor; `PR_SET_DUMPABLE` blocks same-uid ptrace and `/proc/1/*` | I-10, I-14 |
| `kill(-1, SIGKILL)` from PID 1 removes every child, including `setsid` daemons | I-11 |
| `podman --timeout` and `--rm` remove containers after an app crash | C-06 |
| Rootless Docker equivalents for every row above | the same tests with the Docker adapter |
| P3.3 host filesystem protections (0700 workspace and instance directories, owner checks, private root under a shared `/tmp`, `umask`) hold for an **unprivileged** user against a second unprivileged account. **Early evidence in the cloud test container** (uid 65534/65533, see §9 and §11); not yet on VE-2 | the full suite re-run as VE-2 `ipval`, plus cross-account probes from `ipprobe` (mandatory) |

---

## 15. Image, CI and dependency changes

None in P3.1. Every one of these needs its own review gate:

- new pinned dependencies (inside the image only; host `requirements.txt` unchanged);
- the GitHub Actions workflow file;
- the VM definition.

---

## 16. Acceptance-test matrix

Test IDs use these prefixes (correction B-4 renamed the dataset tests to DS-xx):

| Prefix | Area |
|---|---|
| F | flags |
| CT | contracts |
| L | launcher |
| DS | datasets |
| O | ownership |
| S | storage |
| I | isolation |
| R | resources |
| A | artifacts |
| C | cleanup |
| G | regression |
| IMG | image |
| P | performance |

**Environments:**

- **C** = Linux without a container runtime (FakeRuntime where needed; results labelled **wiring-only**).
- **VE** = validation environment (must execute; a skip counts as failure).
- ★ = AST scanner bypassed.

| ID | Test | Env | Status |
|---|---|---|---|
| **F-01** | P0 `test_feature_flags.py` unchanged | C | **IMPLEMENTED: 28/28 pass** |
| F-02 | Hardened with backend `subprocess`, unset or blank → `InsecureConfigurationError` (exam, secure, directly constructed) | C | **IMPLEMENTED** (`tests/v2/p3/test_flags_p3.py`) |
| F-03 | Hardened + container valid; `resolve` returns `"container"` only if available | C | **IMPLEMENTED** |
| F-04 | Hardening off → `"legacy"` without consulting availability | C | **IMPLEMENTED** |
| F-05 | Resolver never returns `"subprocess"` even when "available"; error is still a `FeatureFlagError` | C | **IMPLEMENTED** |
| F-06 | Docs state the container requirement and the `ui/app.py` bypass | C | **IMPLEMENTED** |
| F-07 | Launcher refuses the secure profile (P2 missing) | C | **IMPLEMENTED in P3.2a** as part of the refusal of every non-default flag (`test_unsupported_v2_features_are_refused[secure_profile]`, `test_refusal_stops_before_ui_app_executes[secure_profile]`) |
| **CT-01** | Exactly two isolation levels, no process tier; only the container is OS-isolated; backend mapping rejects `"subprocess"` | C | **IMPLEMENTED** (`test_contracts_p3.py`) |
| CT-02 | `Limits`: frozen, positive, consistent; derived frame budget; default wall clock = V1 `MAX_EXECUTION_TIME` | C | **IMPLEMENTED** |
| CT-03 | Error families separate; fixed artifact reason set | C | **IMPLEMENTED** |
| CT-04 | `ExecutionResult` invariants; run-id format | C | **IMPLEMENTED** |
| CT-05 | Contract purity (AST allowlist, fresh-interpreter import set, reviewed-file scope, now including `v2/launch_app.py`); no V1 module imports `v2`; contracts never import the launcher | C | **IMPLEMENTED** (`test_contract_purity.py`) |
| CT-06 | `Limits.cpus` rejects NaN and ±infinity; accepts positive finite numbers | C | **IMPLEMENTED (P3.2a)** |
| **L-01** | AppTest `v2/launch_app.py` vs `ui/app.py`, flags off, 4 flows (§7.1a): identical element trees, exceptions, LLM calls and `session_state` at every step; equivalent report files; byte-identical charts | C | **IMPLEMENTED (P3.2a)** (`test_launcher_is_equivalent_to_ui_app`) |
| L-01b | Golden parity through the launcher: `sales_ui_approve_safe_mode_on` (approve; retries; golden answers; MD, PDF, notebook) and `sales_graph_safe_mode_off` (graph path) | C | **IMPLEMENTED (P3.2a)** |
| L-02 | Flags off: `sandbox.executor.execute_code` (and its code object), `agent.nodes.executor.execute_code`, `executor_node`, `streamlit.file_uploader` and `runpy.run_path` identities unchanged after a full run; fresh-process checks: `ui/app.py` imports no `v2` and no flags; the launcher loads no `v2.execution` | C | **IMPLEMENTED (P3.2a)** |
| L-03 | Magic invariant: Streamlit's transform of `ui/app.py` (and of the launcher) differs only by its own inserted import | C | **IMPLEMENTED (P3.2a)** (replaces the earlier "only docstrings and calls" wording) |
| L-14 | Import resolution equals `streamlit run ui/app.py` (simulated `_fix_sys_path`); idempotent across reruns; no `pages/` folders | C | **IMPLEMENTED (P3.2a)** |
| L-15 | `runpy` execution semantics equal Streamlit's main-script semantics (probe scripts: `__name__`, `__file__`, `__main__`, cwd, `st.stop`, `st.rerun`, session state); `__main__` and `argv[0]` restored | C | **IMPLEMENTED (P3.2a)** |
| L-16 | Refusal before `ui/app.py` runs: 8 unsupported and 5 invalid configurations via AppTest (no `run_path` call, one error, no app widgets or state, no LLM calls) and one in a fresh process (no `agent.*` import) | C | **IMPLEMENTED (P3.2a)** |
| L-17 | 10 reruns: identical page, launcher adds nothing to `sys.path` (ui/app.py's own V1 insert unchanged); a V2 flag appearing mid-session stops the next rerun | C | **IMPLEMENTED (P3.2a)** |
| L-18 | Launcher source constraints: imports only `os`, `runpy`, `sys`, `config.feature_flags` and `streamlit`; no attribute assignment, `exec`, `eval`, `open`, `setattr`, `dotenv` or environment writes; fixed target path | C | **IMPLEMENTED (P3.2a)** |
| L-04 | Flags on, FakeRuntime: Path 1 (`analyst_graph.invoke`; also the UI with safe mode off) and Path 2 (the UI approve loop through the launcher) each call the runtime once per executor attempt that passed the V1 scanner; nothing runs before approval; tripwire never fires; no V1 sandbox subprocess is spawned; the code never runs (no GOLDEN output) | C | **IMPLEMENTED (P3.2b)** |
| L-05 | 15 reruns in hardened test mode: one bootstrap initialisation, binding verified every rerun, 0 repairs | C | **IMPLEMENTED (P3.2b)** |
| L-06 | Real `importlib.reload` of `agent.nodes.executor` → `executor_node` hits the tripwire (fail closed) → next `ensure_ready` repairs (counted). Reload of `sandbox.executor` → drift detected and repaired. Module dicts fully restored afterwards | C | **IMPLEMENTED (P3.2b)** |
| L-07 | Call-site census: only `executor_node` calls `execute_code` | C | **IMPLEMENTED (P3.2b)** |
| L-08 | Runtime preflight failure (simulated attestation mismatch) → `FAILED`, sticky over 3 reruns, one error element, no app widgets, no binding, no LLM calls | C | **IMPLEMENTED (P3.2b)** |
| L-09 | Hardened path requires `server.fileWatcherType=none` and `server.runOnSave=false`: checked at initialisation and on every run; the V1-compatible path is not affected | C | **IMPLEMENTED (P3.2b)** |
| L-10 | Deployment-unit invariants (§20 Q1) → refused if violated | C | PLANNED: waits for the Q1 decision. The secure-profile and file-watcher parts are implemented. |
| L-11 | Concurrency: 16 threads initialise once; 8 parallel `executor_node` calls get independent results and unique run ids; two interleaved AppTest sessions share one hardened mode (one initialisation). The ≤ 2 concurrent-container limit belongs to P3.6 | C | **IMPLEMENTED (P3.2b)** (limit PLANNED) |
| L-19 | Production fail-closed: hardened (all prerequisites reported), secure profile, other V2 features, invalid flags; sticky; no environment variable can supply a runtime; test-only entry points refuse outside pytest and are never called outside `tests/` | C | **IMPLEMENTED (P3.2b)** |
| L-20 | Dispatcher: V1 tuple contract for every outcome; any runtime exception → `SandboxUnavailableError`; bad results → `SandboxProtocolError`; never references the V1 executor | C | **IMPLEMENTED (P3.2b)** |
| L-21 | Leak guard: a deliberate binding leak fails and is repaired before a following V1 test; without the guard the leak breaks that V1 test; a runtime falling back to V1 fails (subprocess pytest runs) | C | **IMPLEMENTED (P3.2b)** |
| L-12a | Playwright real browser, no LLM: both entry points render identically up to the missing-key error; refusal page with V2 flags (§7.1b) | C | **Run once in P3.2a as uncommitted evidence: identical at all 3 steps; refusal correct.** Committing it as a test is PROPOSED (needs approval) |
| L-12 | Playwright real browser via a test-only mock-LLM launcher: demo → run → approve → chart `<img>` loads → report present | VE (C wiring-only) | PLANNED (P3.8) |
| L-13 | Direct `ui/app.py` with hardened flags inside the deployment unit's constraints: documents the bypass; 0 executions when the key is withheld | C | PLANNED |
| **DS-01/02** | Demo pins; tampered copy refused; pins equal the golden JSON | C | **IMPLEMENTED (P3.3)** |
| DS-03 | Any non-demo path (including any `/tmp` upload) → `DatasetNotAuthorizedError` in demo-only mode | C | **IMPLEMENTED (P3.3)** |
| DS-05 | Traversal, relative, NUL, newline, subdirectory refused before any open | C | **IMPLEMENTED (P3.3)** |
| DS-06/07/08 | Symlink, FIFO (no hang), directory, socket, device, hardlink refused | C | **IMPLEMENTED (P3.3)** |
| DS-09 | Foreign owner refused | C / VE | **IMPLEMENTED (P3.3)** (root-only fixture; runs in this environment) |
| DS-10/11 | Oversize (abort at cap+1); swap or in-place modification during copy → refused and partial copy deleted | C | **IMPLEMENTED (P3.3)** |
| DS-12 | V1 S3 crafted path → refused; marker absent; 0 executions | C | **IMPLEMENTED (P3.3)** at the authoriser level (not yet through the dispatcher) |
| DS-13 | Job inputs are only `code.py` and `dataset.csv`; argv contains no `csv_path` or `task_id` bytes | C | **Inputs part IMPLEMENTED (P3.3)**; the argv part waits for the container backend (P3.6) |
| DS-15 | Demo directory reached through a symlinked `DATA_DIR` accepted; symlinked final component refused (B-5) | C | **IMPLEMENTED (P3.3)** |
| WS-01…10 | Workspace: private layout and modes; 128-bit unique run ids; verbatim code; code size checked before writing; only two input names; exclusive creation; planted symlinks never followed (input, inner, replaced run directory); cleanup on success, refusal and exception, idempotent; sibling runs untouched; 16 simultaneous runs isolated | C | **IMPLEMENTED (P3.3)** |
| LS-01…09 | Lease: private root refusals (symlink, mode, file, foreign owner, symlinked `instances/`); minimal metadata without credentials; two leases distinct; release never touches another live instance; release with active runs leaves everything; cross-process live → stale after SIGKILL; crash → stale and untouched; PID reuse cannot fake liveness; previous boot → stale; orphans and unexpected entries reported, not deleted; destructive reconciliation disabled | C | **IMPLEMENTED (P3.3)** |
| **O-01…04** | Ownership (§8.2) | VE (O-01 also C) | PLANNED |
| **S-01/02** ★ | `/out` and `/tmp` fill: ENOSPC at the byte and inode caps | VE | PLANNED |
| S-03 ★ | Writes to `/`, `/in`, `/var/tmp`, `/run`, `/dev/shm`, `/dev` fail | VE | PLANNED |
| S-04 ★ | Fill tmpfs + allocate 950 MiB → OOM | VE | PLANNED |
| S-05 ★ | Exhaustion run: host free space changes < 1 MiB beyond host-written inputs; no log growth | VE | PLANNED |
| S-06 ★ | 100 MB stdout flood; oversized or forged frame → host bounded, `SandboxProtocolError` | VE / C | PLANNED |
| **I-01** ★ | Dummy `GOOGLE_API_KEY` and proxy variables in parent: absent in the container; environment ⊆ allowlist | VE | PLANNED |
| I-02 ★ | The 5 P0 bypass shapes are contained | VE | PLANNED |
| I-03/04/05 ★ | Host files, repository, host `/proc/*/environ` unreadable; writes outside fail; repository hashes unchanged | VE | PLANNED |
| I-06 ★ | Host loopback listener, abstract socket, DNS, 169.254.169.254 unreachable; interfaces = {`lo`} | VE | PLANNED |
| I-07/08 | uid ≠ 0, `CapEff` 0, `NoNewPrivs` 1, `Seccomp` 2; mountinfo allowlist; no runtime sockets | VE | PLANNED |
| I-09 | Rootful runtime, euid 0, cgroup v1, missing controllers → preflight refuses | C | PLANNED |
| I-10 ★ | Child sends SIGINT, SIGTERM, SIGHUP, SIGUSR1, SIGKILL, SIGSTOP to PID 1; ptrace(1); `/proc/1/mem`; `/proc/1/fd/1` → no effect; frame intact (B-2) | VE | PLANNED |
| I-11 ★ | Background daemon outliving the child is killed before extraction; `/proc` shows only PID 1 | VE | PLANNED |
| I-12 | Attestation mismatch → `SandboxUnavailableError` | C | PLANNED |
| I-13 ★ | keyctl, unshare, mount denied | VE | PLANNED |
| I-14 | Host stop: `kill -s TERM` has no effect; `kill -s KILL` stops; zombie reaping under a 50-grandchild churn leaves `pids.current` back at baseline | VE | PLANNED |
| **R-01…06** ★ | Memory OOM message; PID limit; CPU loop → V1-identical timeout message; SIGTERM-ignoring child; `nofile`; fork bomb | VE | PLANNED |
| **A-01…21** | Artifact corpus: valid chart through stage A; wrong magic; renamed JPEG/SVG; bad CRC; tEXt, iCCP, zTXt, eXIf; APNG; interlaced; 16-bit; palette; dimensions over cap; pixel bomb; zlib bomb; truncated IDAT; trailing data and polyglot; double IHDR; empty; over size; symlink, hardlink, FIFO or directory `chart.png` (VE); atomic unique store and safe stem; raw Matplotlib PNG at stage B rejected (B-6) | C / VE | PLANNED |
| **C-01…08** | Exact-name cleanup; live instance B untouched by A's reconcile; stale instance cleaned up via flock even with PID reuse; orphan reported but not removed; crash → self-termination; changed `boot_id` → stale; 8 dispatches with limit 2 | VE / C | Host-side detection IMPLEMENTED (P3.3, see LS-xx). Container cleanup, and any destructive stale cleanup, PLANNED and disabled |
| **G-01** | Full suite, flags off | C + VE | **IMPLEMENTED for P3.1: see §17** |
| G-02 | Golden snapshots byte-identical (`git diff --exit-code tests/v2/golden`) | C | **IMPLEMENTED (checked for P3.1)** |
| G-03 | Golden end-to-end, 4 datasets, container backend: 28 answers; behaviour equals the committed snapshots; LLM calls equal | VE | PLANNED |
| G-04/05 | Retry semantics identical; UI AppTest through the launcher with the container backend | VE | PLANNED |
| G-06 | 13 strict-xfails remain xfailed in the same session after hardened fixtures ran, **and 0 tripwire hits**: a leaked tripwire would raise `InsecureConfigurationError`, which is not the `AssertionError` the xfails require, so it would show up as a failure (B-11) | C + VE | PLANNED (13 xfails checked for P3.1) |
| G-07 | No diff in V1 paths or P0 test files | C | **IMPLEMENTED (checked for P3.1)** |
| **IMG-01…05** | Hash-locked build; labels; no pip, setuid or `VOLUME`; user 10001; staged build context; reproducible package set | VE | PLANNED |
| **P-01/02** | Benchmark; preflight duration | VE | PLANNED |

**Additional rule (correction B-10):** P3.4 unit tests call pure functions (frame encode and decode, signal setup, attestation parsing) with benign fixtures only. No host-side execution backend may exist; a test asserts that the dispatcher has exactly one backend, `ContainerRuntime`.

---

## 17. Implementation stages

| Stage | Scope | Environment | Status |
|---|---|---|---|
| **P3.1** | Baseline re-run in a scratch venv; B1 flag rule; pure contracts; this document | C | **IMPLEMENTED** |
| **P3.2a** | Launcher flags-off compatibility boundary: `prepare_sys_path`, strict flag check (only V1 defaults start), `runpy` execution of the unchanged `ui/app.py`; equivalence and parity tests; `Limits.cpus` finiteness | C | **IMPLEMENTED** |
| **P3.2b** | Bootstrap (process-wide mode, fail closed), executor binding, V1 tripwire, dispatcher, runtime interface, file-watcher enforcement, secure-profile gate, leak guard; FakeRuntime wiring tests. Container always unavailable, so production fails closed. | C | **IMPLEMENTED** (deployment-unit checks wait for Q1) |
| **P3.3** | Demo-only `DatasetAuthorizer`, instance lease, `JobWorkspace` (not wired into the production dispatcher; destructive reconciliation disabled) | C | **IMPLEMENTED** |
| P3.4 | Frame protocol, strict decoder, host PNG validator, re-encode and store; pure supervisor, runner and canonicaliser functions (no backend) | C | PLANNED |
| P3.5 | Runtime image: Containerfile, hash lock, staged build, verification. **Dependency review gate.** | VE | PLANNED |
| P3.6 | Container backend: detection, argv builder, run/kill/remove, preflight self-test, reconcile, dispatcher. **Sandbox-permission review gate.** | C (unit) + VE | PLANNED |
| P3.7 | Resource and storage limit enforcement; optional custom seccomp | VE | PLANNED |
| P3.8 | Integration: golden end-to-end and UI through the launcher; real browser; benchmark; R-24 alignment in docs; handoff. **Senior review before merging into `feature/insightpilot-v2`.** | VE + C | PLANNED |

**P3.1 test results** (scratch venv, §2.4 environment):

| Run | Result |
|---|---|
| Base `1fd3af3c` (before changes) | 270 passed, 13 xfailed, 0 failed |
| After P3.1, the 270 existing tests plus 13 xfails | unchanged: 270 passed, 13 xfailed |
| New `tests/v2/p3` | 166 passed (F-02…F-06, CT-01…CT-05) |
| Full suite after P3.1 | **436 passed, 13 xfailed, 0 failed, 0 skipped** |
| Golden snapshots | byte-identical; never regenerated |

**P3.2a test results** (same environment):

| Run | Result |
|---|---|
| Base `147f8340` (P3.1, before changes) | 436 passed, 13 xfailed, 0 failed |
| New launcher tests `tests/v2/p3/test_launcher_p3.py` | 49 passed (2 visible AppTest stale-widget workaround warnings) |
| New `Limits.cpus` tests | 9 passed |
| Full suite after P3.2a | **494 passed, 13 xfailed, 0 failed, 0 skipped** |
| The 13 V1 strict-xfails; golden snapshots | unchanged; never regenerated |

**P3.2b test results** (same environment):

| Run | Result |
|---|---|
| Base `5fe60aa9` (P3.2a, before changes) | 494 passed, 13 xfailed, 0 failed (final P3.2a run) |
| Existing P3 tests on the new code | 225 passed (224 + one purity case for `runtime.py`) |
| New `tests/v2/p3/test_bootstrap_p3.py` | 63 passed |
| Mutation checks (throw-away copies) | V1 fallback on runtime error → 6 failures; silent production downgrade to V1 → 7; mode switching → 2; no tripwire → 8 |
| Full suite after P3.2b | **558 passed, 13 xfailed, 0 failed, 0 skipped, 0 xpassed**; 3 warnings, all the known Streamlit 1.59 AppTest stale-widget workaround (2 in the P3.2a equivalence flow, 1 in the P0 UI smoke test) |
| The 13 V1 strict-xfails; golden snapshots | unchanged; never regenerated |

**P3.3 test results** (same environment):

| Run | Result |
|---|---|
| Base `d9d5787a` (P3.2b, before changes) | 558 passed, 13 xfailed, 0 failed |
| New `tests/v2/p3/test_datasets_p33.py` | 60 passed, 0 skipped (the root-only device and ownership cases ran) |
| New `tests/v2/p3/test_workspace_lease_p33.py` | 44 passed |
| Scope guard (`test_contract_purity.py`) | 10 passed (three reviewed modules added) |
| Mutation checks (throw-away copies) | dataset open follows symlinks → 2 failures; no hash comparison → 2; no metadata recheck → 2; basename-only path check → 2; hard links allowed → 1; inspection deletes stale instances → 3; cleanup follows a replaced run directory → 1; shared workspace root accepted → 4 |
| Full suite after P3.3 | **662 passed, 13 xfailed, 0 failed, 0 skipped, 0 xpassed** (558 + 60 + 44); 3 warnings, all the known Streamlit 1.59 AppTest stale-widget workaround (2 in the P3.2a equivalence flow, 1 in the P0 UI smoke test). Run as **uid 0**; see the non-root validation requirement in §9 and §11 |
| The 13 V1 strict-xfails; golden snapshots | unchanged; never regenerated |

**P3.3 security-correction results** (same environment):

| Run | Result |
|---|---|
| Base `78355f3e` (P3.3, before changes) | 662 passed, 13 xfailed, 0 failed, 0 skipped |
| New `tests/v2/p3/test_lifecycle_p33.py` | 16 passed (3 consecutive runs) |
| Existing P3.3 tests | 104 passed. 3 assertions narrowly updated: the release report gained an `error` field; deletions moved to `_remove_own_directory` |
| Mutation checks (throw-away copies) | pre-fix unconditional removal on failed creation → 1 failure; `cleaned` set before deletion → 2; teardown only on success → 5; creation not serialised with release → 1; failed removal reported as success → 6 |
| Non-root (cloud container, uid 65534) | 104 passed, 3 skipped (root-only), 13 failed, all the deliberate refusal of the root-owned `data/`. The cross-account probe with uid 65533 was denied everywhere (§9, §11) |
| Full suite after the correction (uid 0) | **678 passed, 13 xfailed, 0 failed, 0 skipped, 0 xpassed** (662 + 16); 3 warnings, all the known Streamlit 1.59 AppTest stale-widget workaround (2 in the P3.2a equivalence flow, 1 in the P0 UI smoke test) |
| The 13 V1 strict-xfails; golden snapshots | unchanged; never regenerated |

---

## 18. Rollback

- **P3.1:** `git revert <P3.1 commit>` on `feature/v2-p3-secure-execution`. No history rewriting. Reverting restores the earlier acceptance of `hardened=1` with `backend=subprocess`, which was harmless because no component reads the flags.
- **Later stages:** revert the stage commit or merge. Operationally, unset the flags or launch `ui/app.py`.
- **VE artefacts:** `scripts/p3_cleanup.sh --instance <id>`, then remove the image tag.
- **Examination laptop:** nothing is installed there, so there is nothing to roll back.

---

## 19. Remaining risks and non-goals

**Risks:**

1. **Shared kernel.** A kernel exploit escapes the namespaces.
2. **The direct `ui/app.py` launch bypasses V2** (§20 Q1).
3. **The supervisor shares uid 10001 with the child.** Separation relies on PID-namespace-init semantics and non-dumpability, which must be proven (I-10).
4. **The host parses untrusted bytes** (frame, text, PNG): bounded and structurally validated first; a residual Pillow or zlib risk remains.
5. **Uploads are unsupported in hardened mode** until approved. V1 upload writing is unchanged (P2).
6. **`CHARTS_DIR` is shared across sessions** (R-18): unique names prevent overwrites but not visibility (P5).
7. **Runtime configuration drift** is caught by attestation only for the attested properties.
8. **Supply chain:** public base image and wheels, hash-pinned.
9. **Unauthenticated Streamlit server** (R-18).
10. **VE-1 may prove unsuitable.**
11. **Performance unmeasured.**
12. **Streamlit version drift** could affect the `runpy` equivalence (L-01, L-03).

**Non-goals for P3:**

- a secure deployment, which requires P2, P5 and P8;
- prompt-injection, HTML, lesson or export guardrails (P2);
- approval enforcement (P8);
- notebook-export execution (R-15);
- macOS or Windows isolation (hardened mode refuses there; the exam laptop is unchanged);
- multi-tenant hosting and authentication;
- network access or package installs inside the sandbox;
- warm container pools;
- side-channel resistance.

---

## 20. Unresolved engineering questions (positions for review)

### Q1. A direct V1 launch is not safe just because the API key is withheld. What is the secure deployment boundary?

Withholding the key stops V1 from planning and generating code, but a V1 process still:

- serves an unauthenticated UI on the network (R-18);
- writes uploads to `/tmp/{name}` while following symlinks and accepting traversal names: attacker-chosen file writes with **no LLM involved** (R-07);
- parses untrusted CSVs in the host process;
- renders unescaped untrusted HTML (R-13);
- gains code execution again as soon as anyone puts a key into `.env` or the environment.

Key withholding is therefore a **tripwire, not the boundary**.

**Position:** the secure deployment boundary is a **deployment unit**, not a code path:

1. **A dedicated unprivileged service account** (no login shell, no sudo) is the only identity that can read the V2 secrets file (0600) and the instance base, and the only one that runs the app. Operators without that account cannot start an app process holding the key.
2. **One supervised entrypoint** (a systemd unit, or the application's own container in a later phase) executes `scripts/run_insightpilot_v2.sh` → `streamlit run v2/launch_app.py …`. No other unit or command starts InsightPilot in that deployment.
3. **No `.env` in the deployed checkout**; the key is loaded by the launcher only after preflight; the checkout is read-only to the service account.
4. **Network:** `--server.address 127.0.0.1` until an authenticating reverse proxy exists (P5).
5. **The launcher self-checks the invariants it can observe** and refuses to start if any fails: euid not root; flags valid; file watcher off; no `.env`; key not inherited; secrets file owner and mode; rootless runtime.
6. **A trusted administrator** who deliberately runs `ui/app.py` inside the unit is outside the threat model (A-5) and is documented as such.

Until P2, P5 and P8 are implemented and validated, **no deployment is called secure**, and the secure profile is refused.

### Q2. Demo datasets first; no Streamlit uploader monkeypatch without separate approval

Adopted. P3.3 implements the demo-only policy (§9.2). The upload observer (§9.3) is deferred to a separately approved step, or replaced by P2's server-side upload registry.

### Q3. How do bounded binary frames coexist with the stdout/stderr byte limits?

Answered in §8.5:

- separate streams (child pipes → supervisor; supervisor → container stdout);
- per-section caps applied while collecting;
- a derived frame maximum (`Limits.max_frame_bytes`, IMPLEMENTED);
- declared lengths checked against their own caps;
- the host reads at most `max_frame_bytes + 1` bytes;
- runtime CLI stderr capped separately and never shown to users.

### Q4. A custom PID-1 supervisor or a separate init process?

| Option | Signal protection for the supervisor | Privilege needed | Complexity |
|---|---|---|---|
| **(a) Custom supervisor as PID 1, no `--init` (recommended)** | Yes: PID-namespace init ignores same-namespace signals without handlers; non-dumpable blocks ptrace | None beyond uid 10001 | Must reap zombies and reset signal dispositions (B-2) |
| (b) `--init` (catatonit/tini) as PID 1, supervisor as PID 2 | **No:** a same-uid child can SIGKILL PID 2, so extraction integrity needs a different uid for the supervisor | `CAP_SETUID`/`CAP_SETGID` at start | Two processes plus capability handling |

Position: (a). All signal behaviour is **REQUIRES RUNTIME VALIDATION**: tests I-10, I-11 and I-14.

### Q5. Runtime-dependent properties

UID mappings, mounts, tmpfs limits, image settings and the security properties are listed in §14. None is claimed until a VE evidence report shows the tests executed and passed with 0 skips.

### Q6. P3 does not secure the application

P3 isolates TB3 only. TB1, TB2, TB4 and TB5 (P2) and approval enforcement (P8) remain open. Every P3 handoff must say "TB3 isolation (verified or not verified); application not secure".

---

## 21. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | B1: hardened requires container; no process tier | **Approved and IMPLEMENTED (P3.1)** |
| D2 | Launcher: `runpy` + bootstrap + dispatch binding + V1 tripwire; file watcher off | **IMPLEMENTED (P3.2a + P3.2b)**; binding is active only in test mode until a validated backend exists |
| D3 | Deployment unit (§20 Q1) with launcher self-checks; key withheld from direct launches | **UNRESOLVED:** reviewer to confirm the boundary definition. P3.2b documents the boundary (§7.4) and implements only the file-watcher and secure-profile checks |
| D4 | Ownership R1; reject R2, R4 and R5; defer R3 | Conditionally accepted |
| D5 | tmpfs-only storage, `--read-only-tmpfs=false`, `--ipc=none`, no logs, frame-only extraction | Conditionally accepted |
| D6 | Custom PID-1 supervisor, same uid, non-dumpable, signals reset (B-2); custom seccomp optional | Conditionally accepted; requires runtime validation |
| D7 | Datasets: **demo-only first**; uploader observer only after separate approval | **IMPLEMENTED (P3.3, demo-only)**; uploader observer not implemented |
| D8 | Unique chart names `<stem>-<run8>.png` | Conditionally accepted |
| D9 | Provisional limits (`DEFAULT_LIMITS`) | Contract IMPLEMENTED; values provisional |
| D10 | Two concurrent containers per instance; 120 s queue wait | Conditionally accepted |
| D11 | VE-1 probe first, plus the VE-2 VM definition; who runs VE-2 | **Open** |
| D12 | Image base and contents (§8.7) | P3.5 review gate |
| D13 | Test-only mock-LLM launcher for Playwright | Open (P3.8); a no-LLM real-browser check (L-12a) is proposed separately |
| D14 | Scratch venv for the baseline in the cloud session only | **Approved and done (P3.1)** |
| D15 | Policy and infrastructure failures raise; no retries, no fallback | Contract IMPLEMENTED (error families) |
| D16 | Stages P3.1–P3.8 with review gates | Conditionally accepted |
| D17 | Top-level package `v2/` | **IMPLEMENTED** |
| D18 | Rejected chart → success with `chart_path=None` and a warning | Contract IMPLEMENTED (`ExecutionResult`) |
| D19 | Policy and infrastructure error hierarchy (B-3) | **IMPLEMENTED** |

---

## 22. Revision history

| Revision | Content |
|---|---|
| rev. 1 | Initial P3.0 proposal: rootless container recommended. It proposed a labelled process tier, later **rejected**. |
| rev. 2 | Reviewer decisions 1–9: B1; launcher reliability; ownership R1; tmpfs storage plus frame extraction; dataset authorisation; two-stage artifact validation; lease-based cleanup; VE claim rule; 8 stages |
| rev. 2.1 | 11 corrections, all integrated into this document: **B-1** exact flag and doc diffs (§5.2); **B-2** reset PID-1 signal dispositions (§8.4, I-10); **B-3** separate error families (§6); **B-4** dataset tests renamed DS-xx (§16); **B-5** demo-path realpath comparison (§9.2, DS-15); **B-6** strict PNG allowlist applies to stage-A output only (§10, A-21); **B-7** uploader observer return shapes (§9.3, deferred); **B-8** test-only bootstrap reset (§7.2); **B-9** more runtime flags to verify (§8.1); **B-10** P3.4 tests use pure functions only; single backend (§16); **B-11** a tripwire leak fails the V1 xfails loudly (G-06) |
| rev. 2.1 + P3.1 | Status markers; P3.1 implementation and results; reviewer directions (demo-only datasets; no uploader monkeypatch) and open questions Q1–Q6 |
| rev. 2.1 + P3.2a | Launcher flags-off boundary implemented (§7.1–7.1b); **correction:** Streamlit magic always inserts one unused import, and wraps nothing in `ui/app.py` (§2.2 item 9, L-03); `sys.path` mirroring of `streamlit run ui/app.py`; `Limits.cpus` finiteness; AppTest limitations and the real-browser proposal |
| rev. 2.1 + P3.2b | Bootstrap, binding, tripwire, dispatcher and runtime interface implemented (§7.1c, §7.2, §7.5); deployment boundary documented (§7.4); leak guard; **plan refinements:** flags are frozen per process and compared on every run (a mismatch is a sticky refusal, never a mode switch); a `FAILED` state reached from `HARDENED` keeps the binding installed |
| rev. 2.1 + P3.3 | Demo-only `DatasetAuthorizer`, `JobWorkspace` and `InstanceLease` implemented and documented (§9, §11); not wired into production; destructive stale reconciliation disabled; Linux-only claims |
| rev. 2.1 + P3.3 correction | Run-id collision never deletes an existing run; retryable workspace cleanup (`closing`/`cleaned`); exception-safe lease release with reported failures, metadata-preserving removal order and release/creation serialisation; workspaces own their `runs/` descriptor; early non-root and cross-account evidence (uid 65534/65533); VE-2 non-root run still mandatory |
