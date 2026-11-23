## P3.6-4B CI image build and real-runtime validation (GitHub ubuntu-24.04, rootless Podman)

commit: d1e208cbccbe8c67e4c540f166b03d50f103b0ca

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
| Phase F: real ContainerRuntime (preflight, isolation, fail-closed, golden numbers and charts) | FAIL (exit 1) |
| Phase F: P5 20-case suite, V1 prelude, real container port | NOT RUN |
| Phase F: P5 20-case suite, lazy-seaborn prelude, real container port | NOT RUN |
| Readiness tests (incl. workflow-policy test) | PASS |

### Build 1

- tag `localhost/insightpilot-sandbox:p3.6-9a355dd6e120-x86_64`
- image ID `cf547ba22cac3ab4871dd5344691c6c6dcdb3f58e8f23a784a5185722c4f1d11`
- digest `sha256:82d2f0f12f5c20b7fdf814c57c94374b56f536a779a347b55c0ddfc91298229f`
- size 589306102 bytes (562.0 MiB)
- build duration 25.0 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `f0f757163f2aa8c864960dd4ca5dd34a86f5494f9754341e2714e071d2c75738` (34 files)
- RootFS layers: 7

### Build 2

- tag `localhost/insightpilot-sandbox:p3.6-9a355dd6e120-x86_64`
- image ID `cf547ba22cac3ab4871dd5344691c6c6dcdb3f58e8f23a784a5185722c4f1d11`
- digest `sha256:82d2f0f12f5c20b7fdf814c57c94374b56f536a779a347b55c0ddfc91298229f`
- size 589306102 bytes (562.0 MiB)
- build duration 20.7 s
- dependency lock SHA-256 `9a355dd6e120ba38d9d91e41bcd52921f3070b5749954cc08635e496aa45e053`
- build context SHA-256 `f0f757163f2aa8c864960dd4ca5dd34a86f5494f9754341e2714e071d2c75738` (34 files)
- RootFS layers: 7

### Verification of build 1

- result: all image (incl. /opt/ip hashes, owners, modes), runtime and no-argument-refusal checks passed (verify exits non-zero otherwise)
- distributions found: 18

### Reproducibility

- same image ID: True
- same RootFS layers: True
- build 1 ID `cf547ba22cac3ab4871dd5344691c6c6dcdb3f58e8f23a784a5185722c4f1d11`, build 2 ID `cf547ba22cac3ab4871dd5344691c6c6dcdb3f58e8f23a784a5185722c4f1d11`

### Start-up (supervisor entrypoint with no arguments: imports, refuses, exits 78; podman client wall clock)

- warm: n=30, median 122.2 ms, p95 135.5 ms (min 110.0, max 136.5)
- cold: n=10, median 126.8 ms, p95 130.3 ms (min 119.8, max 130.3)
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

### Phase F: real ContainerRuntime

- preflight: {'error': 'BootstrapRefused: V2 features were requested that this build cannot provide yet: IP_FF_SANDBOX_HARDENED, IP_SANDBOX_BACKEND. Prerequisites not met: runtime preflight failed (SandboxProtocolError: invalid result frame: the frame is truncated (0 of 4 bytes)). Hardened execution is not available, and the V1 executor is never used as a fallback. Unset these variables to run the V1-compatible app.', 'ok': False}
- preflight wall time: None ms
- attestation differences from the required isolation: None
- self-tests: oom None, pids None
- execution latency (container, host wall): None, None
- leftovers: {'containers': [], 'workspaces': []}

Runner account is root-equivalent (passwordless sudo, docker group): this is not VE-2 non-root or cross-account validation.

VERDICT: NOT all stages passed - see the table
