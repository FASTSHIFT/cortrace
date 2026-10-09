#!/usr/bin/env bash
# Build the .deb inside an ubuntu:22.04 container, so the package links against
# 22.04's glibc/libstdc++ and installs there (a newer host would pull in newer
# runtime dependencies than 22.04 has).
#
#   scripts/build-deb.sh [out-dir]      default out-dir: ./dist
#
# The version comes from the git tag (see cmake/Packaging.cmake); pass
# CORTRACE_VERSION=1.2.3 in the environment to override it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)"
IMAGE="${CORTRACE_DEB_IMAGE:-ubuntu:22.04}"

# git inside the container refuses a repo owned by another uid; the build only
# reads it (for `git describe`), so mark it safe there.
docker run --rm \
    -v "$ROOT":/src:ro -v "$OUT":/out \
    -e CORTRACE_VERSION="${CORTRACE_VERSION:-}" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "$IMAGE" bash -euo pipefail -c '
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            build-essential cmake git python3 dpkg-dev file ca-certificates >/dev/null
        git config --global --add safe.directory /src
        cmake -S /src -B /tmp/build -DCMAKE_BUILD_TYPE=Release \
              -DCORTRACE_OPENCSD_STATIC=ON \
              ${CORTRACE_VERSION:+-DCORTRACE_VERSION=$CORTRACE_VERSION}
        cmake --build /tmp/build -j"$(nproc)"
        (cd /tmp/build && ctest --output-on-failure && cpack -G DEB)
        cp /tmp/build/cortrace_*.deb /out/
        chown "$HOST_UID:$HOST_GID" /out/cortrace_*.deb
    '
ls -l "$OUT"/cortrace_*.deb
