**Status (after the D3 attempt, run #8): STOPPED at decision D2.** D1 and D3 are implemented and confirmed on the
real runtime. The container now starts: the trusted supervisor runs as PID 1 and returns valid frames, and the
memory and pids self-tests pass. The attestation differs from the required isolation in exactly one field,
`writable_mounts`. See §9.
D1 was approved and implemented (`d7a5dea2`) and is confirmed on the real runtime: the DNS conflict is gone.
Podman 4.9.3 now refuses the reviewed tmpfs inode bound (`nr_inodes`) before any container starts. Fixing that
changes a resource-limit setting, which is explicitly not approved. See §8.

| | |
|---|---|
| Branch | `feature/v2-p364b-real-container-validation`, from `5452497fa9a58f45752ec8a79eeda4580c364db6` |
| CI runs | #5 `45121353914` on `7d63b0f7`; #6 `45122041350` on `15a2a5b6`; #7 `45125846903` on `d1e208cb` (the single D1 attempt). Sanitised evidence and checksums: `docs/v2/p3/ci_p364b/` |
| Runner | GitHub-hosted ubuntu-24.04, kernel `7.0.0-1017-azure`, Podman 4.9.3, crun 1.14.1, cgroup v2 (systemd), rootless, seccomp enabled, AppArmor and SELinux off |
| Production | unchanged: `ensure_ready` still refuses hardened mode, and no deployment change |

## 1. Stage 1: image build (PASSED, both runs)

| Check | Result |
|---|---|
| Approved-content gate (pushed code equals the reviewed digest) | PASS |
| Host gates 1–3 (host facts and the rootless enforcement probe on the pinned base) | PASS |
| Wheel cache (SHA-256 verified) | PASS |
| Build 1 / build 2 (offline, digest-pinned base) | PASS: 37.3 s and 31.1 s |
| **Image ID** | `cf547ba22cac3ab4871dd5344691c6c6dcdb3f58e8f23a784a5185722c4f1d11`, identical for both builds in run #6 and in run #5 (a different runner) |
| **Image digest** | `sha256:82d2f0f12f5c20b7fdf814c57c94374b56f536a779a347b55c0ddfc91298229f`; 589,306,102 bytes (562.0 MiB); 7 RootFS layers |
| Reproducibility (`compare`) | same image ID: True; same RootFS layers: True |
| Build context | SHA-256 `f0f757163f2aa8c864960dd4ca5dd34a86f5494f9754341e2714e071d2c75738` (34 files); lock `9a355dd6…` |
| `verify` | PASS. Covers the config (supervisor entrypoint, stage and runtime-digest labels, user 10001, env names) and exactly the 14 `/opt/ip` files. Each file has its trusted SHA-256 and size, is root-owned, and has mode 0444; subdirectories are 0555. `/opt/ip` itself is root-owned, with no group/other write and no setid/sticky bits; its exact mode is not printed in the log. Also covers no setid, world-writable, secret-like or development files, no pip/compilers/curl, the 18 distributions equal to the lock, the runtime write probes and the no-argument refusal (exit 78, no stdout) |
| Start-up, no-argument refusal (imports the closure, exits 78) | warm n=30 median 187.1 ms, p95 196.6 ms; cold n=10 median 189.0 ms, p95 192.8 ms |
| Golden fixtures in the image (P3.5b path: the image's Python with the dev-script flags, **not** through the supervisor) | 28/28 numbers; 12/12 hermetic charts; 12/12 hermetic render environment; 0 cache warnings |
| OS packages | 105 dpkg packages. Vulnerability scan NOT RUN (no approved scanner) |

## 2. Stage 2: real `ContainerRuntime` (FAILED CLOSED at preflight)

The real-runtime suite reported **2 passed, 3 failed, 12 errors, 2 deselected** in both runs. The 2 deselected are the
P5 tests, which run in their own later steps; those steps were NOT RUN because this step failed.

- **Passed:** `test_01` (host facts give no problems; the image config, entrypoint and labels are accepted by the runtime) and `test_99` (no leftover containers or workspaces).
- **Failed:** `test_02` (attest-only) and both `test_03` self-tests. Every container returned an empty stdout, so the frame was truncated at 0 of 4 bytes.
- **Errors:** the 12 tests that need hardened mode. `install_for_validation` ran the real preflight, which refused with the same protocol error. `BootstrapRefused` was raised, hardened mode was never entered, and there was no fallback.

**Root cause** (run #6 diagnostics, identical for all four container starts):

```
Error: conflicting options: dns and the network mode: none
```

Podman 4.9.3 refuses `--dns=none` together with `--network=none`, both from the reviewed argv
`v2/execution/podman_argv.run_argv`. No container started. The offline tests use a fake Podman that never
modelled this incompatibility; this is exactly the kind of fact that only a real runtime shows.

**What this already demonstrates (functional, not security):** an unusable runtime makes hardened execution refuse.
There is no fallback to the V1 subprocess executor, and no containers or workspaces are left behind.

## 3. Not done, because the gate failed

- **Stage 2 remainder:** the isolation observations from inside, fail-closed outcomes, dataset-integrity checks, chart validation and cleanup paths under the real supervisor.
- **Stage 3:** golden numbers and hermetic charts through the runtime (both preludes), and the P5 20-case suite (both preludes).
- **Stage 4:** runtime preflight and execution latency. Only the start-up numbers above and the host-side `rusage` of the Podman client (max RSS 152,624 KiB, 1.07 s user, 0.93 s system) exist.

No result was substituted or scripted.

## 4. Decisions needed

**D1: the conflicting DNS flag (blocks everything in Stages 2–4).**
- **Option A (recommended):** remove `--dns=none` from `run_argv` and keep `--network=none`, `--no-hosts` and every other flag.
  - With no network namespace interface besides `lo`, DNS configuration is inert.
  - In run #4 (`docs/v2/p3/ci_run4/enforcement_probe.txt`, dev-script flags, `--network=none`, no `--dns`), **no** `/etc/resolv.conf` was mounted.
  - This must be re-observed under the runtime's flags. It also changes the reviewed argv contract: the exact-argv test and mutation A4 ("host name services mounted") must be updated to `--no-hosts` only.
- **Option B:** keep `--dns=none` and drop `--network=none`. **Not recommended:** that removes the network isolation itself.

**D2: anticipated next blocker (not yet observed under the runtime's flags).**
- **The issue:** run #4 showed `/dev/pts` (`rw,nosuid,noexec`) and `/dev/mqueue` (`rw,nosuid,nodev,noexec`) mounted read-write. The runtime's required attestation allows writable mounts `["/out", "/tmp"]` only, so preflight will probably refuse once D1 is resolved.
- **Options:**
  - accept these two kernel pseudo-filesystems explicitly in `expected_attestation`, which is a policy change;
  - remove or mask them with additional Podman options, which is an isolation-flag change;
  - keep refusing and stop.
- **Proposed sequence:** after D1, one CI run that records the real attestation (the suite already does this before verification), then a separate decision on D2 with that evidence.

Neither D1 nor D2 has been implemented. No isolation flag, attestation rule or security assumption was changed.

## 5. Unresolved security limitations (unchanged)

- The runner account is root-equivalent, so all of this is functional evidence, not VE-2.
- No seccomp profile of our own (P3.7); resource limits are not independently validated.
- The child shares uid 10001 with the PID-1 supervisor, and the supervisor decodes PNG files with Pillow.
- The host test environment is Python 3.12 on the runner, against 3.11 in environment B. Its `pip freeze` is in the log; `requirements.txt` is deliberately unpinned (the exam app).

## 6. Rollback

Delete `feature/v2-p364b-real-container-validation`, or revert its commits. No other branch changed. The workflow
runs only on this branch and only when its pinned content digest matches.

## 7. Requirements carried into P3.7 and VE-2

- **P3.7:** a custom seccomp profile; independent validation of the memory, pids and CPU limits; an explicit policy for the `/dev` pseudo-filesystems.
- **VE-2:** a non-root, non-sudo account on a VM you control; the hostile-code and cross-account probes; 0 skips. That is the only basis for any security claim.

## 8. D1 attempt: run #7 (`45125846903`, commit `d1e208cb`, code digest of `d7a5dea2`)

**D1, as implemented:**
- **Argv:** `--dns=none` is removed. `--network=none`, `--no-hosts`, `--http-proxy=false` and every other flag are unchanged.
- **Guard:** `validate_argv` now refuses any `--dns*` flag combined with `--network=none`.
- **Fake Podman:** it reproduces the 4.9.3 refusal.
- **Regression tests:** 10 new tests.
- **Mutation:** 7/7 D1 mutants detected at the pinned commit `a5db2815`. Those that dropped `--network=none`, `--no-hosts` or `--http-proxy=false`, or used `--network=host`, were caught by the exact-argv and fake-runtime preflight checks. A re-added `--dns=none`, a removed guard, and a guard missing `--dns-option` were caught by the 4.9.3 regression and grammar tests.
- **Offline regression at `a5db2815`:** 1861 passed, 13 xfailed, 0 failed, 0 skipped; clean tree.

**Run #7, Stage 1: PASSED again**, on a different cloud: kernel `7.0.0-1014-aws`, where runs #5 and #6 used `7.0.0-1017-azure`.
- **Image:** ID `cf547ba22cac3ab4871dd5344691c6c6dcdb3f58e8f23a784a5185722c4f1d11` and digest
  `sha256:82d2f0f12f5c20b7fdf814c57c94374b56f536a779a347b55c0ddfc91298229f`, identical in all three runs and both builds of each.
  So the build is reproducible across runners and clouds.
- **Builds:** 25.0 s and 20.7 s.
- **Start-up:** warm median 122.2 ms (p95 135.5 ms); cold median 126.8 ms (p95 130.3 ms). This runner was faster than #6
  (187.1 / 189.0 ms). The difference is runner hardware, not a code change, so it is not a performance claim.
- **Golden fixtures in the image:** 28/28 numbers, 12/12 hermetic charts.

**Run #7, Stage 2: FAILED CLOSED at preflight.** The suite reported 2 passed (host and image preconditions; no leftovers), 3 failed
(attest-only and both self-tests: the frame was truncated at 0 of 4 bytes), 12 errors (preflight refused, so hardened mode was
never entered) and 2 deselected (P5, NOT RUN). Diagnostics, identical for all four container starts:

```
Error: unknown mount option "nr_inodes=4096": invalid mount option
```

`run_argv` passes `--tmpfs /tmp:rw,size=67108864,nr_inodes=4096,...` and `--tmpfs /out:rw,size=33554432,nr_inodes=64,...`
(`Limits.tmp_inodes` and `Limits.out_inodes`), and Podman 4.9.3's tmpfs option parser rejects `nr_inodes`.
No container started.

**Not obtained, because preflight never ran a container:** the attestation, the inside network/route/DNS observations,
the D2 writable-mount facts, Stages 3 and 4. Each Podman option error appears only after the previous one is fixed,
so further incompatibilities may follow.

### D3 (decision needed): the tmpfs inode bound

- **Option A:** drop `nr_inodes` from both `--tmpfs` specifications and keep `size`, `mode=1777`, `noexec`, `nosuid` and `nodev`.
  - The kernel default inode count then applies. Inode memory is kernel memory charged to the container's cgroup v2 memory limit,
    which preflight requires to be 1 GiB.
  - `/out` keeps its byte size bound, and the supervisor already counts and ignores every `/out` entry except `chart.png`.
  - `Limits.tmp_inodes` and `out_inodes` would no longer be enforced by Podman. They are a resource limit, so this needs your approval.
  - I would also add an observation of the actual inode counts (statvfs on `/tmp` and `/out`) to the real-container suite.
- **Option B:** keep the inode bound with another mechanism, for example a host-prepared, size- and inode-limited filesystem
  bind-mounted for `/tmp` and `/out`. That changes the mount design and the persistent-storage/sandbox surface, so it is a
  larger change needing review.
- **Option C:** keep refusing. P3.6-4B then cannot proceed on Podman 4.9.3.

**D2** (`/dev/pts`, `/dev/mqueue` mounted read-write, against the attestation that allows only `/out` and `/tmp`) is still
anticipated and still not approved. It can only be observed once a container starts.

## 9. D3 attempt: run #8 (`45341414519`, commit `5868f463`, code digest of `4b7f7244`)

**Fixed before CI, found offline:**
- **Supervisor crash:** `RealOS.install_signal_plan` called `getattr(signal, "SIGRT35")` and would have crashed PID 1.
  The new host smoke test of the safe RealOS methods found it (it never calls `kill(-1)`). Real-time signals are now
  resolved by number; the plan is unchanged.
- **Argument compatibility:** `scripts/p3/argv_compat.py` checks the whole reviewed argv in one pass with Podman
  `create`; nothing is started. Locally it reproduced both earlier CI failures with identical messages.

**D3, as implemented:** `nr_inodes` is removed from `/tmp` and `/out`. **The explicit inode-count limits are no longer
enforced; this is not equivalent protection.** Byte sizes, `mode=1777`, `noexec`, `nosuid`, `nodev`, `--read-only` and
1 GiB of memory are unchanged. `frame.py` still refuses more than `Limits.out_inodes` other `/out` entries (fail closed).
Mutation: 7/7 detected. Offline regression at `908f979a`: 1869 passed, 13 xfailed, 0 failed, 0 skipped.

**Run #8 results:**

| Stage | Result |
|---|---|
| Stage 1 | PASS. New image ID `bb92b2ddb0d2933a4f28d8fb69783565ddc590036b1e2e35bd6a0b3724224ed9`, digest `sha256:cfa84a4d356519f49c5bd2dff3bf4ce2b3c764ddd00e6e60f298fc2f887298d1` (new runtime digest); both builds identical; 28/28 numbers and 12/12 hermetic charts in the image; start-up warm 128.0 ms, cold 125.4 ms (median) |
| Argument compatibility (rootless, create only) | PASS. The full reviewed argv is accepted in all 4 modes; no option rejected alone or in combination; `--passwd` present on `run` |
| Real runtime | **1 failed, 4 passed, 12 errors, 2 deselected** (P5, NOT RUN) |
| passed | host and image preconditions; **memory self-test** (child SIGKILLed, `oom_killed` true, 597 ms); **pids self-test** (child exit 4 at the limit, 189 ms); no leftovers |
| failed | `test_02` attestation (attest-only 182 ms): differs only in `writable_mounts` |
| errors | the 12 tests that need hardened mode: preflight refused (`sandbox attestation does not match the required isolation: writable_mounts`), and there was no fallback |

**Observed attestation** (every field except `writable_mounts` equals the requirement):
uid/gid 10001; CapEff/CapPrm/CapBnd all zero; NoNewPrivs 1; seccomp mode 2; net interfaces `["lo"]`; `/dev` not writable;
memory.max 1073741824; memory.swap.max 0; pids.max 64; cpu.max `100000 100000`.

```
writable_mounts observed: /dev/full /dev/mqueue /dev/null /dev/pts /dev/random /dev/tty /dev/urandom /dev/zero /out /tmp
writable_mounts required: /out /tmp
```

**No other blocker is visible in the log:** no Podman warnings, and no errors apart from this refusal.

### D2 (decision needed): writable `/dev` mount points

**Why they exist:**
- **Device nodes:** a rootless container cannot create device nodes, so Podman/crun **bind-mounts the six standard host
  character devices** (`null`, `zero`, `full`, `random`, `urandom`, `tty`) into the read-only `/dev` tmpfs. Each bind is
  its own mount entry, flagged `rw`.
- **Pseudo-filesystems:** `/dev/pts` (a container-private devpts instance) and `/dev/mqueue` (the mqueue filesystem of the
  container's private IPC namespace) are Podman defaults.
- **The attestation metric:** it lists every `rw` mount point except `/proc` and `/sys`, so it counts device nodes as if they were storage.

**What each one allows:**

| Mount | Storage? | What generated code can do | Needed? |
|---|---|---|---|
| `/dev/null`, `/dev/zero`, `/dev/full` | no | discard writes / read zeros / get ENOSPC | **yes**: the supervisor opens `/dev/null` for the child's stdin |
| `/dev/random`, `/dev/urandom` | no | read randomness; writes are mixed into the host entropy pool without crediting entropy, as any unprivileged host user can | **yes**: Python and NumPy fall back to it |
| `/dev/tty` | no | open fails (ENXIO): no controlling terminal (`podman run` without `-t`, child in a new session) | no |
| `/dev/pts` | no files persist | allocate pseudo-terminals in the container's private devpts instance; no host terminals are visible | no |
| `/dev/mqueue` | small, ephemeral | POSIX message queues in the private IPC namespace, bounded by RLIMIT_MSGQUEUE (default 800 KiB) and kernel limits, destroyed with the namespace; no host or other-container reach | no |

**Options:**
- **Option A (recommended, smallest):** change the required `writable_mounts` from `["/out", "/tmp"]` to the exact sorted
  set observed above (10 entries).
  - Exact equality is kept, so any additional writable mount still fails closed, and so does a missing one.
  - No Podman flag changes.
  - The real-container suite would additionally require, from inside, that these `/dev` entries have the expected filesystem
    types (`devtmpfs` binds, `devpts`, `mqueue`) and that `/out` and `/tmp` are the only writable tmpfs.
  - **Residual risk:** the attestation checks mount-point names, not types, so the runtime check itself cannot tell what is mounted there.
    `/dev/pts` and `/dev/mqueue` remain available to generated code, both private to the container.
- **Option B:** Option A for the six character devices, plus `--security-opt=mask=/dev/pts:/dev/mqueue`. Podman 4.9.3 accepts
  this flag (local create-only check). However, masking stacks a read-only tmpfs over the mount, and `/proc/self/mountinfo`
  still lists the covered `rw` devpts and mqueue entries. The current attestation would most likely still refuse, unless the
  supervisor's mount parsing (trusted image code) changes to consider only the top-most mount. That is more change, and at
  least one more CI cycle to confirm. Recommended for P3.7 instead.
- **Option C (not viable):** masking all devices. `/dev/null` is required by the supervisor itself, and masking a file
  bind-mounts `/dev/null` over it, which still appears as an `rw` mount.

**Expected after Option A:** preflight passes (every other attestation field already matches), then Stages 2–4 run.
P5 needs 78 container executions per strategy, which fits the 20-minute steps. Run-mode-only behaviour (`/in` reads by the
mapped uid, charts) has not been observed yet. It is covered by the existing workspace modes (0755/0644), but it is the
remaining unknown.
