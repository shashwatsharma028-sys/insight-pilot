# P3.6-4A mutation checks: image-integration guards

**Final result: 31/31 mutants detected at `deabb605757083c992c5afd85d39791352adbe0f`.** The first run
found 2 survivors, L5 and T3. Both were closed by new tests, not by weakening anything, and
re-run against the final commit.

## Method

- **Worktree:** each mutant was applied to a detached Git worktree, never to the main checkout.
- **Tests run against each mutant:** `tests/v2/p36/`, `tests/v2/p3/test_image_p35a.py` and `tests/v2/p3/test_rootless_p35b.py`, with `-x`.
- **Environment:** CPython 3.11.18 (environment B), `PYTHONDONTWRITEBYTECODE=1`, `GOOGLE_API_KEY` unset.
- **Killed:** at least one test failed. The mutated file was restored with `git checkout` after each run, and the worktree was clean afterwards.
- **No containers:** no mutant ran a container runtime. Podman, Docker and Buildah were never invoked.

| Run | Commit | Mutants | Killed | Survived |
|---|---|---|---|---|
| Full batch | `03501e00` (implementation) | 31 | 29 | 2 (L5, T3) |
| Re-run of the survivors | `deabb605` (final) | 2 | 2 | 0 |
| Secondary check: S1–S3 against only their dedicated tests | `deabb605` | 3 | 3 | 0 |

The batch ran at `03501e00`. The two later commits change only `tests/v2/p36/test_image_integration_p364a.py`
by adding tests, so the other 29 detections still hold at `deabb605`.

## Results

| # | Deliberate defect | Result | First failing test |
|---|---|---|---|
| L1 | closure misses frame.py | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| L2 | extra repository file installed | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| L3 | traversal paths accepted | killed | `test_layout_validation_rejects_unsafe_unsorted_or_duplicate_entries[files0]` |
| L4 | unsorted/duplicate layout accepted | killed | `test_layout_validation_rejects_unsafe_unsorted_or_duplicate_entries[files5]` |
| L5 | symlinked source followed | **survived** at `03501e00` → **killed** at `deabb605` | `test_staging_refuses_a_missing_or_unsafe_runtime_source[symlink_inside_repo]` |
| L6 | source may escape the repository | killed | `test_staging_refuses_a_missing_or_unsafe_runtime_source[escaping_dir]` |
| L7 | digest ignores file content | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| T1 | inert entrypoint back in the context | killed | `test_staged_context_holds_exactly_the_allowlist_and_no_decoy` |
| T2 | staging skips layout validation | killed | `test_staging_refuses_a_missing_or_unsafe_runtime_source[symlink]` |
| T3 | staged digest computed from the repository, not the context | **survived** at `03501e00` → **killed** at `deabb605` | `test_the_staged_digest_is_computed_from_the_context_not_the_repository` |
| C1 | entrypoint still the inert program | killed | `test_entrypoint_is_exactly_the_trusted_supervisor_in_exec_form` |
| C2 | entrypoint without isolated mode | killed | `test_entrypoint_is_exactly_the_trusted_supervisor_in_exec_form` |
| C3 | runtime files left writable (no chmod 0444) | killed | `test_image_construction_runs_no_runtime_file` |
| C4 | whole build stage copied into /opt/ip | killed | `test_every_containerfile_copy_source_is_a_staged_entry` |
| C5 | stale runtime label | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| C6 | stage label not updated | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| C7 | runtime executed during image construction | killed | `test_image_construction_runs_no_runtime_file` |
| C8 | runtime tree compiled at build | killed | `test_image_construction_runs_no_runtime_file` |
| C9 | PYTHONPATH exposes /opt/ip to generated code | killed | `test_user_environment_and_declarations_are_unchanged_by_the_integration` |
| C10 | repository root copied into the build | killed | `test_every_containerfile_copy_source_is_a_staged_entry` |
| E1 | expectations miss an installed file | killed | `test_the_staged_tree_alone_satisfies_the_import_closure_and_every_file_is_needed` |
| E2 | supervisor not write-probed | killed | `test_the_child_can_write_only_the_two_bounded_tmpfs_and_never_the_runtime` |
| I1 | inspector does not probe the supervisor | killed | `test_the_child_can_write_only_the_two_bounded_tmpfs_and_never_the_runtime` |
| RL1 | driver ignores the runtime label | killed | `test_built_image_config_is_checked[mutate9-runtime label]` |
| RL2 | driver ignores the stage label | killed | `test_built_image_config_is_checked[mutate8-stage label]` |
| RL3 | build does not compare staged runtime with the label | killed | `test_the_build_driver_refuses_when_the_staged_runtime_differs_from_the_label` |
| RL4 | no-argument refusal ignores stdout | killed | `test_no_argument_refusal_output_must_be_exact` |
| K1 | preflight ignores the runtime label | killed | `test_preflight_refuses_and_stays_unusable[scenario3-runtime label]` |
| S1 | child inherits supervisor fds | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| S2 | child environment inherits the host | killed | `test_labels_pin_stage_lock_runtime_and_base` |
| S3 | runner executes code at import | killed | `test_labels_pin_stage_lock_runtime_and_base` |

Test names are the first failure reported with `-x`, cut to about 80 characters by the harness.

### Survivors and how they were closed

- **L5: symlinked runtime source followed** (`os.stat` instead of `os.lstat` in `image_layout.read_source`).
  The only symlink case pointed outside the repository, so the escape check refused it and hid the missing
  symlink check. A symlink to *another repository file* (for example `frame.py` → `bootstrap.py`) would have
  been staged under a reviewed name. Fix in `24578d70`: the case `symlink_inside_repo` in
  `test_staging_refuses_a_missing_or_unsafe_runtime_source`.
- **T3: staged digest computed from the repository, not the staged context.** Every test staged identical
  copies, so both digests agreed. `build()` must compare the label with what will actually be copied. Fix in
  `deabb605`: `test_the_staged_digest_is_computed_from_the_context_not_the_repository`. It changes one
  byte in a staged file and requires a different digest, and it requires a missing staged file to fail.

### Secondary check: closure-file mutants (S1–S3)

Any change to a runtime file first trips the runtime-digest label pin (`test_labels_pin_stage_lock_runtime_and_base`).
That is intended: it makes every runtime change a reviewed Containerfile change. To show the dedicated
security-review tests also catch each defect independently, S1–S3 were re-run against only those tests:

| # | Result | Failing test |
|---|---|---|
| S1 | killed | `test_the_child_is_started_once_with_fixed_options_and_a_fixed_environment` |
| S2 | killed | `test_the_child_is_started_once_with_fixed_options_and_a_fixed_environment` |
| S3 | killed | `test_entry_modules_only_define_things_at_import_time[runner.py]` |

## Reproduce

```bash
git worktree add --detach /tmp/wt364 deabb605757083c992c5afd85d39791352adbe0f
```

```bash
PYTHONDONTWRITEBYTECODE=1 <venvB>/bin/python -B mut_p364a.py <dir containing venvB> /tmp/wt364
```

```bash
PYTHONDONTWRITEBYTECODE=1 <venvB>/bin/python -B mut_p364a_secondary.py <dir containing venvB> /tmp/wt364
```

```bash
git worktree remove /tmp/wt364
```

A third argument such as `L5,T3` runs only those mutants. Each mutant's original text must occur exactly
once in the target file, otherwise the harness stops.

<details><summary><code>mut_p364a.py</code> (main harness, verbatim)</summary>

```python
import os, subprocess, sys
SP, WT = sys.argv[1], sys.argv[2]
PY = os.path.join(SP, "venvB/bin/python")
TESTS = ["tests/v2/p36/", "tests/v2/p3/test_image_p35a.py", "tests/v2/p3/test_rootless_p35b.py"]
L, T, C, E = ("v2/execution/image_layout.py", "scripts/p3/image_tool.py", "v2/execution/image/Containerfile",
              "v2/execution/image/image-expectations.json")
RL, CR, I = "scripts/p3/rootless.py", "v2/execution/container_runtime.py", "scripts/p3/image_inspect.py"
S, RU = "v2/execution/image/runtime/supervisor.py", "v2/execution/image/runtime/runner.py"
M = [
 ("L1 closure misses frame.py", L, '    ("v2/execution/frame.py", "v2/execution/frame.py"),\n', ''),
 ("L2 extra repository file installed", L, '    ("v2/execution/errors.py", "v2/execution/errors.py"),\n', '    ("v2/execution/bootstrap.py", "v2/execution/bootstrap.py"),\n    ("v2/execution/errors.py", "v2/execution/errors.py"),\n'),
 ("L3 traversal paths accepted", L, '_REL = re.compile(r"[a-z0-9_]+(/[a-z0-9_]+)*\\.(py|json)")', '_REL = re.compile(r".+")'),
 ("L4 unsorted/duplicate layout accepted", L, '    if dests != sorted(dests) or len(set(dests)) != len(dests):', '    if False:'),
 ("L5 symlinked source followed", L, '    st = os.lstat(path)', '    st = os.stat(path)'),
 ("L6 source may escape the repository", L, '    if os.path.commonpath([os.path.realpath(path), real_root]) != real_root:', '    if False:'),
 ("L7 digest ignores file content", L, '        h.update(f"{dest}\\0{len(data)}\\0{hashlib.sha256(data).hexdigest()}\\n".encode())', '        h.update(f"{dest}\\0{len(data)}\\n".encode())'),
 ("T1 inert entrypoint back in the context", T, 'CONTEXT_FILES = ("Containerfile", "requirements.lock")', 'CONTEXT_FILES = ("Containerfile", "requirements.lock", "inert_entrypoint.py")'),
 ("T2 staging skips layout validation", T, '        image_layout.check_layout()\n        for dest, src in image_layout.RUNTIME_FILES:\n            data = image_layout.read_source(src, source_root)', '        for dest, src in image_layout.RUNTIME_FILES:\n            data = open(os.path.join(source_root, src), "rb").read()'),
 ("T3 staged digest computed from the repository, not the context", T, '    return image_layout.tree_digest(entries)', '    return image_layout.runtime_tree_sha256()'),
 ("C1 entrypoint still the inert program", C, 'ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/ip/supervisor.py"]', 'ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/ip/inert_entrypoint.py"]'),
 ("C2 entrypoint without isolated mode", C, 'ENTRYPOINT ["/usr/local/bin/python3", "-I", "-B", "/opt/ip/supervisor.py"]', 'ENTRYPOINT ["/usr/local/bin/python3", "/opt/ip/supervisor.py"]'),
 ("C3 runtime files left writable (no chmod 0444)", C, '    find /stage/opt-ip -type f -exec chmod 0444 {} +\n', '    true\n'),
 ("C4 whole build stage copied into /opt/ip", C, 'COPY --from=build /stage/opt-ip/ /opt/ip/', 'COPY --from=build /stage/ /opt/ip/'),
 ("C5 stale runtime label", C, 'io.insightpilot.runtime.sha256="4', 'io.insightpilot.runtime.sha256="5'),
 ("C6 stage label not updated", C, 'io.insightpilot.stage="P3.6"', 'io.insightpilot.stage="P3.5a-inert"'),
 ("C7 runtime executed during image construction", C, 'COPY --from=build /stage/opt-ip/ /opt/ip/\n', 'COPY --from=build /stage/opt-ip/ /opt/ip/\nRUN python3 -I -B /opt/ip/supervisor.py || true\n'),
 ("C8 runtime tree compiled at build", C, '-p /usr/local/lib/python3.11/site-packages /stage/site-packages\n', '-p /usr/local/lib/python3.11/site-packages /stage/site-packages /stage/opt-ip\n'),
 ("C9 PYTHONPATH exposes /opt/ip to generated code", C, 'ENV PYTHONDONTWRITEBYTECODE=1', 'ENV PYTHONPATH=/opt/ip PYTHONDONTWRITEBYTECODE=1'),
 ("C10 repository root copied into the build", C, 'COPY opt-ip/ /stage/opt-ip/', 'COPY . /stage/opt-ip/'),
 ("E1 expectations miss an installed file", E, '  "v2/execution/frame.py",\n', ''),
 ("E2 supervisor not write-probed", E, '   "/opt/ip/supervisor.py",\n', ''),
 ("I1 inspector does not probe the supervisor", I, '"/opt/ip/supervisor.py", "/opt/ip/runner.py",', '"/opt/ip/runner.py",'),
 ("RL1 driver ignores the runtime label", RL, '    if labels.get(image_layout.RUNTIME_LABEL) != image_layout.runtime_tree_sha256():', '    if False:'),
 ("RL2 driver ignores the stage label", RL, '    if labels.get("io.insightpilot.stage") != EXPECT["stage_label"]:', '    if False:'),
 ("RL3 build does not compare staged runtime with the label", RL, '        if f\'{image_layout.RUNTIME_LABEL}="{runtime_sha}"\' not in containerfile:', '        if False:'),
 ("RL4 no-argument refusal ignores stdout", RL, '    if result.stdout:\n        p.append(f"NOARGS', '    if False:\n        p.append(f"NOARGS'),
 ("K1 preflight ignores the runtime label", CR, '        if labels.get(EXPECTED_RUNTIME_LABEL) != image_layout.runtime_tree_sha256():', '        if False:'),
 ("S1 child inherits supervisor fds", S, 'env=env, close_fds=True,', 'env=env, close_fds=False,'),
 ("S2 child environment inherits the host", S, 'CHILD_ENV = {"MPLBACKEND"', 'CHILD_ENV = {**os.environ, "MPLBACKEND"'),
 ("S3 runner executes code at import", RU, '\nif __name__ == "__main__":\n    sys.exit(main())', '\nmain()\nif __name__ == "__main__":\n    sys.exit(main())'),
]
only = sys.argv[3].split(",") if len(sys.argv) > 3 else None
env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}; env["PYTHONDONTWRITEBYTECODE"] = "1"
for name, rel, old, new in M:
    if only and name.split()[0] not in only: continue
    p = os.path.join(WT, rel); s = open(p).read()
    assert s.count(old) == 1, (name, s.count(old))
    open(p, "w").write(s.replace(old, new, 1))
    try:
        r = subprocess.run([PY, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider", "-p", "no:logging"],
                           cwd=WT, capture_output=True, text=True, env=env, timeout=1500)
    finally:
        subprocess.run(["git", "checkout", "--", rel], cwd=WT, check=True)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or "error" in l][-1:]
    fails = [l.split("::")[-1][:80] for l in r.stdout.splitlines() if l.startswith(("FAILED", "ERROR"))][:1]
    print(("KILLED  " if r.returncode != 0 else "SURVIVED"), name, "|", *last, "|", *fails, flush=True)
```
</details>

<details><summary><code>mut_p364a_secondary.py</code> (secondary check, verbatim)</summary>

```python
import os, subprocess, sys
SP, WT = sys.argv[1], sys.argv[2]
PY = os.path.join(SP, "venvB/bin/python")
F = "tests/v2/p36/test_image_integration_p364a.py"
S, RU = "v2/execution/image/runtime/supervisor.py", "v2/execution/image/runtime/runner.py"
M = [
 ("S1", S, 'env=env, close_fds=True,', 'env=env, close_fds=False,', "test_the_child_is_started_once_with_fixed_options_and_a_fixed_environment"),
 ("S2", S, 'CHILD_ENV = {"MPLBACKEND"', 'CHILD_ENV = {**os.environ, "MPLBACKEND"', "test_the_child_is_started_once_with_fixed_options_and_a_fixed_environment"),
 ("S3", RU, '\nif __name__ == "__main__":\n    sys.exit(main())', '\nmain()\nif __name__ == "__main__":\n    sys.exit(main())', "test_entry_modules_only_define_things_at_import_time or test_importing_the_staged_runtime"),
]
env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}; env["PYTHONDONTWRITEBYTECODE"] = "1"
for name, rel, old, new, sel in M:
    p = os.path.join(WT, rel); s = open(p).read(); assert s.count(old) == 1, name
    open(p, "w").write(s.replace(old, new, 1))
    try:
        r = subprocess.run([PY, "-m", "pytest", F, "-q", "-p", "no:cacheprovider", "-p", "no:logging", "-k", sel],
                           cwd=WT, capture_output=True, text=True, env=env, timeout=600)
    finally:
        subprocess.run(["git", "checkout", "--", rel], cwd=WT, check=True)
    last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l][-1:]
    fails = [l.split("::")[-1][:90] for l in r.stdout.splitlines() if l.startswith("FAILED")]
    print(("KILLED  " if r.returncode != 0 else "SURVIVED"), name, "|", *last, "|", "; ".join(fails), flush=True)
```
</details>
