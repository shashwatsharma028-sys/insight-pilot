## P3.6-4B CI image build and real-runtime validation (GitHub ubuntu-24.04, rootless Podman)

commit: f50af3dd0f33742b96bfe9a20582c2d75dd0db53

| Stage | Result |
|---|---|
| Approved-content gate (pushed code == reviewed P3.6-4B content) | PASS |
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
| Phase F: host test environment | PASS |
| Phase F: Podman argument compatibility (create only, reviewed argv) | PASS |
| Phase F: real ContainerRuntime (preflight, isolation, fail-closed, golden numbers and charts) | PASS |
| Phase F: P5 20-case suite, V1 prelude, real container port | PASS |
| Phase F: P5 20-case suite, lazy-seaborn prelude, real container port | PASS |
| Readiness tests (incl. workflow-policy test) | PASS |

### Build 1

- tag `localhost/insightpilot-sandbox:p3.6-9a355dd6e120-x86_64`
- image ID `a9f31b115177e216b591b30b4fa67b6d149b7edfb5a8231be3168ddd4fad8be7`
- digest `sha256:2425bd3e91295bd9a3472906d0475e6bf20c3faff765af11dcd7691e5495d162`
- size 589307638 bytes (562.0 MiB)
- build duration 25.1 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `46ac9ce88d3e01ca6850882e8dfd49877e21c659e6183b6c5a8315d9f712db55` (34 files)
- RootFS layers: 7

### Build 2

- tag `localhost/insightpilot-sandbox:p3.6-9a355dd6e120-x86_64`
- image ID `a9f31b115177e216b591b30b4fa67b6d149b7edfb5a8231be3168ddd4fad8be7`
- digest `sha256:2425bd3e91295bd9a3472906d0475e6bf20c3faff765af11dcd7691e5495d162`
- size 589307638 bytes (562.0 MiB)
- build duration 20.5 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `46ac9ce88d3e01ca6850882e8dfd49877e21c659e6183b6c5a8315d9f712db55` (34 files)
- RootFS layers: 7

### Verification of build 1

- result: all image (incl. /opt/ip hashes, owners, modes), runtime and no-argument-refusal checks passed (verify exits non-zero otherwise)
- distributions found: 18

### Reproducibility

- same image ID: True
- same RootFS layers: True
- build 1 ID `a9f31b115177e216b591b30b4fa67b6d149b7edfb5a8231be3168ddd4fad8be7`, build 2 ID `a9f31b115177e216b591b30b4fa67b6d149b7edfb5a8231be3168ddd4fad8be7`

### Start-up (supervisor entrypoint with no arguments: imports, refuses, exits 78; podman client wall clock)

- warm: n=30, median 121.9 ms, p95 135.0 ms (min 112.0, max 135.9)
- cold: n=10, median 119.7 ms, p95 126.7 ms (min 111.9, max 126.7)
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

### Phase F: Podman argument compatibility (create only, never started)

- full reviewed argv accepted in every mode: True
- run-only flags present in `podman run --help`: {'--passwd=false': True}
- options rejected on their own: []
- removal that makes the full argv pass: []

### Phase F: real ContainerRuntime

- preflight: {'ok': True}
- preflight wall time: 1537.3 ms
- attestation differences from the required isolation: {}
- self-tests: oom {'child_exit': None, 'child_signal': 9, 'oom_killed': True, 'timed_out': False}, pids {'child_exit': 4, 'child_signal': None, 'oom_killed': False, 'timed_out': False}
- golden (v1): numbers 28/28, hermetic charts 12/12
- golden (lazy_seaborn): numbers 28/28, hermetic charts 12/12
- fail-closed outcomes: allocate_2_gib=memory_limit/None, chart_not_png=succeeded/decode_failed, chart_symlink=succeeded/symlink, code_error=code_error/None, sleep_past_wall=timed_out/None
- execution latency (container, host wall): {'max_ms': 30156, 'median_ms': 1303.5, 'min_ms': 810, 'n': 30, 'p95_ms': 1574}, {'max_ms': 30206.4, 'median_ms': 1358.0, 'min_ms': 863.3, 'n': 30, 'p95_ms': 1666.0}
- leftovers: {'containers': [], 'workspaces': []}

### Phase F: P5 suite through the real container port (V1)

- expectations met 20/20; clean-run golden answers 28/28; incorrect claims accepted 0; provider calls 0
- wall time 101.5 s; preflight 1605.1 ms

### Phase F: P5 suite through the real container port (lazy-seaborn)

- expectations met 20/20; clean-run golden answers 28/28; incorrect claims accepted 0; provider calls 0
- wall time 68.4 s; preflight 1555.3 ms

Runner account is root-equivalent (passwordless sudo, docker group): this is not VE-2 non-root or cross-account validation.

VERDICT: every stage ran and passed
