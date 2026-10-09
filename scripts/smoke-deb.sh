#!/usr/bin/env bash
# Install a cortrace .deb into a clean ubuntu:22.04 container and exercise it as
# an ordinary (non-root) user: versions, imports, no missing shared libraries,
# the Perfetto Record target answering a WebSocket handshake (and refusing a
# foreign Origin), and an unprivileged cortrace-grab start-up.
#
#   scripts/smoke-deb.sh path/to/cortrace_X_amd64.deb [expected-version]
set -euo pipefail
DEB="$(readlink -f "${1:?usage: $0 cortrace.deb [expected-version]}")"
IMAGE="${CORTRACE_DEB_IMAGE:-ubuntu:22.04}"
WANT="${2:-$(dpkg-deb -f "$DEB" Version)}"

docker run --rm -v "$DEB":/pkg/cortrace.deb:ro -e WANT="$WANT" "$IMAGE" bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq /pkg/cortrace.deb >/dev/null
    useradd -m tester
    run() { su tester -c "$*"; }

    echo "== versions"
    [ "$(run cortrace version)" = "cortrace $WANT" ]
    run cortrace --help | grep -q "^  serve "
    run cortrace-decode --version
    run "cortrace-grab 2>&1 | grep -q usage"

    echo "== no missing shared libraries"
    ! ldd /usr/bin/cortrace-decode /usr/bin/cortrace-grab | grep "not found"
    [ -z "$(ls /usr/lib/cortrace 2>/dev/null)" ]   # OpenCSD is linked statically

    echo "== python package"
    run "python3 -c \"import cortrace.cli, cortrace.capture, cortrace.serve, cortrace.fuse, cortrace.fpga.health, cortrace.fpga.ctrl; print(cortrace.__version__)\""

    echo "== output directory is never defaulted"
    out="$(run "cortrace capture --elf /bin/true --iface lo 2>&1" || true)"
    echo "$out" | grep -q "out-dir is required"

    echo "== perfetto record target: websocket handshake + origin check"
    run "cortrace serve --out-dir /tmp/o --elf /bin/true --iface lo --port 18037 --quiet" &
    SERVER=$!
    for _ in $(seq 1 50); do
        python3 -c "import socket; socket.create_connection((\"127.0.0.1\", 18037), 1)" 2>/dev/null && break
        sleep 0.2
    done
    python3 - <<PY
import socket
def handshake(origin):
    s = socket.create_connection(("127.0.0.1", 18037), 5)
    s.sendall((
        "GET /traced HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Origin: " + origin + "\r\n\r\n").encode())
    return s.recv(200).split(b"\r\n")[0].decode()
assert handshake("https://ui.perfetto.dev").startswith("HTTP/1.1 101"), "perfetto UI refused"
assert handshake("https://evil.example").startswith("HTTP/1.1 403"), "foreign origin accepted"
print("handshake ok")
PY
    kill $SERVER 2>/dev/null || true

    echo "== remove leaves nothing behind"
    apt-get remove -y -qq cortrace >/dev/null
    [ ! -e /usr/bin/cortrace ] && [ ! -d /usr/lib/python3/dist-packages/cortrace ]
    echo SMOKE-OK
'
