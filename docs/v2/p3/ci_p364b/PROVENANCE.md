# P3.6-4B CI evidence: provenance

The source is GitHub Actions workflow `p3-ubuntu-rootless-probe` in `shashwatsharma028-sys/insight-pilot`,
on a GitHub-hosted `ubuntu-24.04` runner. The repository is public, so standard-runner minutes are not billed.
The logs were retrieved through the GitHub API (MCP `get_job_logs`).

| Run | Run ID | Job ID | Commit | Result |
|---|---|---|---|---|
| #5 | 45121353914 | 137515602649 | `7d63b0f78fb88b9cea39b7a8c7644f5647c92d79` | Stage 1 passed; Phase F runtime refused at preflight |
| #6 | 45122041350 | 137517791540 | `15a2a5b6572bac270de60301ce63683c77e0e0ed` | the same, with diagnostics recorded (root cause) |

| File | SHA-256 |
|---|---|
| raw log #5, as retrieved (NOT committed) | `6ca18fc9b8d4ce6ca7ab5546d2a9f741fa374e78767a29bb2bc94366aee125bd` |
| raw log #6, as retrieved (NOT committed) | `81a830f8bf8e5da8bda46cc1562ed05eb76f446876fd3d4da06337226da1b299` |
| `run5_job_137515602649.sanitized.log` | `6ecb67ed8f0348eed4eb7813480581c11a78834e453072177e270f64538a3260` |
| `run6_job_137517791540.sanitized.log` | `e6af19e8fd258b9bfe4e19137f1831b1fe8eb9b8aeed236b30853e106f49e500` |
| `run5_report.md` | `bc23b8329613b95f44b3329564060866f3026dac32902941fc1a81e9685c2633` |
| `run6_report.md` | `34c96d78262128e69a6d7a7be525cd359fecb6cdc36c5d0ae5066ff9d0401286` |

**Sanitisation:** `sanitize_p364b.py` applies exactly the reviewed P3.6-0 rules R1–R6 by importing
`docs/v2/p3/ci_run4/sanitize.py:sanitize_line`. The rules strip ANSI codes and remove credential-handling lines
(7 per log). They redact runner identifiers, host paths, overlay IDs, e-mail addresses and IPv4 addresses;
the IPv4 rule also redacts the RFC 5737 address in the fixture text. Commands, results, versions, digests and
timings are kept.

A post-sanitisation scan for tokens, authorization headers, e-mail addresses and host paths found nothing.

Reproduce:

```bash
python3 -I -B docs/v2/p3/ci_p364b/sanitize_p364b.py RAW_LOG RUN JOB OUT_DIR
```
