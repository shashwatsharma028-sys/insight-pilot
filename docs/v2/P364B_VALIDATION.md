# InsightPilot V2 P3.6-4B: real container build and functional validation

**Status: STOPPED at a mandatory gate. The real-runtime preflight refused, and fixing it requires
changing the reviewed isolation flags, which needs your approval.**

| | |
|---|---|
| Branch | `feature/v2-p364b-real-container-validation`, from `5452497fa9a58f45752ec8a79eeda4580c364db6` |
| CI runs | #5 `45121353914` on `7d63b0f7`; #6 `45122041350` on `15a2a5b6`. Sanitised evidence and checksums: `docs/v2/p3/ci_p364b/` |
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
