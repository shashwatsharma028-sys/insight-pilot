# P3.6-3 fake-runtime demonstration (no real container; generated code not executed)

ContainerRuntime (real class; test double flag + reviewed host facts only) against the fake podman.

## 1. Preflight

| # | podman call | detail |
|---|---|---|
| 1 | `version --format` |  |
| 2 | `info --format` |  |
| 3 | `image inspect` |  |
| 4 | `run --rm` | attest-only |
| 5 | `rm --force` |  |
| 6 | `container exists` |  |
| 7 | `run --rm` | selftest-oom |
| 8 | `rm --force` |  |
| 9 | `container exists` |  |
| 10 | `run --rm` | selftest-pids |
| 11 | `rm --force` |  |
| 12 | `container exists` |  |

## 2. One execution

Exact `podman run` argv (paths shortened):

```
--rm
  --name ip-1762f3bd-6436c3749b68
  --label org.insightpilot.instance=<instance>
  --label org.insightpilot.run=6436c3749b685927fbf6a422207ced90
  --label org.insightpilot.schema=1
  --pull=never
  --image-volume=ignore
  --read-only
  --read-only-tmpfs=false
  --tmpfs /tmp:rw,size=67108864,nr_inodes=4096,mode=1777,noexec,nosuid,nodev
  --tmpfs /out:rw,size=33554432,nr_inodes=64,mode=1777,noexec,nosuid,nodev
  --mount type=bind,source=<state>/root/instances/<instance>/runs/6436c3749b685927fbf6a422207ced90/in,destination=/in,readonly=true
  --network=none
  --dns=none
  --no-hosts
  --http-proxy=false
  --user 10001:10001
  --passwd=false
  --cap-drop=ALL
  --security-opt=no-new-privileges
  --ipc=none
  --cgroupns=private
  --log-driver=passthrough
  --env-host=false
  --memory=1073741824
  --memory-swap=1073741824
  --cpus=1
  --pids-limit=64
  --ulimit=nofile=256:256
  --ulimit=core=0
  --timeout=45
  --env LANG=C.UTF-8
  --env TZ=UTC
  --env OPENBLAS_NUM_THREADS=1
  --env OMP_NUM_THREADS=1
  --env MKL_NUM_THREADS=1 sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  --mode run
  --run-id 6436c3749b685927fbf6a422207ced90
  --prelude lazy_seaborn
  --wall 30
  --stdout-cap 1048576
  --stderr-head 131072
  --stderr-tail 131072
  --chart-cap 10485760
  --max-side 8192
  --max-pixels 16777216
  --memory-max 1073741824
  --pids-max 64
```

- workspace inputs seen by the container: ['code.py', 'dataset.csv']
- staged dataset SHA-256: `3259adb09dbbedeafd91802f2285dcf3a89cde407088387e495d7dea99b4fc7f` (pin `3259adb09dbbedeafd91802f2285dcf3a89cde407088387e495d7dea99b4fc7f`; equal: True)
- result: outcome=succeeded, isolation=rootless_container, run_id=1234567890abcdef1234567890abcdef
- stdout: 'CLAIM row_count=500\nCLAIM mean_score=71.4'
- chart: <state>/charts/stu_t1_a1-1234567890abcdef1234567890abcdef.png (re-encoded, published without overwrite)
- calls after the run: `rm --force`, `container exists`
- containers left: []; workspaces left: []

## 3. Failures fail closed

- forged frame -> SandboxProtocolError: invalid result frame: header length 543584114 is outside 1..65536; containers left [], workspaces left []
- container reporting a network interface -> SandboxUnavailableError: sandbox attestation does not match the required isolation: net_interfaces
