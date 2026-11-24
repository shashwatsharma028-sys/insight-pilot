## P3.6-4B CI image build and real-runtime validation (GitHub ubuntu-24.04, rootless Podman)

commit: 5868f46360c0947b3171e4d12032caccd9c1405b

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
| Phase F: real ContainerRuntime (preflight, isolation, fail-closed, golden numbers and charts) | FAIL (exit 1) |
| Phase F: P5 20-case suite, V1 prelude, real container port | NOT RUN |
| Phase F: P5 20-case suite, lazy-seaborn prelude, real container port | NOT RUN |
| Readiness tests (incl. workflow-policy test) | PASS |

### Build 1

- tag `localhost/insightpilot-sandbox:p3.6-9a355dd6e120-x86_64`
- image ID `bb92b2ddb0d2933a4f28d8fb69783565ddc590036b1e2e35bd6a0b3724224ed9`
- digest `sha256:cfa84a4d356519f49c5bd2dff3bf4ce2b3c764ddd00e6e60f298fc2f887298d1`
- size 589306614 bytes (562.0 MiB)
- build duration 24.5 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `79db48374fee67269a9ca6413949caf6b7c0af2237b0ec845aa4554ac93c1d8f` (34 files)
- RootFS layers: 7

### Build 2

- tag `localhost/insightpilot-sandbox:p3.6-9a355dd6e120-x86_64`
- image ID `bb92b2ddb0d2933a4f28d8fb69783565ddc590036b1e2e35bd6a0b3724224ed9`
- digest `sha256:cfa84a4d356519f49c5bd2dff3bf4ce2b3c764ddd00e6e60f298fc2f887298d1`
- size 589306614 bytes (562.0 MiB)
- build duration 20.5 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `79db48374fee67269a9ca6413949caf6b7c0af2237b0ec845aa4554ac93c1d8f` (34 files)
- RootFS layers: 7

### Verification of build 1

- result: all image (incl. /opt/ip hashes, owners, modes), runtime and no-argument-refusal checks passed (verify exits non-zero otherwise)
- distributions found: 18

### Reproducibility

- same image ID: True
- same RootFS layers: True
- build 1 ID `bb92b2ddb0d2933a4f28d8fb69783565ddc590036b1e2e35bd6a0b3724224ed9`, build 2 ID `bb92b2ddb0d2933a4f28d8fb69783565ddc590036b1e2e35bd6a0b3724224ed9`

### Start-up (supervisor entrypoint with no arguments: imports, refuses, exits 78; podman client wall clock)

- warm: n=30, median 128.0 ms, p95 134.9 ms (min 118.4, max 135.6)
- cold: n=10, median 125.4 ms, p95 134.0 ms (min 114.1, max 134.0)
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

- preflight: {'error': 'BootstrapRefused: V2 features were requested that this build cannot provide yet: IP_FF_SANDBOX_HARDENED, IP_SANDBOX_BACKEND. Prerequisites not met: runtime preflight failed (SandboxUnavailableError: sandbox attestation does not match the required isolation: writable_mounts). Hardened execution is not available, and the V1 executor is never used as a fallback. Unset these variables to run the V1-compatible app.', 'ok': False}
- preflight wall time: None ms
- attestation differences from the required isolation: {'writable_mounts': {'observed': ['/dev/full', '/dev/mqueue', '/dev/null', '/dev/pts', '/dev/random', '/dev/tty', '/dev/urandom', '/dev/zero', '/out', '/tmp'], 'required': ['/out', '/tmp']}}
- self-tests: oom {'child_exit': None, 'child_signal': 9, 'oom_killed': True, 'timed_out': False}, pids {'child_exit': 4, 'child_signal': None, 'oom_killed': False, 'timed_out': False}
- execution latency (container, host wall): None, None
- leftovers: {'containers': [], 'workspaces': []}

Runner account is root-equivalent (passwordless sudo, docker group): this is not VE-2 non-root or cross-account validation.

VERDICT: NOT all stages passed - see the table
