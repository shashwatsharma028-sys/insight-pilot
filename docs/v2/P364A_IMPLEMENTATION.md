# InsightPilot V2 P3.6-4A: trusted container image integration (offline only)

**Status:** implemented and tested offline on `feature/v2-p364a-image-integration`. The base is
`feature/v2-p36-container-runtime` at `41536829957c27a053775989aa619d10cd114406`.

**No image was built and no container runtime was invoked.** Podman, Docker and Buildah were not
run. No generated code was executed, and production hardened execution still refuses. Nothing in this
stage shows that the image builds, that the supervisor behaves correctly in a real container, or
that any isolation property is enforced. Those questions belong to P3.6-4B and VE-2.

## 1. What changed

| File | Change |
|---|---|
| `v2/execution/image_layout.py` (new) | The exact `/opt/ip` file list (`RUNTIME_FILES`, 14 files), the entrypoint, the stage and runtime label names, layout validation, a symlink- and escape-safe source reader, and the runtime-tree digest |
| `v2/execution/image/Containerfile` | The build stage copies `opt-ip/` and normalises its modes (files 0444, directories 0555). The final stage copies it to `/opt/ip`. The entrypoint is the supervisor; the stage label is `P3.6`; the new label `io.insightpilot.runtime.sha256` is added. The base, lock, environment, user and pip steps are unchanged |
| `v2/execution/image/image-expectations.json` | Updated for the supervisor entrypoint and the 14 `opt_ip_files`. It adds the runtime label, `stage_label`, and `no_args_exit_status` / `no_args_message`, which replace the inert keys. The new runtime files are added to the write probes. **The runtime-isolation section is unchanged** |
| `scripts/p3/image_tool.py` | `CONTEXT_FILES` drops `inert_entrypoint.py`. `stage()` copies each runtime file by its exact path (`source_root`; no directory walks; exclusive create). New `staged_runtime_digest()` |
| `scripts/p3/rootless.py` | Image tag `p3.6-…`. `check_image_config` checks the stage and runtime labels by value. `build` refuses unless the Containerfile label equals the digest of the **staged** runtime. `check_inert` is replaced by `check_no_args_refusal` (used by `verify` and `measure-startup`) |
| `scripts/p3/image_inspect.py` | The P3.6 runtime files are added to the file write probes; the P3.5a probe is kept |
| `v2/execution/container_runtime.py` | `preflight()` also refuses an image whose runtime label differs from the host's own trusted sources |
| `docs/v2/p3/p35a_inert_reference/` (new) | Byte copies of the last inert `Containerfile`, `image-expectations.json` and `inert_entrypoint.py` from `41536829`, with SHA-256 provenance |
| Tests | New `tests/v2/p36/test_image_integration_p364a.py` (64 tests). Narrow updates to `test_image_p35a.py` (now tests the frozen reference), `test_rootless_p35b.py`, `conftest.py`, `test_container_runtime_p36.py`, `test_contract_purity.py` and `test_integration_controls_p51a.py` |

The 14 runtime source files are **byte-identical** to the reviewed P3.6-3 sources, and a test enforces
this. Unchanged: V1, `sandbox_v2/`, the P4/P5 evidence, the CI run #4 evidence, both requirements
files, the CI workflow, `podman_argv.py` and every runtime isolation flag.

## 2. Build-context changes

Before (P3.5a): `Containerfile`, `requirements.lock`, `inert_entrypoint.py` and `wheels/`.
After (P3.6-4A): `Containerfile`, `requirements.lock`, `opt-ip/<14 files>` and `wheels/`.

`stage()` never copies a directory. Each runtime file is read with `lstat`, must be a regular file
whose real path stays inside the repository, and is written with exclusive create. Paths must match
`[a-z0-9_]+(/[a-z0-9_]+)*\.(py|json)`, be normalised, unique and sorted. Tests stage from a fake
repository full of decoys (`.env`, `.git/`, `venv/`, a dataset CSV, `config/secrets.json`, a cached
`.pyc`, an extra runtime file, `bootstrap.py`, `agent/`). None of them reaches the context, and two
stagings give the same context digest.

## 3. Image entrypoint and dependency closure

```
ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/ip/supervisor.py"]   CMD []
/opt/ip/supervisor.py  runner.py  selftest.py  preludes.json
/opt/ip/v2/__init__.py  v2/execution/{__init__,errors,isolation,limits,results,frame,supervisor_helpers}.py
/opt/ip/config/{__init__,feature_flags}.py
io.insightpilot.runtime.sha256 = 4132ff83f369ac398e78c2f5281ec5e5622b32e6d492415e011cb3dd39969eb8
```

- The supervisor puts `/opt/ip` first on its own `sys.path`. The runner and the generated code run
  under `-I`, so neither `/opt/ip`, the working directory nor `PYTHON*` variables reach their import path.
- A staged copy of the tree, imported by a fresh `python3 -I -B` with no repository on the path,
  loads exactly these 10 modules plus the 3 entry modules. Every staged file is used.
- The only third-party import is `PIL` (Pillow 13.0.0, hash-locked). Everything else is the standard library.
- Started with no arguments, the supervisor writes one fixed usage line and exits 78 with no frame.
  `verify` and `measure-startup` use this in place of the inert check.

## 4. Security review of the entrypoint and closure (Task 4)

| Concern | Finding (static and offline) | Enforced by |
|---|---|---|
| Unexpected filesystem access | The supervisor reads `/proc/self/{status,mountinfo}`, `/sys/class/net`, the cgroup files and `/proc` (PID list). It reads `/out/chart.png` with `O_NOFOLLOW`, bounded. It writes only fd 1 (the frame) and fd 2. The runner reads `preludes.json` and `/in/code.py`. Importing the closure opens nothing for writing and touches no `/in`, `/out`, `/proc` or `/sys` path (audit hook) | `test_importing_the_staged_runtime_…` |
| Network access | No network module anywhere in the closure. Generated code can still import `socket`; only `--network=none` stops it, and that is **not verified** | `test_closure_imports_only_…` |
| Uncontrolled subprocesses | Exactly one `Popen` (in `RealOS.spawn`): fixed argv, `stdin=DEVNULL`, explicit env, `close_fds=True`, a new session, `cwd=/tmp`, no shell. `os.fork` appears only in the pids self-test, `os.kill` only in `kill_all`, `ctypes` only for `prctl`, and `exec` only in `runner.main` | `test_process_creation_…`, `test_the_child_is_started_once_…` |
| Inherited environment | The child gets the fixed 11-name `CHILD_ENV` (no secret or proxy names). The image `ENV` is unchanged. The entry and contract modules never read `os.environ`; `feature_flags` reads it only inside functions, which the image never calls | `test_the_in_image_entry_…` |
| Untrusted input on argv | The child argv is built only from two enums and integers. The supervisor argv comes from the `run_argv` grammar, and anything else exits 78. Code arrives only through `/in/code.py` | `test_the_child_is_started_once_…`, the staged-argv refusal tests |
| Files writable by the child | Requested: a read-only root, tmpfs `/tmp` and `/out` (noexec, nosuid, nodev), and `/in` read-only. `/opt/ip` is root-owned 0444/0555 and is write-probed | `test_the_child_can_write_only_…` |
| Dependencies outside the pinned image | None. Standard library plus `PIL` → `pillow==13.0.0` in the lock | `test_dependency_lock_…` |
| Execution at import or build | The entry modules only define names at import time (AST). No `RUN` step references the runtime, and `compileall` touches only site-packages. Generated source during import comes only from `dataclasses` and `collections` | `test_entry_modules_only_define_…`, `test_image_construction_runs_no_runtime_file` |

**Residual concerns (unresolved; carry into P3.6-4B):**
- The generated child and PID 1 share uid 10001. The protection against the child signalling,
  `ptrace`-ing, or opening `/proc/1/fd/*` of the supervisor rests on PID-namespace init semantics and
  `PR_SET_DUMPABLE=0`, and neither has been observed.
- The child can read everything readable in the image, including the `/opt/ip` sources (no secrets) and the dataset (intended).
- Stage A decodes an untrusted PNG with Pillow inside PID 1, after every untrusted process has been killed.

## 5. Tests and mutation results

| Run | Result |
|---|---|
| New `test_image_integration_p364a.py` | 64 passed |
| **Full offline suite** at `deabb605757083c992c5afd85d39791352adbe0f` (clean committed checkout; 2026-11-23 05:18:58–05:25:51 UTC) | **1836 passed, 13 xfailed, 0 failed, 0 skipped, 0 errors**. Exit 0. `git status --porcelain` was empty afterwards |
| Mutation checks (`docs/v2/p3/p364a_mutation_checks.md`) | **31/31 detected at the final commit.** The first batch at `03501e00` detected 29; the 2 survivors (L5, T3) were closed by new tests and re-run at `deabb605`. A secondary check also caught S1–S3 with their dedicated security tests alone |

The command:

```bash
env -u GOOGLE_API_KEY PYTHONDONTWRITEBYTECODE=1 <venvB>/bin/python -m pytest tests/ -q -p no:cacheprovider -rxfEs
```

It ran on CPython 3.11.18 (environment B), with no network, API or container use.

- **Count:** the baseline before P3.6-4A was 1769 passed and 13 xfailed. The difference, +67, is 64 new tests plus 2 P3.5b label cases plus 2 runtime-label refusal cases, minus 1 parametrisation (`inert_entrypoint.py` left `CONTEXT_FILES`).
- **xfails:** the 13 strict xfails are the documented V1 baseline gaps, unchanged.
- **Warnings:** the 3 warnings are the documented Streamlit AppTest stale-widget retry (`docs/v2/BASELINE.md`); how many appear varies between runs.
- **Discarded runs:** two earlier full-suite runs were stopped on purpose and are not used as evidence. The first started from a working tree with uncommitted docs, and the second had a test edited in the checkout while it ran.

**Preservation and fail-closed checks**, all passing in that run:
- **V1:** `ui`, `agent`, `sandbox`, `utils`, `rules`, `skills`, `data`, `lessons` and `requirements.txt` are byte-identical to `fd5783c6` (`test_v1_application_paths_are_byte_identical_to_the_examination_commit`, `test_v1_application_and_p4_evidence_are_unchanged`).
- **P4.1 and P5 evidence:** `sandbox_v2/`, `docs/v2/p4`, `docs/v2/p5`, `evaluation/`, `requirements-v2.txt` and `docs/v2/p3/ci_run4` are unchanged (`test_p41_optimisation_and_all_benchmark_evidence_are_unchanged`, which also checks `HARNESS_VERSION == "p4.1"`).
- **Runtime sources:** the 14 runtime sources are byte-identical to the reviewed P3.6-3 sources (`test_the_installed_runtime_is_byte_identical_to_the_reviewed_p36_3_sources`).
- **Production fail-closed:** `ensure_ready` refuses under every hardened, validation-variable and orchestrator combination tested, and nothing outside `tests/` references `install_for_validation` (`test_production_still_refuses_…`, `test_production_cannot_reach_hardened_mode_…`).
- **Protected refs (`git ls-remote`, 2026-11-23):**
  - `main` and `exam-backup-2026-11-17` are at `fd5783c6`; `feature/insightpilot-v2` is at `1fd3af3c`.
  - P3 is at `2d0a5a73`, the probe branch at `4cd107e0` and P4 at `d321785c`.
  - P5 is at `1444491e`, P5.1A at `05d1588d` and P3.6 at `41536829`.
  - The default branch is still `main`. None of these refs changed.

## 6. Outstanding real-runtime uncertainties (P3.6-4B)

1. Whether Buildah's `COPY --from=build /stage/opt-ip/ /opt/ip/` keeps 0444/0555 and owner 0:0, and
   which mode `/opt/ip` itself ends with (0755 from `mkdir`, or 0555).
2. Two clean builds giving identical RootFS layers with the new layer (reproducibility).
3. The supervisor's `RealOS` has never run: signal plan, `prctl` through `ctypes` in the slim image,
   `select` on pipes, `kill(-1)`, reaping until `ECHILD`, `/proc` enumeration and cgroup reads.
4. The dev script's runtime expectations (`allowed_mountpoints` lists `/in/dataset.csv` and no `/out` tmpfs)
   differ from `podman_argv`'s mount set (`/in` directory, two tmpfs). This must be reconciled against real
   mountinfo; it was deliberately left unchanged (no isolation change without review).
5. Pillow 13 in the image against 12.3 in environment B, so only the hermetic chart references apply.
6. Start-up cost of an uncompiled `/opt/ip` (`-B`, read-only root, no `.pyc`), to be measured.
7. Same-uid child versus supervisor protections (§4).
8. The no-argument message is pinned to the supervisor's exact usage text, deliberately strict.

## 7. Rollback

Delete `feature/v2-p364a-image-integration`, or revert its commits newest first: the final docs
commit, then `deabb605`, `24578d70`, `5f8e1370`, `03501e00`. Reverting only `03501e00` and the three
commits after it restores the P3.6-3 state exactly. No other branch changed. The P3.6-3 head `41536829` still holds the inert image definition, and production behaviour
is identical either way.

## 8. Exact requirements for P3.6-4B (NOT approved; not started)

1. **A separate approval** for a workflow revision (the guard `test_the_ci_workflow_only_gained_comment_lines`
   must change with it) on a GitHub-hosted ubuntu-24.04 runner. Rootless Podman only, the runner's
   read-only token, no secrets.
2. `check-host --probe`, then **two clean `build`s** of the same commit; `compare` must report identical image IDs or a
   member-level diff. The build must refuse on any lock, runtime-label or context mismatch.
3. `verify`: image config (entrypoint, stage and runtime labels), inspector image section (exactly the 14 `opt_ip_files`,
   no world-writable, setid, secret-like or dev files), write probes for every runtime file, and the no-argument refusal.
4. `ContainerRuntime.preflight()` on the built image: host rules, image config, attest-only frame and attestation, OOM
   and pids self-tests. Real container stdout and stderr preserved and sanitised as in P3.6-0.
5. The container-marked suite through `install_for_validation` only: golden 28/28 for both preludes, 12/12
   hermetic charts, every fail-closed case, and the P5 20-case suite. **0 skips.** Generated code limited to the
   committed, pre-reviewed fixtures; no live model.
6. Record items 1–8 of §6 as observed facts, and resolve item 4 by a reviewed expectations change, not by loosening checks.
7. The evidence is **functional only**: the runner account is root-equivalent, so no security claim follows. A vulnerability
   scan needs a named scanner and database. Security claims need VE-2 (R8) and P3.7 (seccomp, independent limits).
