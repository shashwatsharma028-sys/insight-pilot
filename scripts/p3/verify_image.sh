#!/usr/bin/env bash
# InsightPilot P3.5b: verify a BUILT inert sandbox image.
#
#   scripts/p3/verify_image.sh IMAGE [--evidence FILE]
#
# Thin wrapper over scripts/p3/rootless.py verify. Needs rootless Podman on
# Linux with cgroup v2 and refuses otherwise. It runs only the inspector
# (passed on stdin) and the image's own inert entrypoint.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "verify_image: REFUSED: $*" >&2; exit 2; }
[ "$(id -u)" != "0" ] || die "must run rootless (not as uid 0)"
exec python3 -I -B "$HERE/rootless.py" verify "$@"
