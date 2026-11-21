#!/usr/bin/env bash
# InsightPilot P3.5a: reproducible build of the INERT sandbox image.
#
#   scripts/p3/build_image.sh --check-host
#       Report the host's container capabilities and exit. Builds nothing.
#   scripts/p3/build_image.sh --wheel-cache DIR [--arch x86_64|aarch64]
#       Stage the allowlisted context, then build with rootless Podman.
#
# Refuses to run as root, through a rootful or remote daemon, or with Docker.
# The build itself runs with --network=none: every input is in the staged
# context or is the digest-pinned base image (pulled by digest beforehand).
# Never run this on the examination laptop.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
IMAGE_DIR="$ROOT/v2/execution/image"
BASE_REF="docker.io/library/python@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39"

die() { echo "build_image: REFUSED: $*" >&2; exit 2; }

check_host() {
  local ok=1
  echo "uid: $(id -u)"
  if [ "$(id -u)" = "0" ]; then echo "rootless: NO (running as root)"; ok=0; fi
  if command -v podman >/dev/null 2>&1; then
    echo "podman: $(podman --version 2>/dev/null || echo unavailable)"
    local rl; rl="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo error)"
    echo "podman rootless: $rl"; [ "$rl" = "true" ] || ok=0
    echo "podman remote: $(podman info --format '{{.Host.ServiceIsRemote}}' 2>/dev/null || echo error)"
    echo "podman cgroup: $(podman info --format '{{.Host.CgroupsVersion}} {{.Host.CgroupManager}}' 2>/dev/null || echo error)"
  else
    echo "podman: not installed"; ok=0
  fi
  if [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = "cgroup2fs" ]; then echo "cgroup: v2"; else echo "cgroup: NOT v2"; ok=0; fi
  echo "max_user_namespaces: $(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo unknown)"
  echo "subuid entry for $(id -un): $(grep -c "^$(id -un):" /etc/subuid 2>/dev/null || true)"
  [ "$ok" = "1" ] && echo "host suitable for P3.5b build and isolation tests: YES" \
                  || echo "host suitable for P3.5b build and isolation tests: NO"
  [ "$ok" = "1" ]
}

ARCH="$(uname -m)"; CACHE=""
case "${1:-}" in
  --check-host) check_host; exit $? ;;
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --wheel-cache) CACHE="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    *) die "unknown argument $1" ;;
  esac
done

[ "$(id -u)" != "0" ] || die "must run rootless (not as uid 0); a rootful runtime is not a substitute"
[ -z "${DOCKER_HOST:-}${CONTAINER_HOST:-}" ] || die "remote container hosts are not allowed"
command -v podman >/dev/null 2>&1 || die "rootless Podman is required"
[ "$(podman info --format '{{.Host.Security.Rootless}}')" = "true" ] || die "Podman is not rootless"
[ "$(podman info --format '{{.Host.ServiceIsRemote}}')" = "false" ] || die "remote Podman service"
[ -n "$CACHE" ] || die "--wheel-cache DIR is required (fill it with image_tool.py fetch-wheels)"

LOCK_SHA="$(sha256sum "$IMAGE_DIR/requirements.lock" | cut -c1-64)"
grep -q "io.insightpilot.lock.sha256=\"$LOCK_SHA\"" "$IMAGE_DIR/Containerfile" \
  || die "Containerfile lock label does not match requirements.lock"

CTX="$(mktemp -d)"; trap 'rm -rf "$CTX"' EXIT
python3 -I -B "$HERE/image_tool.py" stage --arch "$ARCH" --cache "$CACHE" --out "$CTX/context"
podman pull "$BASE_REF"
TAG="localhost/insightpilot-sandbox:p3.5a-${LOCK_SHA:0:12}-$ARCH"
podman build --pull=never --network=none --no-cache --timestamp 0 \
  --file "$CTX/context/Containerfile" --tag "$TAG" "$CTX/context"
podman image inspect --format '{{.Id}} {{.Size}}' "$TAG"
echo "built $TAG"
