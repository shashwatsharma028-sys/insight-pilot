# InsightPilot V2 — P3.5b validation environment (VE-2) setup and run guide

**Status:** this guide has NOT been executed. P3.5b Phases C–E (image build, reproducibility, start-up, golden compatibility inside the image, OS vulnerability review) are **incomplete**. They need the environment described here. The cloud design session runs as uid 0, with cgroup v1 and no Podman, so it is **not** a validation environment. `scripts/p3/rootless.py check-host` refuses it, as tested.

**Where this must run:**
- a dedicated, reviewer-controlled Ubuntu 24.04 VM;
- **never** the examination laptop;
- **never** the cloud design session;
- **never** on cloud resources that cost money unless the reviewer has explicitly approved them.

VE-1 (GitHub Actions) needs its own approval and is not covered here.

> **Update (P3.6-0, 2026-11-22):** VE-1 was later approved and run.
> - GitHub run #4 at `4cd107e0` passed every stage for the **inert** image. The evidence is in `docs/v2/p3/ci_run4/`.
> - The VE-1 account is root-equivalent, so it does **not** satisfy any VE-2 requirement in this guide.
> - This guide has still not been executed.

## 1. Minimum requirements

| Item | Requirement |
|---|---|
| Hypervisor | Any that runs a full Linux kernel with systemd as PID 1: KVM/libvirt, VirtualBox, VMware, Hyper-V or UTM. **Not** WSL2, a container, `podman machine`, Docker Desktop or any remote Podman service (refused as remote) |
| Guest OS | Ubuntu Server 24.04 LTS (noble). Kernel ≥ 6.8, systemd, unified cgroup v2 hierarchy (the 24.04 default) |
| Architecture | x86_64 (amd64), built **natively**. An arm64 VM is acceptable only as a separate native-arm64 run with its own evidence; **cross-architecture builds are refused** |
| CPU / RAM / disk | ≥ 2 vCPU (4 recommended), ≥ 4 GiB RAM (8 recommended), ≥ 30 GiB free disk |
| Network | Outbound HTTPS to `registry-1.docker.io`, `auth.docker.io`, the Docker Hub blob CDN, `pypi.org` and `files.pythonhosted.org`, and to `github.com` to fetch the repository. **Builds and test containers themselves run with `--network=none`** |
| Software | `podman` ≥ 4.4 (noble ships 4.9.x), `crun`, `uidmap` (newuidmap/newgidmap), `passt`, `git`, `python3` (3.12 is fine for the driver scripts) |
| Not installed | Docker, rootful Podman services, any container daemon. No `docker` group membership |
| Credentials | None on the VM: no API keys, no `.env`, no cloud credentials. The tests use mock LLMs only |

## 2. VM setup (once, as the VM administrator)

```bash
sudo apt-get update
```
```bash
sudo apt-get install -y --no-install-recommends podman crun uidmap passt git python3 python3-venv
```

Optional but recommended for reproducibility: install from a fixed Ubuntu snapshot. On noble, apt supports `--snapshot`. Record the timestamp you use, for example:
```bash
sudo apt-get install -y --snapshot 20261115T000000Z --no-install-recommends podman crun uidmap passt git python3 python3-venv
```

Two unprivileged accounts, no `sudo`, no `docker` group:
- `ipval` runs the validation;
- `ipprobe` is the second account for cross-account probes.

```bash
sudo useradd --create-home --shell /bin/bash ipval
```
```bash
sudo useradd --create-home --shell /bin/bash ipprobe
```
```bash
sudo chmod 0750 /home/ipval /home/ipprobe
```
```bash
grep -E '^(ipval|ipprobe):' /etc/subuid /etc/subgid
```
`useradd` on 24.04 allocates non-overlapping 65536-ID ranges. If a line is missing:
```bash
sudo usermod --add-subuids 200000-265535 --add-subgids 200000-265535 ipval
```

Persistent user services and cgroup delegation:
```bash
sudo loginctl enable-linger ipval
```
```bash
cat /sys/fs/cgroup/user.slice/user-$(id -u ipval).slice/user@$(id -u ipval).service/cgroup.controllers
```
The output must include `cpu memory pids`. If it does not, delegate them explicitly and reboot:
```bash
sudo mkdir -p /etc/systemd/system/user@.service.d && printf '[Service]\nDelegate=cpu cpuset io memory pids\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf && sudo systemctl daemon-reload
```

**AppArmor:** Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns=1`, and the podman package ships an AppArmor profile that allows Podman's user namespaces. If the enforcement probe in step 4 fails with a user-namespace permission error, record it and stop. Do **not** disable the restriction system-wide without reviewer approval.

## 3. Get the exact commit (as `ipval`)

Log in through a real user session so that `XDG_RUNTIME_DIR` and the systemd user manager exist. `su` and `sudo -u` do not create one.
```bash
sudo machinectl shell ipval@
```
```bash
git clone https://github.com/shashwatsharma028-sys/insight-pilot.git ~/insight-pilot && cd ~/insight-pilot && git checkout --detach <REVIEWED_SHA> && git rev-parse HEAD
```
The repository may be private. In that case, use a read-only deploy key or a reviewer-provided archive of that commit; never a personal token stored on the VM. Then check that there is nothing to leak:
```bash
env | grep -iE 'key|token|secret|proxy' ; ls -a ~ | grep -i env
```
Both should print nothing relevant. Remove anything they find before continuing.

## 4. Environment gate (must pass before any build)

```bash
mkdir -p ~/evidence && python3 -I -B scripts/p3/rootless.py check-host | tee ~/evidence/01-host-facts.txt
```
```bash
python3 -I -B scripts/p3/rootless.py check-host --probe | tee ~/evidence/02-host-probe.txt
```

**What `--probe` does:**
- pulls the digest-pinned base `python:3.11.18-slim-bookworm@sha256:a83ba18a…` for the native platform;
- checks that the pulled config digest is the verified one;
- starts a real container with the production isolation options: `--network=none --ipc=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --user 10001:10001 --http-proxy=false --env-host=false`, cgroup limits and a bounded `noexec` `/tmp` tmpfs;
- requires the container to report each property **as enforced**: uid/gid 10001 in a subordinate range, NoNewPrivs=1, all five capability sets empty, seccomp mode 2, only `lo` with no routes, a read-only root, the expected mounts only, `/tmp` the only writable path, memory/pids/cpu limits visible in the container's cgroup, and no proxy or secret environment variables.

**Stop on any `REFUSED`.** Record the output, and do not work around it.

## 5. Build (Phase C)

```bash
python3 -I -B scripts/p3/image_tool.py fetch-wheels --arch "$(uname -m)" --cache ~/ip-wheels | tee ~/evidence/03-wheels.txt
```
```bash
scripts/p3/build_image.sh --wheel-cache ~/ip-wheels | tee ~/evidence/04-build1.json
```
```bash
scripts/p3/verify_image.sh "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)" --evidence ~/evidence/05-verify.json
```

**What `verify` checks:**
- **image config:** platform, `User 10001:10001`, the inert entrypoint, empty `Cmd`, no ports or volumes, exact environment names, exact labels with lock and base digests;
- **image contents:** no setid, world-writable or secret-like files; no `.git` directories, virtual environments, pip, setuptools, wheel, compilers, curl or wget; exactly the 18 locked distributions; exactly `/opt/ip/inert_entrypoint.py`;
- **runtime-enforced properties:** as in the probe;
- **the inert entrypoint:** exit status 78, nothing on stdout, and the exact message on stderr.

## 6. Reproducibility (Phase D)

```bash
podman tag "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)" localhost/ip-build1 && podman rmi "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)"
```
```bash
scripts/p3/build_image.sh --wheel-cache ~/ip-wheels | tee ~/evidence/06-build2.json
```
```bash
for i in localhost/ip-build1 "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)"; do podman image inspect --format '{{.Id}} {{.Digest}} {{.Size}} {{json .RootFS.Layers}}' "$i"; done | tee ~/evidence/07-compare.txt
```

If the IDs or layer digests differ, save both images and compare the layer contents to explain the difference. Do not hide it.
```bash
mkdir -p ~/cmp && podman save --format oci-dir -o ~/cmp/b1 localhost/ip-build1 && podman save --format oci-dir -o ~/cmp/b2 "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)"
```
Then, for each layer pair: `tar -tvf` both, and `diff` the listings (names, modes, owners, mtimes).

## 7. Start-up, golden compatibility, OS packages (Phases D and E)

```bash
python3 -I -B scripts/p3/rootless.py measure-startup "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)" --warm 30 --cold 10 --evidence ~/evidence/08-startup.json
```
```bash
python3 -I -B scripts/p3/rootless.py golden "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)" --evidence ~/evidence/09-golden.json
```
```bash
python3 -I -B scripts/p3/rootless.py os-packages "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)" --evidence ~/evidence/10-os-packages.json
```

**Start-up samples:**
- *warm* samples are consecutive runs;
- each *cold* sample follows `podman rmi` plus `podman load`;
- the page cache is not dropped (that is impossible rootless);
- the output reports the sample count, median and p95 (nearest rank) for each.

**Golden run:** only the hash-pinned fixtures in `scripts/p3/golden_fixtures.json`, on hash-checked dataset copies.
- **Mounts:** dataset copies read-only at `/in/dataset.csv`; outputs on a 64 MiB `noexec` tmpfs at `/out`; Matplotlib's `MPLCONFIGDIR` on the `/tmp` tmpfs with a read-only root.
- **Compared:** the answers against the committed golden values, and each chart's decoded pixels against the P3.5a host reference.

**Vulnerability scan (reviewer chooses the scanner):**
1. Download the scanner and its database with network access, and record the scanner version and the database timestamp.
2. Scan offline:
   ```bash
   podman save -o ~/cmp/image.tar "localhost/insightpilot-sandbox:p3.5a-9a355dd6e120-$(uname -m)"
   ```
   Then run, for example, `trivy image --input ~/cmp/image.tar --skip-db-update --offline-scan`, or `grype docker-archive:$HOME/cmp/image.tar` after `grype db status`.
3. Record every finding and a remediation decision.
4. Never report "no vulnerabilities". Report "no findings by <scanner> <version>, DB <timestamp>".

## 8. Non-root regression suite and cross-account probes (VE-2 requirement)

The full offline suite must also pass as `ipval` (environment B pins, Python 3.11.18, see `docs/v2/BASELINE.md`):
```bash
python3 -m venv ~/venv-uv && ~/venv-uv/bin/pip install uv && ~/venv-uv/bin/uv python install 3.11.18 && ~/venv-uv/bin/uv venv -p 3.11.18 ~/venvB && ~/venv-uv/bin/uv pip install -p ~/venvB/bin/python -r docs/v2/ENV_B_PINS.txt
```
```bash
env -u GOOGLE_API_KEY PYTHONDONTWRITEBYTECODE=1 ~/venvB/bin/python -m pytest tests -q -p no:cacheprovider -rxXfE | tee ~/evidence/11-suite-ipval.txt
```

Cross-account probes as `ipprobe`, in a separate `machinectl shell ipprobe@` session. Every command must fail or show nothing of `ipval`'s:
```bash
ls /home/ipval ; ls /home/ipval/.local/share/containers ; podman ps -a ; podman images
```

## 9. Evidence and teardown

```bash
{ uname -a; cat /etc/os-release; podman version; podman info --format json; git -C ~/insight-pilot rev-parse HEAD; sha256sum ~/evidence/*; } > ~/evidence/00-environment.txt
```

Hand `~/evidence/` to the reviewer. It contains no secrets: check with `grep -riE 'key|token|secret' ~/evidence` and remove anything unexpected. Then:
```bash
podman system reset --force
```
Delete the VM when the review is done.

**Claim rule:** P3.5b can be reported complete only if every step above ran and passed, with evidence files. A `REFUSED`, a skipped step or a missing file means it is incomplete.
