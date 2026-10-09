import os
import socket
import struct
import tempfile
import threading

import pytest
from cortrace import wsrelay as ws

KEY = "dGhlIHNhbXBsZSBub25jZQ=="  # RFC 6455 section 1.3 example
MASK = b"\x01\x02\x03\x04"


def test_accept_key_matches_rfc_example():
    assert ws.accept_key(KEY) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize(
    "origin,ok",
    [
        (None, True),
        ("https://ui.perfetto.dev", True),
        ("http://localhost:10000", True),
        ("http://127.0.0.1:9001", True),
        ("http://ui.perfetto.dev", False),
        ("https://evil.example", False),
        ("https://ui.perfetto.dev.evil.example", False),
        ("null", False),
    ],
)
def test_origin_allow_list(origin, ok):
    assert ws.origin_allowed(origin) is ok


def test_origin_extra_allow():
    assert ws.origin_allowed("https://ide.example", extra=("https://ide.example",))


@pytest.mark.parametrize("size", [0, 5, 125, 126, 65535, 65536, 70000])
def test_frame_roundtrip_all_length_encodings(size):
    payload = bytes(range(256)) * (size // 256 + 1)
    payload = payload[:size]
    a, b = socket.socketpair()
    a.sendall(ws.encode_frame(ws.OP_BINARY, payload, mask_key=MASK))
    assert ws.read_frame(b) == (ws.OP_BINARY, True, payload)
    a.sendall(ws.encode_frame(ws.OP_TEXT, payload))  # unmasked (server style)
    assert ws.read_frame(b) == (ws.OP_TEXT, True, payload)
    a.close()
    assert ws.read_frame(b) is None
    b.close()


def test_read_frame_truncated_inputs_return_none():
    for cut in (1, 3, 5):
        a, b = socket.socketpair()
        a.sendall(ws.encode_frame(ws.OP_BINARY, b"x" * 200, mask_key=MASK)[:cut])
        a.close()
        assert ws.read_frame(b) is None
        b.close()


class Consumer:
    """UNIX-socket server standing in for the fake traced: echoes with a prefix."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.path = os.path.join(self.dir, "consumer")
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(4)
        self.received = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        while True:
            data = conn.recv(65536)
            if not data:
                return
            self.received.append(data)
            conn.sendall(b"echo:" + data)

    def close(self):
        self.srv.close()
        os.unlink(self.path)
        os.rmdir(self.dir)


@pytest.fixture
def relay():
    consumer = Consumer()
    server = ws.WsRelay(consumer.path, port=0).start()
    yield server, consumer
    server.stop()
    consumer.close()


def connect(port, *, path="/traced", origin=None, key=KEY, upgrade=True, version="13"):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    if upgrade:
        lines += ["Upgrade: websocket", "Connection: Upgrade"]
    if key:
        lines.append(f"Sec-WebSocket-Key: {key}")
    lines.append(f"Sec-WebSocket-Version: {version}")
    if origin:
        lines.append(f"Origin: {origin}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return sock


def status_line(sock):
    data = b""
    while b"\r\n" not in data:
        data += sock.recv(1)
    return data.split(b"\r\n")[0].decode()


def read_headers(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        data += sock.recv(1)
    return data.decode()


def test_handshake_and_bidirectional_relay(relay):
    server, consumer = relay
    sock = connect(server.port, origin="https://ui.perfetto.dev")
    head = read_headers(sock)
    assert head.startswith("HTTP/1.1 101")
    assert "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head
    sock.sendall(ws.encode_frame(ws.OP_BINARY, b"hello", mask_key=MASK))
    assert ws.read_frame(sock) == (ws.OP_BINARY, True, b"echo:hello")
    # fragments are forwarded in order
    sock.sendall(
        bytes([ws.OP_BINARY]) + bytes([0x80 | 2]) + MASK
        + bytes(b ^ MASK[i % 4] for i, b in enumerate(b"ab"))
    )  # fmt: skip
    sock.sendall(ws.encode_frame(ws.OP_CONT, b"cd", mask_key=MASK))
    got = b""
    while b"cd" not in b"".join(consumer.received):
        got += ws.read_frame(sock)[2]
    assert b"".join(consumer.received).endswith(b"cd")
    sock.close()


def test_large_payload_is_relayed_intact(relay):
    server, consumer = relay
    sock = connect(server.port)
    read_headers(sock)
    payload = os.urandom(200_000)
    sock.sendall(ws.encode_frame(ws.OP_BINARY, payload, mask_key=MASK))
    deadline_bytes = len(payload) + 5 * 4  # allow several echo chunks
    echoed = b""
    while len(b"".join(consumer.received)) < len(payload) or len(echoed) < len(payload):
        echoed += ws.read_frame(sock)[2]
        assert len(echoed) <= deadline_bytes * 2
    assert b"".join(consumer.received) == payload
    sock.close()


def test_ping_is_answered_with_pong(relay):
    server, _ = relay
    sock = connect(server.port)
    read_headers(sock)
    sock.sendall(ws.encode_frame(ws.OP_PING, b"are-you-there", mask_key=MASK))
    assert ws.read_frame(sock) == (ws.OP_PONG, True, b"are-you-there")
    sock.close()


def test_close_frame_is_echoed_and_connection_ends(relay):
    server, _ = relay
    sock = connect(server.port)
    read_headers(sock)
    sock.sendall(ws.encode_frame(ws.OP_CLOSE, struct.pack(">H", 1000), mask_key=MASK))
    opcode, _, payload = ws.read_frame(sock)
    assert opcode == ws.OP_CLOSE and payload == struct.pack(">H", 1000)
    assert sock.recv(1) == b""
    sock.close()


@pytest.mark.parametrize(
    "kwargs,status",
    [
        ({"path": "/adb"}, "HTTP/1.1 404 Not Found"),
        ({"origin": "https://evil.example"}, "HTTP/1.1 403 Forbidden"),
        ({"upgrade": False}, "HTTP/1.1 400 Bad Request"),
        ({"key": None}, "HTTP/1.1 400 Bad Request"),
        ({"version": "8"}, "HTTP/1.1 400 Bad Request"),
    ],
)
def test_rejected_requests(relay, kwargs, status):
    server, _ = relay
    sock = connect(server.port, **kwargs)
    assert status_line(sock) == status
    sock.close()


def test_unreachable_consumer_closes_with_1011():
    server = ws.WsRelay("/tmp/definitely-not-a-socket-xyz", port=0).start()
    try:
        sock = connect(server.port)
        read_headers(sock)
        opcode, _, payload = ws.read_frame(sock)
        assert opcode == ws.OP_CLOSE and struct.unpack(">H", payload)[0] == 1011
        sock.close()
    finally:
        server.stop()


def test_consumer_hangup_ends_the_websocket(relay):
    server, consumer = relay
    sock = connect(server.port)
    read_headers(sock)
    consumer.srv.close()  # no more accepts; existing echo still runs
    sock.shutdown(socket.SHUT_WR)
    sock.settimeout(5)
    # the relay tears the connection down once the client half-closes
    assert sock.recv(1) in (b"", b"\x88")
    sock.close()


def test_garbage_request_is_dropped(relay):
    server, _ = relay
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    sock.sendall(b"\x00\x01garbage\r\n\r\n")
    sock.settimeout(5)
    assert sock.recv(1) in (b"", b"H")  # closed or an HTTP error reply
    sock.close()
