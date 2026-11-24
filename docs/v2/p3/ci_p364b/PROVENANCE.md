# P3.6-4B CI evidence: provenance

The source is GitHub Actions workflow `p3-ubuntu-rootless-probe` in `shashwatsharma028-sys/insight-pilot`,
on a GitHub-hosted `ubuntu-24.04` runner. The repository is public, so standard-runner minutes are not billed.
The logs were retrieved through the GitHub API (MCP `get_job_logs`).

| Run | Run ID | Job ID | Commit | Result |
|---|---|---|---|---|
| #5 | 45121353914 | 137515602649 | `7d63b0f78fb88b9cea39b7a8c7644f5647c92d79` | Stage 1 passed; Phase F runtime refused at preflight |
| #6 | 45122041350 | 137517791540 | `15a2a5b6572bac270de60301ce63683c77e0e0ed` | the same, with diagnostics recorded (root cause) |
| #9 | 45344790686 | 138276631114 | `36b0beb306f4a753fad2ef490e5cca536322a920` | D2 attempt: preflight and attestation passed; real runtime 16 passed, 1 failed (test-code defect: a helper used fixture functions), 2 deselected; P5 steps NOT RUN |
| #8 | 45341414519 | 138265701550 | `5868f46360c0947b3171e4d12032caccd9c1405b` | D3 attempt: Stage 1 and the argument-compatibility check passed; the container started; the attestation was refused only on `writable_mounts` (D2) |
| #7 | 45125846903 | 137529873779 | `d1e208cbccbe8c67e4c540f166b03d50f103b0ca` | D1 attempt: Stage 1 passed; the DNS conflict is gone; the runtime was refused on the tmpfs option `nr_inodes` (D3) |

| File | SHA-256 |
|---|---|
| raw log #5, as retrieved (NOT committed) | `6ca18fc9b8d4ce6ca7ab5546d2a9f741fa374e78767a29bb2bc94366aee125bd` |
| raw log #6, as retrieved (NOT committed) | `81a830f8bf8e5da8bda46cc1562ed05eb76f446876fd3d4da06337226da1b299` |
| raw log #9, as retrieved (NOT committed) | `939c9acb9bac779af52c90ec7692063787f8c46467550e2b0cb141d71a9801c1` |
| `run9_job_138276631114.sanitized.log` | `f52b22397f9cf42515efa2cba48eacef54e3383468e981d6950b57c037b7682a` |
| `run9_report.md` | `8e576bc90691e328c27c6c89dd61f7294abd43529fa3b41c2fc1325d2530db9e` |
| raw log #8, as retrieved (NOT committed) | `9e2deafd673cf53f240c09231e25cf967409303171a4c4bfa288aea8ddde9343` |
| `run8_job_138265701550.sanitized.log` | `b22bba2592185f90768edca7a9c03320529642274312be472985313fd557bd02` |
| `run8_report.md` | `18eb74b41b5b6883ea57af4713a8dd14086d315c1b65b3140ad236644e220cf6` |
| raw log #7, as retrieved (NOT committed) | `5c7695ff138b7729c6cea368f98e98dc288d0758e17a5ab3ecb702400340109a` |
| `run5_job_137515602649.sanitized.log` | `6ecb67ed8f0348eed4eb7813480581c11a78834e453072177e270f64538a3260` |
| `run6_job_137517791540.sanitized.log` | `e6af19e8fd258b9bfe4e19137f1831b1fe8eb9b8aeed236b30853e106f49e500` |
| `run5_report.md` | `bc23b8329613b95f44b3329564060866f3026dac32902941fc1a81e9685c2633` |
| `run7_job_137529873779.sanitized.log` | `c981b1d1d76681257e08cb3c37bf003e93cd7416e8f6bfeb42aa11284c7127f7` |
| `run7_report.md` | `cc72f6bb5556194d5d96bf82aeb2bbc790147134121c5e359a45bfce0e81c12d` |
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
