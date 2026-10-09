#!/usr/bin/env bash
# Build the .deb inside an ubuntu:22.04 container, so the package links against
# 22.04's glibc/libstdc++ and installs there (a newer host would pull in newer
# runtime dependencies than 22.04 has).
#
#   scripts/build-deb.sh [out-dir]      default out-dir: ./dist
#
# The version is the VERSION file (see cmake/Version.cmake): exactly VERSION when
# HEAD is the v<VERSION> tag, otherwise VERSION+git<N>.<hash>. Pass
# CORTRACE_VERSION=1.0.0a1 in the environment to override it.
#
# Speed: the toolchain lives in a local builder image (built once) and the build
# tree in a named docker volume, so a rebuild only recompiles what changed and
# does not reinstall packages or recompile OpenCSD. Set CORTRACE_DEB_CLEAN=1 (CI
# does, via $CI) for a from-scratch build in a throw-away container.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)"
BASE="${CORTRACE_DEB_IMAGE:-ubuntu:22.04}"
PKGS="build-essential cmake git python3 dpkg-dev file ca-certificates"

if [[ -n "${CORTRACE_DEB_CLEAN:-}" || -n "${CI:-}" ]]; then
    IMAGE="$BASE"
    SETUP="apt-get update -qq && apt-get install -y -qq --no-install-recommends $PKGS >/dev/null"
    CACHE=()
else
    IMAGE="cortrace-deb-builder:${BASE//[\/:]/-}"
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "building the builder image $IMAGE (once)..." >&2
        docker build -q -t "$IMAGE" - <<EOF
FROM $BASE
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends $PKGS \
    && rm -rf /var/lib/apt/lists/*
EOF
    fi
    SETUP=true
    CACHE=(-v cortrace-deb-build:/tmp/build)
fi

# git inside the container refuses a repo owned by another uid; the build only
# reads it (to see whether HEAD is the release tag), so mark it safe there.
docker run --rm \
    -v "$ROOT":/src:ro -v "$OUT":/out "${CACHE[@]}" \
    -e CORTRACE_VERSION="${CORTRACE_VERSION:-}" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
    "$IMAGE" bash -euo pipefail -c "
        export DEBIAN_FRONTEND=noninteractive
        $SETUP
        git config --global --add safe.directory /src
        cmake -S /src -B /tmp/build -DCMAKE_BUILD_TYPE=Release \
              -DCORTRACE_OPENCSD_STATIC=ON -DCORTRACE_WERROR=ON \
              \${CORTRACE_VERSION:+-DCORTRACE_VERSION=\$CORTRACE_VERSION}
        cmake --build /tmp/build -j\"\$(nproc)\"
        rm -f /tmp/build/cortrace_*.deb
        (cd /tmp/build && ctest --output-on-failure && cpack -G DEB)
        cp /tmp/build/cortrace_*.deb /out/
        chown \"\$HOST_UID:\$HOST_GID\" /out/cortrace_*.deb
    "
ls -l "$OUT"/cortrace_*.deb
