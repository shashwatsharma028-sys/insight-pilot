> **Record status:** P3.6 implementation proposal as delivered for review (2026-11-22), unchanged below. The reviewer approved **R1–R6 only** (stages P3.6-0 to P3.6-3: no image build, no real container, production stays fail-closed). R7 (VE-1 builds) and R8 (VE-2) are not approved. Implementation notes: `docs/v2/P36_IMPLEMENTATION.md`.

# InsightPilot V2: P3.6 production rootless-container runtime, implementation proposal

**Type:** final security-review gate, design only. Nothing was implemented, built, executed, merged or changed.

**Base:** `feature/v2-p51-hardened-integration` at `05d1588d23c0045a177af3e6f9e68d6a6feb9df4`. Its full suite is 1608 passed + 13 xfailed; P5.1A detected 27/27 mutants.

**Inputs reviewed:**
- CLAUDE.md;
- `docs/v2/P3_SECURE_EXECUTION_DESIGN.md` (rev. 2.1, §§1–22);
- `docs/v2/P51_READINESS_REVIEW.md`, `docs/v2/P51A_INTEGRATION.md`;
- the code of `v2/execution/{runtime,dispatch,bootstrap,binding,datasets,workspace,lease,frame,artifacts,chart_store,supervisor_helpers,limits,results,errors}.py`;
- `v2/execution/image/*`;
- `scripts/p3/{rootless.py,build_image.sh,verify_image.sh,image_tool.py,ci_report.py}`;
- the full log of GitHub Actions run #4.

**Headline:** P3.6 can be built as a small extension of existing, tested parts, in five stages. The only new security-critical code is:
- (a) a host `ContainerRuntime` that starts one rootless Podman container per execution;
- (b) a trusted PID-1 supervisor plus runner inside the image.

**Production stays switched off for all of P3.6.** Enabling it is a separate P3.8 decision after VE-2 evidence. Nothing in P3.6 can establish isolation without VE-2 (§8).

---

## 1. What already exists and is reused unchanged

| Need | Existing, tested component | Gap for P3.6 |
|---|---|---|
| Request, result and limits contracts | `ExecutionRequest` (with P5.1A `dataset: DatasetIdentity`, `prelude: PreludeId`), `ExecutionResult`, `Limits` | none |
| One dispatcher for V1 and P5 | `HardenedDispatcher` (scan, size, `identify()`, request, result checks, V1 tuple) | accept a **store-published** chart path (it rejects all charts today) |
| Dataset authorisation | `DatasetAuthorizer.authorize_into(csv_path, workspace)`: descriptor-relative verified copy | a by-identity entry point (no caller path) |
| Workspace and lease | `JobWorkspace` (`runs/<run_id>/in/{code.py,dataset.csv}`), `InstanceLease` (flock) | wired in by the runtime |
| Result frame | `frame.decode_frame` (bounded, strict), `verify_attestation`, `expected_attestation(limits)`, `display_text` | wired in by the runtime |
| In-container pure helpers | `supervisor_helpers` (collectors, signal plan, wait status, attestation parsing, `/out` classification, `assemble_frame`) | used by the new in-image supervisor |
| Charts | `artifacts.reencode_png` (stage B), `ChartStore.publish` (`os.link`, no overwrite) | stage A in the image; wiring |
| Fallback prevention | tripwire on the V1 and P4.1 executors, census, legacy-port refusal, P5 terminating statuses | unchanged |
| Image | digest-pinned base, hash-locked wheels, staged context, inert entrypoint; reproducible (run #4) | supervisor, runner and preludes added; rebuild |
| Host gate | `scripts/p3/rootless.py` `evaluate_host`, enforcement probe, `verify`, `golden` | move the pure checks into `v2/` so production does not import `scripts/` |

## 2. Minimum P3.6 implementation

### 2.1 Host: `v2/execution/container_runtime.py` (new) — `ContainerRuntime`

It implements the existing `ExecutionRuntime` protocol: `name = "rootless-podman"`, `isolation = ROOTLESS_CONTAINER`, `is_test_double = False`.

**Constructor** (all arguments explicit; nothing read from the environment):

```
ContainerRuntime(image_id: str,             # "sha256:<64 hex>", pinned by ID, never a tag
                 lease: InstanceLease, authorizer: DatasetAuthorizer, chart_store: ChartStore,
                 limits: Limits = DEFAULT_LIMITS,
                 podman: str,               # absolute path, resolved once, root-owned, not group/other-writable
                 runner=subprocess)         # injectable for offline tests only (fake podman)
```

**`preflight()`** raises `SandboxUnavailableError`, never falls back. It checks:
1. **Host facts** (the reused `evaluate_host` rules):
   - euid ≠ 0;
   - rootless, not remote;
   - cgroup v2 with the systemd manager and controllers ⊇ {cpu, memory, pids};
   - seccomp enabled;
   - subordinate uid/gid ranges;
   - Podman ≥ 4.4;
   - native architecture.
2. **The image:** it exists by ID, and its config matches `image-expectations.json` (user 10001, entrypoint = supervisor, labels including the lock SHA-256, no volumes or ports).
3. **A self-test container** (trusted supervisor modes only, no generated code):
   - `--attest-only` produces a frame whose attestation must equal `expected_attestation(limits)`;
   - `--selftest-oom` must report `oom_killed`;
   - `--selftest-pids` must hit `pids.max`.

**`execute(request)`:**

1. **Concurrency.** Acquire a `BoundedSemaphore(limits.max_concurrent)` within `queue_wait_seconds`; otherwise raise `SandboxUnavailableError("busy")`.
2. **Workspace.** `ws = JobWorkspace.create(lease, run_id=request.run_id)`, then `ws.write_code(request.code)` (verbatim).
3. **Stage the dataset.** `authorizer.authorize_identity_into(request.dataset, ws)` (new; §4). It copies by the approved name from the trusted directory. **The copied bytes' SHA-256 must equal both the pin and `request.dataset.sha256`, and the size must match.** The copy is the third digest check after profiling (P5) and `identify()` (dispatcher).
4. **Start the container.** `argv = podman_argv.run_argv(...)` (§3). Start it with `Popen(argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, env={PATH, HOME, XDG_RUNTIME_DIR, LANG}, start_new_session=True, close_fds=True)`.
5. **Read the frame.**
   - Read stdout as **one IPX1 frame**: `decode_frame(stream, run_id, limits)` reads at most `8 + max_frame_bytes + 1` bytes, with a host deadline of `limits.host_deadline_seconds` (45 s).
   - Drain CLI stderr in a thread, capped at 64 KiB. It is diagnostics only, never shown to users.
6. **Verify isolation.** `verify_attestation(frame.attestation, expected_attestation(limits))`. A mismatch raises `SandboxUnavailableError` and **discards the result**.
7. **Map the result** (§4.3) to an `ExecutionResult`.
8. **Handle the chart.**
   - chart bytes → `reencode_png` → `chart_store.publish(chart, task_id, run_id)`, which sets `chart_path`;
   - `ArtifactRejectedError` sets `chart_rejected_reason`;
   - `ChartPublishedWithCleanupError` keeps the published path and logs the warning.
9. **`finally`, always** (success, code error, timeout, protocol error, host exception, `KeyboardInterrupt` or any other `BaseException`):
   1. `podman kill -s KILL <exact name>`, if the container is still running;
   2. `podman rm -f -t 0 <exact name>`;
   3. `podman container exists <name>` must be false. A container still present raises `SandboxUnavailableError` after the result is discarded.
   4. `ws.cleanup()`: any failure raises, and cleanup is retryable;
   5. release the semaphore.

**Self-limits independent of the host process:**
- `--rm` and `--timeout=<wall + grace>`, so a crashed app leaves no long-lived container;
- names `ip-<inst8>-<run12>`;
- labels `org.insightpilot.instance=<id>`, `org.insightpilot.run=<run_id>`, `org.insightpilot.schema=1`.

**Destructive stale-instance reconciliation stays disabled** (as P3.3). The runtime only *reports* labelled containers belonging to other instances.

### 2.2 Host: `v2/execution/podman_argv.py` (new, pure)

- **The argv builder.** It builds exactly the §8.1 command (§3 below) from `Limits`, `image_id`, `run_id`, `instance_id`, the workspace `in/` path and the `PreludeId`. It is a list, never a shell string.
- **Validation of every element:**
  - the run id is 32 hex characters and the instance id has the lease format;
  - the image id matches `^sha256:[0-9a-f]{64}$`;
  - the `in/` path matches `^[A-Za-z0-9/._-]+$` and lies under the lease root, otherwise the build is refused (prevents `--mount` option injection);
  - only integers come from `Limits`;
  - the prelude is from the enum.
- **No caller-supplied string ever reaches the argv.**
  - The task id never appears: the container name is derived from the run id.
  - The dataset path never appears: the mount source is the workspace `in/`, which holds the copied `dataset.csv`.

### 2.3 Host: `v2/execution/host_checks.py` (new, pure)

- The `gather_facts`/`evaluate_host` logic moves here from `scripts/p3/rootless.py` without change. `rootless.py` then imports it, so there is one implementation.
- Production code never imports `scripts/`.

### 2.4 Image: trusted supervisor and runner (new files in `v2/execution/image/`)

**`supervisor.py`: PID 1, runs as uid 10001, standard library only.** It follows design §8.4 exactly:
1. Install `signal_plan()`: every catchable signal `SIG_DFL` except `SIGCHLD` (handled) and `SIGPIPE` (ignored). Set `prctl(PR_SET_DUMPABLE, 0)`.
2. Build the attestation from `/proc/self/status`, `/proc/self/mountinfo`, `/sys/class/net` and the cgroup files, using the existing parsers.
3. Spawn `python3 -I -B /opt/ip/runner.py --prelude <id>`:
   - stdin is `/dev/null`;
   - stdout and stderr go to separate pipes;
   - `close_fds`, and a new session;
   - a minimal environment: `MPLBACKEND=Agg`, `MPLCONFIGDIR`, `HOME`, `LANG=C.UTF-8`, `TZ=UTC`, `OPENBLAS_NUM_THREADS=1`, `OMP_NUM_THREADS=1`.
4. Drain the pipes into `BoundedCollector(stdout_bytes)` and `HeadTailCollector(stderr_head, stderr_tail)`, with a wall deadline of `--wall` (30 s).
5. On child exit or the deadline:
   - `kill(-1, SIGKILL)`;
   - reap until `ECHILD`;
   - confirm `/proc` lists only PID 1;
   - read the cgroup `oom_kill` count from `memory.events`.
6. **Stage A:** run `classify_out_entry` on `/out/chart.png` (lstat, `O_NOFOLLOW`, fstat equality). Pillow is restricted to PNG with `MAX_IMAGE_PIXELS`, then `verify`, reopen and `load`. It converts to RGB/RGBA and re-encodes with no metadata. Other `/out` entries are reported as a count only.
7. `assemble_frame(...)` writes **one frame** to fd 1, then the supervisor exits 0.
8. Its argv is integers and enums only, checked against a grammar. Unknown arguments exit 78 with no frame, which the host treats as a protocol error.

**`runner.py`:** builds the **exact V1 wrapper text** for the container paths (`/in/dataset.csv`, `CHART_PATH="/out/chart.png"`) around `/in/code.py`, then `exec(compile(text, "<generated>", "exec"))`.
- The prelude text is **trusted image content**: `preludes/v1.py` and `preludes/lazy_seaborn.py`, generated at image-build time from `sandbox.executor.wrap_code_safely` and `sandbox_v2.lazy_prelude.derive_lazy_wrapper_text`.
- **Parity test:** for both preludes, image prelude + code + epilogue equals the host derivation byte for byte. This preserves V1 semantics and P4.1 lazy-seaborn behaviour.
- The image pins `seaborn==0.13.2`, the same as environment B, so the `cm.py` data pre-registered by the lazy prelude are identical.
- The generated code is never modified and never placed on the argv.

**Why a Python PID 1** (design Q4, option a): no capability is needed, and init semantics protect the supervisor from same-namespace signals. Whether that holds **requires runtime validation** (I-10, I-11, I-14).

### 2.5 Dispatcher and P5 changes (small)

- **`dispatch.HardenedDispatcher._check_result`:** accept `chart_path` only if it equals `chart_store.directory + chart_filename(task_id, run_id)`, is a regular file with link count ≤ 2 (the store's temporary hard link may remain), and is inside the store directory. Everything else stays a protocol error. The V1 tuple is unchanged.
- **`multi_agent.execution.ContainerExecutionPort`:** accept the published chart path (it raises today). It validates the same rule, then sets `ExecutionOutcome.chart_path`. P5 contracts, verification and approvals are unchanged.

### 2.6 Bootstrap: still no production path in P3.6

- `ensure_ready()` keeps refusing hardened mode (`STAGE = "P3.6"`, "no validated container backend").
- **New `install_for_validation(runtime: ContainerRuntime)`**, the only way to reach `HARDENED` with the real runtime. It requires:
  - `"pytest" in sys.modules`;
  - `IP_TEST_REQUIRE_CONTAINER=1`;
  - euid ≠ 0;
  - `runtime.preflight()` passing.

  It exists only so VE-1 and VE-2 suites can exercise the real backend.
- Enabling production is a **P3.8 decision** after P3.7 and VE-2 evidence (and the Q1 deployment-unit decision).

## 3. Isolation requirements

Every row uses the same run flags. **VE-2** means proof requires the unprivileged VE-2 host.

| Control | Mechanism (run argv unless stated) | Attested per run | P3.6 implements | Proof |
|---|---|---|---|---|
| Rootless, non-root | rootless Podman; host euid ≠ 0 (preflight); `--user 10001:10001`; image `USER 10001` | uid/gid | yes | VE-2 (O-01…04, cross-account) |
| User namespace | rootless default mapping (`uid_map 0→host user, 1→subuid range`; run #4 showed `0 1001 1 / 1 165536 65536`) | (via uid) | yes | VE-2 |
| Mount, PID, IPC and UTS namespaces | runtime default + `--ipc=none` + `--cgroupns=private` | writable mounts; `/dev/shm` absent | yes | VE-2 (S-03) |
| No network | `--network=none --dns=none --no-hosts --http-proxy=false` | interfaces = {`lo`}; no routes | yes | VE-2 (I-06: host loopback, abstract socket, DNS, 169.254.169.254) |
| Capabilities | `--cap-drop=ALL` | CapEff/CapPrm/CapBnd = 0 | yes | VE-2 (I-07, I-13) |
| No privilege gain | `--security-opt=no-new-privileges`; setuid bits removed in the image | NoNewPrivs = 1 | yes | VE-2 |
| Seccomp | runtime default profile (`Seccomp: 2`) | Seccomp = 2 | default profile: yes. **A custom profile is P3.7** | VE-2 (keyctl, unshare, mount denied) |
| Read-only filesystem | `--read-only --read-only-tmpfs=false --image-volume=ignore`; inputs `--mount type=bind,…,readonly` | writable mounts = {`/out`, `/tmp`}; `/dev` not writable | yes | VE-2 (S-03; run #4 write probes showed EROFS on `/`, `/etc`, `/usr`, `/run`, `/var/tmp`, `/dev`, but in the inert image) |
| Bounded writable storage | `--tmpfs /tmp:size=64m,nr_inodes=4096,…` `--tmpfs /out:size=32m,nr_inodes=64,…` (`noexec,nosuid,nodev`) | — | flags: yes | **P3.7** (S-01/02/04: byte, inode and cgroup charging proven) |
| Memory | `--memory=1g --memory-swap=1g` (swap 0) | `memory.max`, `memory.swap.max` | flags + attestation: yes | **P3.7** (R-01, S-04) |
| CPU, PIDs, files, core | `--cpus=1 --pids-limit=64 --ulimit nofile=256:256 --ulimit core=0` | `cpu.max`, `pids.max` | flags + attestation: yes | **P3.7** (R-02…06, fork bomb, nofile) |
| Time | supervisor wall 30 s; host deadline 45 s; `--timeout=45` | `timed_out` | yes | VE-2 (R-03, I-14) |
| Logs and host storage | `--log-driver=passthrough`; no host output directory | — | yes | **P3.7** (S-05: host growth < 1 MiB) |
| No secrets or environment | CLI env = {PATH, HOME, XDG_RUNTIME_DIR, LANG}; `--env-host=false`; only allowlisted `--env` | — | yes | VE-2 (I-01, with a dummy `GOOGLE_API_KEY` and proxy variables) |
| No host filesystem | only `in/` mounted read-only; no other bind mount | mountinfo allowlist | yes | VE-2 (I-03/04/05) |
| PID-1 integrity | signal plan; `PR_SET_DUMPABLE=0`; `kill(-1)` before the frame | — | yes | VE-2 (I-10, I-11, I-14) |
| No legacy fallback | tripwire; census; terminating errors; production refusal | — | already done (P5.1A) | C (offline) |

**Explicitly gated to P3.7 (not claimed in P3.6):**
- enforcement proof of every limit (R-, S-tests);
- a custom seccomp profile;
- the Docker-rootless adapter;
- destructive stale reconciliation;
- arm64;
- optional gVisor.

## 4. Exact interface changes

### 4.1 New entry point (`datasets.py`)

```
DatasetAuthorizer.authorize_identity_into(identity: DatasetIdentity, workspace: JobWorkspace) -> AuthorizedDataset
```
- `identity.name` must be an approved name.
- The source is `<trusted data dir>/<name>`, opened by name relative to the directory descriptor. There is no caller path.
- It reuses `_open_source` and the `authorize_into` copy loop.
- It refuses (`DatasetNotAuthorizedError`) unless copied digest == pin == `identity.sha256` and size == `identity.size`.

### 4.2 Dispatcher

- `_check_result` accepts a store-published `chart_path` (§2.5).
- `HardenedDispatcher(runtime, limits, authorizer, chart_store=None)`: `chart_store` is used only for that path check.

### 4.3 Frame → `ExecutionResult` mapping (`container_runtime.py`)

| Frame status | `outcome` | stdout / stderr |
|---|---|---|
| `timed_out` | `TIMED_OUT` | sections via `display_text`, with truncation flags |
| `oom_killed` | `MEMORY_LIMIT` | as above |
| `child_exit == 0` | `SUCCEEDED` | as above; chart handled per §2.1 step 8 |
| `child_exit != 0` or `child_signal` | `CODE_ERROR` | as above |

- Any decoder or attestation failure raises (`SandboxProtocolError` or `SandboxUnavailableError`). Through the existing P5.1A path, the run ends with `isolation_unavailable`, with no retry.
- `duration_ms` is the host-measured time.

### 4.4 P5

- `ContainerExecutionPort` accepts the published chart path (§2.5). Nothing else in P5 changes.

### 4.5 Bootstrap

- `install_for_validation(runtime)` is new (§2.6).
- `STAGE = "P3.6"`.
- Production behaviour is unchanged (it refuses).

### 4.6 No other contract changes

`ExecutionRequest`, `ExecutionResult`, `Limits`, frame schema 1, the error families, P5 envelopes and P5 statuses all stay as they are.

## 5. Affected files

| Stage | Files |
|---|---|
| P3.6-0 | `docs/v2/p3/ci_run4/` (new: full log, extracted report, SHA-256 manifest); `docs/v2/P3_SECURE_EXECUTION_DESIGN.md`, `docs/v2/P3_VALIDATION_ENVIRONMENT.md`, `.github/workflows/p3-ubuntu-rootless-probe.yml` (comment header only); `v2/__init__.py`, `v2/execution/results.py`, `v2/execution/runtime.py` (stale docstrings only) |
| P3.6-1 | new: `v2/execution/podman_argv.py`, `v2/execution/host_checks.py`; changed: `v2/execution/datasets.py` (`authorize_identity_into`), `v2/execution/dispatch.py` (chart path), `multi_agent/execution.py` (chart path), `scripts/p3/rootless.py` (imports `host_checks`); tests `tests/v2/p36/` (new); purity and reviewed-module lists in `tests/v2/p3/test_contract_purity.py` and the P5.1A subprocess-site allowlist (narrow additions) |
| P3.6-2 | new: `v2/execution/image/supervisor.py`, `runner.py`, `preludes/` (generated, committed, hash-pinned); changed: `Containerfile` (COPY supervisor, runner, preludes and the pure helpers `frame.py`/`supervisor_helpers.py`/`limits.py`/`errors.py` as `/opt/ip/ipx/`; `ENTRYPOINT` → the supervisor; label stage `P3.6`), `image-expectations.json`, `scripts/p3/image_tool.py` (context allowlist), `scripts/p3/image_inspect.py`; image-test updates in `tests/v2/p3/test_image_p35a.py` |
| P3.6-3 | new: `v2/execution/container_runtime.py`; changed: `v2/execution/bootstrap.py` (`install_for_validation`, `STAGE`); tests with a fake `podman` executable (trusted test double; emits scripted frames, runs no generated code) |
| P3.6-4 | the VE-1 workflow: a new job or a revision of the probe workflow (separate approval) running the container-marked suite against the rebuilt image |

**Untouched:**
- `agent/`, `sandbox/`, `utils/`, `ui/`, `rules/`, `skills/`, `data/`, `lessons/`;
- `requirements.txt` and `requirements-v2.txt`;
- `sandbox_v2/`;
- `docs/v2/p4/`, `docs/v2/p5/`;
- P5 contracts and verifier;
- every existing branch head.

## 6. Dependencies and image

- **Host Python dependencies:** none. The runtime calls the `podman` CLI, which is a host system package (Ubuntu 24.04: Podman 4.9.3, as in run #4), with `crun`, `uidmap` and `passt` per VE-2.
- **Image Python dependencies:** none new. The supervisor and runner use the standard library plus the already-locked Pillow, pandas, numpy, matplotlib and seaborn, so `requirements.lock` is unchanged.
- **Image contents** change (new files, the entrypoint and labels). That means **a new image ID**, which has to be re-established by:
  - two clean builds (reproducibility);
  - `verify`;
  - golden 28/28 and charts 12/12 in the image;
  - the OS package inventory.
- **A vulnerability scan is still required and unapproved** (scanner and database source to be decided).

## 7. Evidence reconciliation and stale documents

### 7.1 Discrepancies between implementation, tooling and evidence (resolve in P3.6-1/2)

| # | Item | Today | Proposal |
|---|---|---|---|
| E1 | Memory limit | `Limits.memory_bytes` = 1 GiB (and `expected_attestation`), but probe `LIMITS["memory"]="256m"` and `image-expectations.json` `memory.max=268435456`; golden 28/28 ran under 256 MiB | single source: argv and expectations derived from `Limits` (decision R2) |
| E2 | `/etc/hosts`, `/etc/resolv.conf`, `/etc/hostname` mounted in the probe | no `--no-hosts`/`--dns=none` in the probe | add both; the mount allowlist then shrinks. Behaviour with `--network=none` **requires runtime validation** |
| E3 | `/dev/mqueue` in the probe's allowed mountpoints | design: `--ipc=none` removes IPC mounts | verify on VE-1 and VE-2; keep only what the runtime proves necessary |
| E4 | `MPLCONFIGDIR` / `HOME` | image `MPLCONFIGDIR=/tmp/matplotlib`, `HOME=/nonexistent`; design §8.1 `HOME=/tmp`, `MPLCONFIGDIR=/opt/ip/mpl` | keep the image values (run #4: 0 Matplotlib cache warnings, hermetic render environment matched); fix design §8.1 |
| E5 | `/out` tmpfs size | probe 64 MiB; design and `Limits.out_bytes` 32 MiB | derive from `Limits` |
| E6 | Workspace path | design §8.1 `<instance>/jobs/<run>/in`; code `instances/<id>/runs/<run_id>/in` | fix the design |
| E7 | Golden-in-image method | run #4 piped a V1-wrapped script on stdin to `python -I -B -` with the dataset bind-mounted directly | P3.6 path: workspace copy + supervisor + runner; golden must be re-run through `ContainerRuntime` (the run #4 result does not transfer) |
| E8 | Inert image ID `0ffd44b3…` | evidence for the P3.5a inert image only | P3.6 image is new; reproducibility re-established |

### 7.2 Stale documents (no evidence value; fix in P3.6-0)

- **The P3 design status and §17 row P3.5b.** They say "no image has been built"; true only for VE-2. Run #4 built and verified the inert image on VE-1.
- **`P3_VALIDATION_ENVIRONMENT.md`.** It says "VE-1 … not covered here".
- **The workflow header.** It lists runs 1–3 only; run #4 passed.
- **Docstrings:**
  - `v2/__init__.py` says there is "no launcher, no executor";
  - `results.py` says "nothing builds one yet";
  - `runtime.py` still has a pre-P3.3 comment.
- **Design §8.1 and §10 original plan text** (E4, E6; `os.replace`/`run8`). This is already marked as superseded; keep it, but mark it more clearly.

### 7.3 CI run #4 evidence: captured now, not yet committed

- **The record:** run id 45033387515, job 137241676343, commit `4cd107e0`, 2026-11-22, conclusion success.
- **The capture:**
  - full job log, 1,926 lines;
  - SHA-256 `9bb247aa9bbcf006246b263d316d116f24a7ee24accf82394846f8c850140c3b`;
  - saved in the session scratchpad: `ci_run4/job_137241676343.log`.
- **Secret check:** the only token appears masked (`***`); no other secret patterns.
- **Why it is needed:** the run uploaded **no artifacts**, so this log is the only record. GitHub's default log retention is 90 days. The repository's actual setting was not verified, so the logs should be assumed to be at risk from about 2027-02-20.
- **Enforced values recorded in the log, inert image:**
  - kernel 7.0.0-1014-aws; Podman 4.9.3;
  - uid 10001;
  - CapEff, CapPrm, CapBnd and CapAmb all 0;
  - NoNewPrivs 1, Seccomp 2;
  - interfaces {`lo`}, no routes;
  - EROFS on `/`, `/etc`, `/usr`, `/run`, `/var/tmp`, `/dev`;
  - `memory.max` 268435456.
- **Its limits:** the runner is root-equivalent; no vulnerability scan was run; 8 skips.
- **Proposed:** commit it in P3.6-0 (decision R1). That is a docs-only commit on a new P3.6 branch.

## 8. Guarantees that cannot be established without VE-2

None of the following may be claimed from mock tests, the fake-podman tests or GitHub-hosted CI (VE-1 runs as a root-equivalent account with passwordless sudo and the docker group):

1. **TB3 containment of hostile code:** the bypass corpus with the scanner off, filesystem read and write, secrets, `/proc`, network, IPC.
2. **Rootless user-namespace effects for an unprivileged account,** and the protection of job files against a second host account (O-01…04, the cross-account probes).
3. **PID-1 signal semantics,** `kill(-1)` completeness, ptrace and `/proc/1/*` protection (I-10, I-11, I-14).
4. **Resource-limit enforcement and tmpfs charging** (R-, S-tests; P3.7).
5. **Cleanup after a host crash and orphan handling** under real systemd user sessions (C-06…08).
6. **No host log or disk growth** (S-05).
7. **The image's OS-package vulnerability status.**

Even with VE-2, P3 covers TB3 only: P2 and P8 remain. The handoff wording is fixed: **"TB3 isolation: verified on VE-2 (evidence X) / not verified; application not secure."**

## 9. Acceptance tests

**Environments:**
- **C:** offline, no runtime;
- **F:** fake `podman` executable (test double) emitting scripted frames;
- **VE-1:** GitHub ubuntu-24.04; functional only;
- **VE-2:** reviewer-controlled unprivileged VM. 0 skips: `IP_TEST_REQUIRE_CONTAINER=1` turns a missing runtime into a failure.

**★** means the AST scanner is bypassed, so the container alone must contain the code.

| Area | Tests | Env |
|---|---|---|
| Mock-only contracts | argv grammar (every element; refusal of unsafe paths, ids and images; no task id or dataset path in argv); `authorize_identity_into` (digest = pin = identity; size; refusals mirror DS-xx); frame → result mapping for every status; attestation mismatch → `SandboxUnavailableError`; oversized, forged or trailing frames → `SandboxProtocolError`; chart flow (stage-B reject → reason; publish → path; cleanup error → path kept); `finally` cleanup on every exit path including `KeyboardInterrupt`; semaphore and queue timeout; container still present after removal → error; `install_for_validation` refusals (no pytest, root, no flag, failing preflight); production `ensure_ready` still refuses | C/F |
| Integration (mock) | P5 end to end through `ContainerRuntime` with fake podman: 4 datasets, 28/28, review-only on termination, approval before the first `podman run`; V1 graph path L-04 through the real runtime class with fake podman | F |
| Trusted golden workload | through `ContainerRuntime` and the new image: 28/28 golden answers (golden tolerance) for both preludes (`v1`, `lazy_seaborn`); V1 snapshot behaviour equal (G-03/04); P5 20-case suite identical TP/TN/FP/FN | VE-1 (functional), VE-2 |
| Charts and pixels | 7 charts produced; 12/12 tasks match `chart_hermetic` decoded-pixel SHA-256 after stage A + B (lossless re-encode); render environment equal to the committed hermetic environment; A-01…21 corpus; raw Matplotlib PNG at stage B rejected (A-21) | C (corpus), VE-1, VE-2 |
| Prelude parity | image prelude text == host derivation for both preludes; P4.1 colormaps registered without importing seaborn; the generated code byte-identical in `code.py` | C, VE-1 |
| Fail closed | preflight failures (root, cgroup v1, missing controller, remote or rootful Podman, wrong image ID or config) → refuse; no frame, truncated frame or second frame → protocol error → `isolation_unavailable`; host deadline → KILL + remove + `TIMED_OUT` | F, VE-1, VE-2 |
| No legacy fallback | all P5.1A routing tests; plus `subprocess.run`/`Popen` spied: the only process started is `podman` (argv[0] = the resolved absolute path); tripwire hits 0; mutation checks on every new guard | C/F |
| Hostile code ★ | the P0 bypass corpus (5 shapes); I-01 environment; I-03/04/05 files, repository, `/proc/*/environ`; I-06 network; I-07/08 capabilities, seccomp, mountinfo; I-10/11/13/14 signals, ptrace, daemons, keyctl/unshare/mount; frame forgery from inside (the child writes fake IPX1 to stdout → appears only as stdout text) | **VE-2** |
| Cleanup and resources | no container left after success, error, timeout, host SIGKILL of the app (`--rm`/`--timeout`), cancellation; the workspace removed; R-01…06 ★, S-01…05 ★ (P3.7) | VE-2 |
| Independent VE-2 | the full guide (`P3_VALIDATION_ENVIRONMENT.md`) as `ipval`; the full suite as `ipval`; cross-account probes as `ipprobe`; evidence bundle with hashes; vulnerability review | **VE-2** |
| Regression | the existing **1608 passed + 13 xfailed** unchanged (plus the new tests); the 13 strict xfails stay xfailed (the V1 path is untouched); golden snapshots byte-identical; V1 paths byte-identical to `fd5783c6`; P4.1 and P5 evidence unchanged | C, VE-2 |

## 10. Implementation stages

Each stage is small, separately reviewed, and stops for approval.

| Stage | Scope | Runs code? | Risk | Rollback |
|---|---|---|---|---|
| **P3.6-0** | branch `feature/v2-p36-container-runtime` from `05d1588d`; commit the CI run #4 evidence and fix the stale docs (§7.2) | no | Low | delete the branch |
| **P3.6-1** | pure host pieces: `podman_argv`, `host_checks` (moved), `authorize_identity_into`, frame → result mapping, chart-path acceptance (dispatcher + P5), tests with fakes; production unchanged | no (no process at all) | High (security code paths) | `git revert` the stage commits |
| **P3.6-2** | in-image supervisor, runner and preludes + Containerfile/expectations/tooling; host-side unit tests of the pure parts only; **no image build** | no | High | revert; the inert image definition is restored |
| **P3.6-3** | `ContainerRuntime` + `install_for_validation`; offline tests with a fake `podman` executable (trusted test double); production still refuses | only the fake podman (trusted test code) | High | revert; bootstrap returns to P5.1A behaviour |
| **P3.6-4** | **separate approval:** VE-1 workflow revision; rootless image build ×2, verify, golden through the runtime, charts, fail-closed tests. Functional evidence only | trusted fixtures in containers | High | revert the workflow commit; `podman rmi` on the runner is ephemeral |
| **P3.6-5** | **separate approval:** VE-2 run by the reviewer (§9) | trusted fixtures + hostile-code corpus in containers on the reviewer's VM | High | `podman system reset --force` as `ipval`; delete the VM |
| P3.7 / P3.8 | limit-enforcement proofs, custom seccomp; then the production-enablement decision | — | High | — |

## 11. Change-approval proposal (`agent/change_approval.py`)

- **Title:** P3.6 production rootless-container runtime (stages P3.6-0 … P3.6-3; P3.6-4/5 approved separately).
- **Branch:** new `feature/v2-p36-container-runtime`, created from `feature/v2-p51-hardened-integration` at `05d1588d23c0045a177af3e6f9e68d6a6feb9df4`. Push only there. No merge, and no change to any existing ref.
- **Affected files:** §5.
- **Type and risk:** the classifier gives core_feature / **Medium** for these paths, because `v2/` is not in its table; architecture / High when described as a new runtime. CLAUDE.md gate 2 (sandbox permissions and isolation) applies, so the **effective classification is Architecture / High. Approval is required for every stage.**
- **Expected impact:**
  - a real hardened backend becomes reachable **only** from container-marked validation tests;
  - production stays fail-closed;
  - V1, the exam build, P4.1 and P5 behaviour are unchanged;
  - no new host dependency;
  - a new image ID.

**Remaining uncertainties** (each must be settled by VE-1 or VE-2 evidence, not argued):
1. `--dns=none` / `--no-hosts` with `--network=none` on Podman 4.9.3: which `/etc` files are still mounted.
2. `--log-driver=passthrough` with piped stdio; `--timeout` behaviour; `nr_inodes` pass-through.
3. Whether `/dev/mqueue` persists with `--ipc=none`.
4. PID-1 signal semantics with a Python init under crun.
5. The Ubuntu 24.04 AppArmor setting `kernel.apparmor_restrict_unprivileged_userns`. Podman's profile allowed it on VE-1; it must be re-checked on VE-2.
6. cgroup v2 delegation of `cpu` for an unprivileged user on VE-2.
7. Pillow 13.0.0 (image) against 12.3.0 (env B): only hermetic references are used, so the P4.1 pixel references do not apply.
8. Real-model code may need memory beyond 256 MiB. That is why E1 matters; 1 GiB is in the design.
9. The deployment unit (Q1/D3) is unresolved, so no production enablement either way.

## 12. Decisions requested

- **R1:** approve P3.6-0: create `feature/v2-p36-container-runtime` from `05d1588d`, commit the CI run #4 evidence (full log + report + SHA-256), and fix the stale docs.
- **R2:** the memory limit. Make `Limits` the single source with 1 GiB (recommended; the attestation already expects it), or adopt 256 MiB everywhere.
- **R3:** approve the architecture: one `ContainerRuntime` per process, one container per execution, a Python PID-1 supervisor (Q4 option a), a trusted runner with image-generated preludes, and staging by identity with triple digest agreement.
- **R4:** approve the runtime flag set in §3, including the additions `--dns=none`, `--no-hosts`, `--cgroupns=private`, `--log-driver=passthrough`, `--timeout`, `--ulimit nofile/core`, `nr_inodes`, `--image-volume=ignore` and labels. Confirm that the default seccomp profile is acceptable for P3.6, with the custom profile in P3.7.
- **R5:** approve `install_for_validation()` as the only path to the real backend in P3.6. Production enablement is reserved for P3.8.
- **R6:** approve stages P3.6-1 … P3.6-3 (no image build, no container run).
- **R7:** separately and later: approve P3.6-4 (the VE-1 workflow revision and image builds) and name the vulnerability scanner and database source.
- **R8:** separately and later: confirm VE-2 (who runs it, and the VM spec per the guide). Any isolation claim depends on it.

**Stopped here.** No implementation, build, container run, VM, dependency change, merge or live-model test has been started.
