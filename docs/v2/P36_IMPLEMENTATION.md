# InsightPilot V2 P3.6 stages 0–3: implementation record

**Branch:** `feature/v2-p36-container-runtime`, from `feature/v2-p51-hardened-integration` at `05d1588d`.

**Approval:** R1–R6 of `docs/v2/P36_IMPLEMENTATION_PROPOSAL.md`, covering stages 0–3 only.

> **Update (P3.6-4A, `feature/v2-p364a-image-integration`):** the image *definition* now installs this
> runtime and uses the supervisor as its entrypoint. Still no image has been built or run. The statements below
> describe the state at `41536829` and are kept unchanged; see `docs/v2/P364A_IMPLEMENTATION.md`.

**What has not been done:**
- **No container image was built.** The image definition is still the inert P3.5a image.
- **No real Podman or Docker container was started.** Every runtime test uses a fake `podman` executable.
- **No generated code was executed:** neither the fakes nor the tests run code.
- **Production hardened execution is still refused.** Nothing here establishes isolation: **TB3 isolation is not verified, and the application is not secure.**

## Stage results

### P3.6-0: evidence and documentation (`0c197f24`, `ecead806`)
- **The GitHub Actions run #4 log is preserved as a sanitized copy** in `docs/v2/p3/ci_run4/`. The raw log is not committed.
  - The sanitizer is deterministic (rules R1–R6) and removes:
    - the 8 credential-handling lines;
    - the hosted-runner worker id, region, cloud and provisioner details;
    - runner home and runtime paths;
    - overlay layer ids.
  - Every command, result and measured value is kept.
  - `PROVENANCE.md` records the run, the job, the commit, how it was retrieved, the raw SHA-256 `9bb247aa…` and the sanitized SHA-256 `46d42257…`, together with what the evidence does and does not show.
  - Tests check the checksum, that the raw log is absent, that no credential or identifier pattern remains, and that the sanitizer is deterministic.
- **Stale documents.** Update notes were added to the P3 design (status, §8.1, §8.3, §17), the VE-2 guide, the probe workflow header (comment lines only; a guard enforces that) and two `v2` docstrings. No historical evidence text was changed.
- **R2 (memory limit).** The probe tooling (`rootless.py` `LIMITS`) and `image-expectations.json` now use the 1 GiB in `Limits.memory_bytes`, and a test pins them to `Limits`. Enforcement is not claimed.

### P3.6-1: pure host components (`01b8aca2`); no process is started
- **`v2/execution/podman_argv.py`** builds the exact reviewed `podman run` command line, plus the kill, rm, exists, inspect, version and info commands.
  - Every size comes from `Limits`.
  - Every element passes an allowlist grammar.
  - No task id, dataset path, code, model text or host environment can reach a command line.
  - The flags are requested, not verified as enforced.
- **`v2/execution/host_checks.py`:** `evaluate_host` was moved verbatim from `scripts/p3/rootless.py`, which now imports it. The development driver and production therefore share one rule set (a test asserts they are the same object).
- **`DatasetAuthorizer.authorize_identity_into(identity, workspace)`** stages the dataset by approved name only. Three digests must agree: the pin, the identity digest and the copied bytes' digest, and the size must match. Otherwise the copy is removed and the request refused.
- **`v2/execution/result_mapping.py`** maps a decoded frame to an `ExecutionResult` (timeout, OOM, success or code error).
  - Output text is sanitised.
  - The frame's run id is checked.
  - A chart is considered only for a successful run. It goes through host stage B (`reencode_png`) and `ChartStore` (no overwrite; a name collision fails closed).
- **Dispatcher charts.** The dispatcher accepts a chart only from a runtime with a chart store, at exactly the store-published name for this task and run, as a regular file. The P5 container port accepts that path.

### P3.6-2: trusted in-image source (`e6bfa3f5`); no image build
All new files are in `v2/execution/image/runtime/`.

- **`runner.py`** composes exactly V1's wrapper text, or the P4.1 lazy-seaborn variant, around `/in/code.py`.
  - It has one execution point, inside `main()`.
  - Its argument list is strict.
- **`preludes.json`** holds the trusted prelude text. `scripts/p3/gen_preludes.py` generates it from the host functions (`sandbox.executor.wrap_code_safely`, `sandbox_v2.lazy_prelude`), and it is pinned by SHA-256.
- **Byte-for-byte parity** with the host functions is tested for every golden fixture code and 11 adversarial cases, for both preludes.
- **`supervisor.py`** is the PID-1 control flow of design §8.4, behind an OS facade:
  - signal plan, then `PR_SET_DUMPABLE`, then the attestation, then the child;
  - bounded collectors and the wall deadline;
  - `kill(-1)` and reaping, with **no frame** if any process survives;
  - stage-A chart canonicalisation, then one IPX1 frame.
  - Its argument list may contain only integers and enums; anything else exits 78 with no frame.
  - It is tested through a `FakeOS` that executes nothing. Its frames are decoded by the real host decoder, and weakened attestations are refused.
- **`selftest.py`** holds the fixed OOM and pids self-test children.
- **Deviation from the proposal (deliberate).**
  - The Containerfile, the build context (`CONTEXT_FILES`) and the inert entrypoint are **unchanged**, so the P3.5a image tests and the inertness guard still hold.
  - Switching the image entrypoint to the supervisor, and copying the files (the import closure is pinned by a test), belongs with the image build in the separately approved P3.6-4.

### P3.6-3: `ContainerRuntime` behind a fake Podman (`7dd7dbbb`)
- **`v2/execution/container_runtime.py`** implements `ContainerRuntime(image_id, podman, lease, authorizer, chart_store, limits)`.
  - **`preflight()`** checks:
    - the shared host rules;
    - the pinned image config: id, user 10001, supervisor entrypoint, P3.6 label, no volumes or ports;
    - three trusted self-test containers: the attestation must equal `expected_attestation`, the memory limit must OOM-kill, and the pids limit must refuse.
  - **`execute(request)`** proceeds as follows:
    1. take the semaphore;
    2. create a workspace and write `code.py` verbatim;
    3. stage the dataset by identity;
    4. `podman run`;
    5. decode one bounded frame and verify its attestation;
    6. pass any chart through stage B and the store;
    7. return the `ExecutionResult`.
  - **Always**, on every exit path: kill and remove the container by its exact name, verify that it is gone, remove the workspace and release the semaphore. A host deadline kills the container.
  - Only the resolved, root-owned, non-writable `podman` binary is ever started, with a 4-variable environment.
- **`bootstrap.install_for_validation(runtime, env)`** is the only path to `HARDENED` mode with the real runtime. It requires all of:
  - pytest;
  - `IP_TEST_REQUIRE_CONTAINER=1` in an explicit `env` (the bootstrap never reads `os.environ` itself);
  - euid ≠ 0;
  - exactly `ContainerRuntime` (no subclass or test double);
  - every prerequisite, including preflight.

  `ensure_ready()` (production) still refuses, whatever flags or environment are set (tested).
- **The fake Podman** (`tests/v2/p36/`) records every call and emulates containers as marker files. For `run`, it drives the real supervisor with a literal-only `FakeOS`.
- **P5 end to end** through the real runtime class: 4 datasets, 28/28 golden answers, charts published, no containers or workspaces left, and 0 tripwire hits.
- **The V1 `executor_node` path** reaches the same runtime through the shared dispatcher.

## Exact interface changes

| Interface | Change |
|---|---|
| `DatasetAuthorizer` | `+ authorize_identity_into(identity: DatasetIdentity, workspace) -> AuthorizedDataset` |
| `result_mapping` (new) | `outcome_of(frame)`, `publish_chart(frame, store, task_id, run_id, limits) -> (path, reason)`, `result_from_frame(frame, *, run_id, duration_ms, chart_path=None, chart_rejected_reason=None, frame_run_id=None)` |
| `podman_argv` (new) | `run_argv(podman, image_id, *, instance_id, run_id, inputs_dir, prelude, mode, limits)`, `kill_argv`, `rm_argv`, `exists_argv`, `image_inspect_argv`, `version_argv`, `info_argv`, `validate_argv`, `container_name` |
| `host_checks` (new) | `evaluate_host(facts)` and its constants (moved) |
| `HardenedDispatcher` | charts accepted only at the store-published name from a runtime with `chart_directory`; the V1 tuple carries that path |
| `ContainerExecutionPort` (P5) | accepts the dispatcher's chart path |
| `bootstrap` | `STAGE = "P3.6"`; `+ install_for_validation(runtime, env, options=None)`; production unchanged |
| `ContainerRuntime` (new) | as above; `is_test_double = False`; `chart_directory` |
| Unchanged | `ExecutionRequest`, `ExecutionResult`, `Limits`, frame schema 1, error families, P5 contracts and statuses, `requirements*.txt`, the Containerfile and the image lock |

**Narrow guard updates:**
- the purity registry;
- the P3.4 wiring guard (the dispatcher may import only `chart_filename`);
- the image-directory listing (exact new source files only);
- the `binding.install` caller list (only `+install_for_validation`);
- the P3.5b probe argument expectation (1 GiB);
- the P5.1A lineage list (an explicit per-stage file list, and a guard that the workflow gained only comment lines);
- the subprocess-site allowlist (the supervisor and the runtime).

None was weakened. Each update names the exact new item.

## Validation

| Run | Result |
|---|---|
| Baseline at `05d1588d` | 1608 passed, 13 xfailed |
| Full suite at `7dd7dbbb` (final code; a real Git checkout, env B, offline) | **1769 passed, 13 xfailed, 0 failed, 0 skipped**; 3 warnings (the known Streamlit AppTest restart); tree clean afterwards |
| New tests | 161: 158 in `tests/v2/p36/` (evidence 4, host components 47, image runtime 60, container runtime 47), plus 1 workflow-comment guard and 2 new contract-purity parametrisations |
| Mutation checks | **36/36 detected on the first run** (`docs/v2/p3/p36_mutation_checks.md`) |
| Fake-runtime demonstration | `docs/v2/p3/p36_fake_runtime_demo.md` |

## Remaining security limitations

- **Nothing about isolation is established.**
  - The flags (no network, read-only root, uid 10001, capabilities dropped, no-new-privileges, seccomp default, IPC none, private cgroupns, limits) are **requested** on the command line.
  - The attestation **checks** reported values.
  - Neither proves enforcement. That needs VE-1 (functional) and VE-2 (security), and P3.7 for the resource limits.
- **The supervisor's real OS behaviour has never run.** This covers signals, `PR_SET_DUMPABLE`, `kill(-1)`, reaping, `/proc` and the cgroup files; only its control flow was tested, against a fake.
- **No custom seccomp profile exists** (P3.7).
- **Destructive cleanup of stale instances stays disabled.**
- **Host-side PNG decoding is a residual risk:** zlib and Pillow still process attacker-shaped bytes, within bounds.
- **Gathering host facts duplicates the read-only file probes of `rootless.py`.** Only the evaluation rules are shared.
- **Production remains fail-closed,** and P2, P8 and the deployment unit (Q1/D3) are still open.

## Unresolved design issues (carry into P3.6-4)

1. **Podman 4.9.3 behaviour still to establish:**
   - which `/etc` files are still mounted with `--no-hosts` and `--dns=none`;
   - whether `/dev/mqueue` remains with `--ipc=none`;
   - whether `--log-driver=passthrough` works with piped stdio;
   - whether `--timeout` and the tmpfs `nr_inodes` option are honoured.
2. **The writable mounts the attestation expects are exactly `/out` and `/tmp`.** Any extra writable mount Podman adds makes preflight fail closed, which needs real-runtime confirmation.
3. **PID-1 signal semantics** with a Python init under `crun`.
4. **Self-test thresholds** (the OOM and pids children) on a real cgroup v2 host.
5. **Image Pillow 13.0.0 against environment B's 12.3.0.** Only the hermetic chart references apply, not the P4.1 pixel references.
6. **The image layout for P3.6-4:** the files under `/opt/ip` listed in the import-closure test, the entrypoint switch, the P3.6 label, and new image expectations.
7. **Root-owned `podman`.** The runtime requires the `podman` binary to be root-owned. That holds for distribution packages; other installs would be refused (by design).

## Rollback

- **Remove everything.** Delete `feature/v2-p36-container-runtime`; no other branch was changed.
- **Revert stages individually.** Alternatively, revert stage commits newest first, with no history rewrite:
  1. `7dd7dbbb` (P3.6-3);
  2. `e6bfa3f5` (P3.6-2);
  3. `01b8aca2` (P3.6-1);
  4. `ecead806` and `0c197f24` (P3.6-0).
- **Operationally nothing changes:** production still refuses hardened mode, and `streamlit run ui/app.py` never imports `v2`.

## Proposed P3.6-4 validation plan (NOT approved; not started)

1. **Image (one reviewed commit):**
   - copy the runtime source and the pinned import closure into `/opt/ip`;
   - set `ENTRYPOINT` to the supervisor and the label to `P3.6`;
   - update `image-expectations.json`, the image tests and `CONTEXT_FILES`.
   - The inert entrypoint is retired only in that commit.
2. **VE-1 workflow revision** (separate approval for the workflow change):
   - two clean rootless builds plus a reproducibility comparison;
   - `verify`;
   - the `ContainerRuntime.preflight()` self-tests;
   - the container-marked suite through `install_for_validation`, with golden 28/28 through `ContainerRuntime` for both preludes, 12/12 hermetic charts, the fail-closed cases and the P5 20-case suite;
   - the evidence report preserved as in P3.6-0.
   - This yields **functional evidence only**: the account is root-equivalent.
3. **The vulnerability scan of the image** needs a named scanner and database source.
4. **Then VE-2** (R8, reviewer-operated), for any security claim: the hostile-code ★ tests and the cross-account probes, with 0 skips.
