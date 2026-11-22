#!/usr/bin/env bash
# InsightPilot P3.5b: build the INERT sandbox image (native architecture only).
#
#   scripts/p3/build_image.sh --check-host [--probe]
#   scripts/p3/build_image.sh --wheel-cache DIR [--arch x86_64|aarch64]
#
# Thin wrapper over scripts/p3/rootless.py, where every check and every podman
# argument is defined and tested. Refuses uid 0 here as well. Never run this
# on the examination laptop.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "build_image: REFUSED: $*" >&2; exit 2; }
[ "$(id -u)" != "0" ] || die "must run rootless (not as uid 0); a rootful runtime is not a substitute"
if [ "${1:-}" = "--check-host" ]; then shift; exec env -u DOCKER_HOST -u CONTAINER_HOST python3 -I -B "$HERE/rootless.py" check-host "$@"; fi
exec python3 -I -B "$HERE/rootless.py" build "$@"
