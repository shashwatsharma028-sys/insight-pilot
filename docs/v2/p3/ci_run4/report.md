## P3.5b CI image build and benchmark (GitHub ubuntu-24.04, rootless Podman)

commit: 4cd107e0eeca61996a4e1b41fe473f93ed14a05a

| Stage | Result |
|---|---|
| Gate 1: host facts as provided (informational) | PASS |
| Gate 2: host facts with a user manager | PASS |
| Gate 3: enforcement probe (real rootless container) | PASS |
| Phase C: SHA-256-verified wheel cache | PASS |
| Phase C: build 1 (offline, digest-pinned base) | PASS |
| Phase C: verification of build 1 | PASS |
| Phase D: clean build 2 (identical inputs) | PASS |
| Phase D: build 1 vs build 2 comparison | PASS |
| Phase E: warm/cold start-up benchmark | PASS |
| Phase E: hash-pinned golden fixtures in the image | PASS |
| Phase E: OS package inventory | PASS |
| Readiness tests (incl. workflow-policy test) | PASS |

### Build 1

- tag `localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-x86_64`
- image ID `0ffd44b311122a9213bf513435f831931ba0392564406dcda470c03ed6c43c2a`
- digest `sha256:f268a42edd4458ed3260618166a9e0ee201362949d12911496c9dcc8e84f6933`
- size 589224532 bytes (561.9 MiB)
- build duration 24.6 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `0026e45b39e5f57d771cc3dca7fcbdf4bf715f8acbf9da9812577276c65e0791` (21 files)
- RootFS layers: 7

### Build 2

- tag `localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-x86_64`
- image ID `0ffd44b311122a9213bf513435f831931ba0392564406dcda470c03ed6c43c2a`
- digest `sha256:f268a42edd4458ed3260618166a9e0ee201362949d12911496c9dcc8e84f6933`
- size 589224532 bytes (561.9 MiB)
- build duration 20.1 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `0026e45b39e5f57d771cc3dca7fcbdf4bf715f8acbf9da9812577276c65e0791` (21 files)
- RootFS layers: 7

### Verification of build 1

- result: all image, runtime and inert-entrypoint checks passed (verify exits non-zero otherwise)
- distributions found: 18

### Reproducibility

- same image ID: True
- same RootFS layers: True
- build 1 ID `0ffd44b311122a9213bf513435f831931ba0392564406dcda470c03ed6c43c2a`, build 2 ID `0ffd44b311122a9213bf513435f831931ba0392564406dcda470c03ed6c43c2a`

### Start-up (inert container, wall clock from the podman client)

- warm: n=30, median 86.0 ms, p95 94.4 ms (min 71.1, max 95.1)
- cold: n=10, median 90.2 ms, p95 108.4 ms (min 87.5, max 108.4)
- cold definition: first run after podman rmi + podman load; page cache not dropped

### Golden fixtures inside the image

- numerical: 28/28 golden answers match (mismatched or missing: 0)
- charts: 7 produced; 12/12 tasks match chart_hermetic exactly (identical decoded pixels, or correctly absent)
- render environment: 12/12 tasks report the committed hermetic environment
- informational: 5/12 match the preserved P3.5a host reference (rendered with the host's MATPLOTLIBRC and system fonts)
- fixture exits: [0]
- Matplotlib temporary-cache warnings: 0

### OS package inventory

- 105 dpkg packages (full list in the log)
- vulnerability scan: NOT RUN (no approved scanner and database source)

Runner account is root-equivalent (passwordless sudo, docker group): this is not VE-2 non-root or cross-account validation.

VERDICT: every stage ran and passed
