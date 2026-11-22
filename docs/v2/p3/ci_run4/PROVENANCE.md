# GitHub Actions run #4 evidence (VE-1, inert P3.5a image): provenance

| Field | Value |
|---|---|
| Workflow | `p3-ubuntu-rootless-probe` (`.github/workflows/p3-ubuntu-rootless-probe.yml`) |
| Run / attempt | run id 45033387515, run number 4, attempt 1, conclusion **success** |
| Job | `probe`, job id 137241676343, runner label `ubuntu-24.04` |
| Commit under test | `4cd107e0eeca61996a4e1b41fe473f93ed14a05a` (branch `feature/v2-github-ubuntu-probe`) |
| Run time | 2026-11-22T14:32:38Z – 14:34:52Z |
| Retrieved | 2026-11-22, through the GitHub API job-log endpoint (read-only); the run uploaded no artifacts |
| Raw log | 1,926 lines, SHA-256 `9bb247aa9bbcf006246b263d316d116f24a7ee24accf82394846f8c850140c3b` (**not committed**) |
| Sanitized log | `job_137241676343.sanitized.log`, SHA-256 `46d42257252e7cc85009e39d99c797aac7447121969c72f84b27ec4e80dce20f` |
| Derived files | `report.md` (the run's own evidence report, timestamps removed); `enforcement_probe.txt` (the Gate 3 step: in-container enforcement facts) |
| Sanitizer | `sanitize.py` in this folder; deterministic; rules R1–R6 in its docstring. `python3 -I -B sanitize.py RAW OUT` reproduces both digests from the raw log |

## Pre-commit review of the raw log (P3.6-0)

- **Credentials:** the only credential (`GITHUB_TOKEN`, read-only `contents: read`) appears masked as `***`. All 8 lines that handle it (the token-permission header, `Secret source`, the masked `TOKEN` env line and the authenticated `git fetch` command lines) are replaced by a placeholder (R2). No other token, key or password pattern was found.
- **Personal data:** no e-mail address, personal name or user login of the repository owner appears. The runner account is the generic hosted-runner service account.
- **Unnecessary environment identifiers removed:** the hosted-compute worker id, region and cloud, the provisioner commit and build date (R3), runner home and runtime paths (R4), and overlay storage layer ids (R5).
- **Kept:** every command, exit status, version, measured value, image id and digest.

## What this evidence shows, and what it does not

**It shows**, for the **inert P3.5a image** on a GitHub-hosted runner with Podman 4.9.3 and kernel 7.0.0-1014-aws:
- reproducible offline rootless builds (identical image ID `0ffd44b3…`, digest `sha256:f268a42e…`);
- image verification;
- in-container uid 10001, CapEff, CapPrm, CapBnd and CapAmb all 0, NoNewPrivs 1, Seccomp 2;
- only `lo`, and EROFS outside `/tmp`;
- `memory.max` 268435456 (the probe's 256 MiB; see the P3 design §8.3 note on the approved 1 GiB);
- hash-pinned golden fixtures: 28/28 answers, 12/12 exact hermetic charts;
- warm and cold start-up of about 90 ms.

**It does not show:**
- isolation of untrusted code;
- non-root or cross-account protection: the runner is **root-equivalent** (passwordless sudo, docker group);
- resource-limit enforcement under load;
- PID-1 behaviour.

There was no vulnerability scan, the readiness tests reported 8 skips, and the P3.6 image (supervisor and runner) does not exist yet. **This is functional VE-1 evidence only, not a security validation.**
