#!/usr/bin/env bash
# InsightPilot P3.5a: verify a BUILT inert sandbox image (planned for P3.5b).
#
#   scripts/p3/verify_image.sh IMAGE
#
# Needs rootless Podman on Linux with cgroup v2, refuses otherwise. It runs
# only trusted inspection code (image_inspect.py, passed on stdin) and the
# image's own inert entrypoint. It never runs generated code.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
IMAGE_DIR="$ROOT/v2/execution/image"
IMG="${1:?usage: verify_image.sh IMAGE}"
die() { echo "verify_image: REFUSED: $*" >&2; exit 2; }

[ "$(id -u)" != "0" ] || die "must run rootless (not as uid 0)"
command -v podman >/dev/null 2>&1 || die "rootless Podman is required"
[ "$(podman info --format '{{.Host.Security.Rootless}}')" = "true" ] || die "Podman is not rootless"
[ "$(stat -fc %T /sys/fs/cgroup)" = "cgroup2fs" ] || die "cgroup v2 is required for isolation testing"

OUT="$(mktemp -d)"; trap 'rm -rf "$OUT"' EXIT
podman image inspect "$IMG" > "$OUT/inspect.json"
python3 -I -B - "$OUT/inspect.json" "$IMAGE_DIR/image-expectations.json" "$IMAGE_DIR/requirements.lock" <<'PY'
import hashlib, json, sys
img = json.load(open(sys.argv[1]))[0]; exp = json.load(open(sys.argv[2]))
cfg = img["Config"]; problems = []
lock_sha = hashlib.sha256(open(sys.argv[3], "rb").read()).hexdigest()
if cfg.get("User") != exp["user"]: problems.append(f"User {cfg.get('User')!r}")
if cfg.get("Entrypoint") != exp["entrypoint"]: problems.append(f"Entrypoint {cfg.get('Entrypoint')}")
if (cfg.get("Cmd") or []) != exp["cmd"]: problems.append(f"Cmd {cfg.get('Cmd')}")
if cfg.get("ExposedPorts"): problems.append(f"ExposedPorts {cfg.get('ExposedPorts')}")
if cfg.get("Volumes"): problems.append(f"Volumes {cfg.get('Volumes')}")
names = sorted(e.split("=", 1)[0] for e in cfg.get("Env", []))
if names != exp["env_names_allowed"]: problems.append(f"Env names {names}")
labels = cfg.get("Labels") or {}
if labels.get("io.insightpilot.lock.sha256") != lock_sha: problems.append("lock label mismatch")
print("\n".join(problems) if problems else "config OK"); sys.exit(1 if problems else 0)
PY
RUNFLAGS=(--rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges
          --user 10001:10001 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m)
podman run "${RUNFLAGS[@]}" -i --entrypoint /usr/local/bin/python3 "$IMG" -I -B - \
  < "$HERE/image_inspect.py" > "$OUT/report.json"
python3 -I -B "$HERE/image_inspect.py" evaluate "$OUT/report.json" "$IMAGE_DIR/image-expectations.json" \
  "$IMAGE_DIR/dependency-inventory.json"
set +e
podman run "${RUNFLAGS[@]}" "$IMG" > "$OUT/inert.out" 2> "$OUT/inert.err"; status=$?
set -e
[ "$status" = "78" ] || die "inert entrypoint exit status $status (expected 78)"
[ ! -s "$OUT/inert.out" ] || die "inert entrypoint wrote to stdout"
echo "inert entrypoint OK: $(cat "$OUT/inert.err")"
echo "verify_image: all checks passed for $IMG"
