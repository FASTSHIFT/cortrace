"""WebSocket <-> UNIX-socket relay for the Perfetto UI (standard library only).

The Perfetto web UI talks to a local "traced" through
ws://127.0.0.1:<port>/traced. This relay accepts that WebSocket and forwards the
bytes verbatim to the consumer UNIX socket of the fake traced
(record_bridge.py), and back. It replaces tracebox's websocket_bridge so the
.deb needs nothing but python3.

Only what the UI needs is implemented: the RFC 6455 opening handshake, binary
and text data frames (fragments are forwarded in order), ping/pong and close.

Security: the server binds loopback only and checks the browser's Origin header
against an allow-list (ui.perfetto.dev and localhost). Without that check any
web page you visit could connect to ws://127.0.0.1 and start captures.
"""

import base64
import hashlib
import logging
import socket
import struct
import threading
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_HEADER = 64 * 1024
RELAY_CHUNK = 65536
ALLOWED_PATHS = ("/traced", "/")


def accept_key(key):
    """Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def origin_allowed(origin, extra=()):
    """True if a browser Origin may use the relay. A missing Origin (non-browser
    client such as a script) is allowed; browsers always send one."""
    if not origin:
        return True
    if origin in extra:
        return True
    url = urlparse(origin)
    if url.scheme == "https" and url.hostname == "ui.perfetto.dev":
        return True
    return url.scheme in ("http", "https") and url.hostname in (
        "localhost",
        "127.0.0.1",
    )


def encode_frame(opcode, payload=b"", mask_key=None):
    """One FIN frame. Servers send unmasked; pass mask_key to build client frames."""
    head = bytearray([0x80 | opcode])
    mask_bit = 0x80 if mask_key else 0
    size = len(payload)
    if size < 126:
        head.append(mask_bit | size)
    elif size < 1 << 16:
        head.append(mask_bit | 126)
        head += struct.pack(">H", size)
    else:
        head.append(mask_bit | 127)
        head += struct.pack(">Q", size)
    if mask_key:
        head += mask_key
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


def recv_exact(sock, count):
    """Read exactly `count` bytes or return None if the peer closed first."""
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    """Return (opcode, fin, payload) or None when the connection ended."""
    head = recv_exact(sock, 2)
    if head is None:
        return None
    fin = bool(head[0] & 0x80)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    size = head[1] & 0x7F
    if size == 126:
        ext = recv_exact(sock, 2)
        size = struct.unpack(">H", ext)[0] if ext else None
    elif size == 127:
        ext = recv_exact(sock, 8)
        size = struct.unpack(">Q", ext)[0] if ext else None
    if size is None:
        return None
    key = recv_exact(sock, 4) if masked else None
    if masked and key is None:
        return None
    payload = recv_exact(sock, size) if size else b""
    if payload is None:
        return None
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return opcode, fin, payload


def _read_http_request(conn):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk or len(data) > MAX_HEADER:
            return None
        data += chunk
    head = bytes(data).split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 3:
        return None
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return parts[0], parts[1], headers


def _reply(conn, status, reason):
    body = f"{status} {reason}\n".encode()
    conn.sendall(
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode() + body
    )


class WsRelay:
    """Accepts WebSocket clients and relays each to the consumer UNIX socket."""

    def __init__(self, consumer_socket, port=8037, host="127.0.0.1", extra_origins=()):
        self.consumer_socket = consumer_socket
        self.host = host
        self.port = port
        self.extra_origins = tuple(extra_origins)
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        """Bind and start accepting in a background thread (port 0 = pick one)."""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info(
            "ws relay on ws://%s:%d -> %s", self.host, self.port, self.consumer_socket
        )
        return self

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _handshake(self, conn):
        req = _read_http_request(conn)
        if req is None:
            return False
        method, path, headers = req
        if method != "GET" or "websocket" not in headers.get("upgrade", "").lower():
            _reply(conn, 400, "Bad Request")
            return False
        if path.split("?")[0] not in ALLOWED_PATHS:
            _reply(conn, 404, "Not Found")
            return False
        if not origin_allowed(headers.get("origin"), self.extra_origins):
            logger.warning("ws relay: refusing origin %r", headers.get("origin"))
            _reply(conn, 403, "Forbidden")
            return False
        key = headers.get("sec-websocket-key")
        if not key or headers.get("sec-websocket-version") != "13":
            _reply(conn, 400, "Bad Request")
            return False
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept_key(key)}\r\n\r\n"
            ).encode()
        )
        return True

    def _serve(self, conn):
        unix = None
        try:
            if not self._handshake(conn):
                return
            unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                unix.connect(self.consumer_socket)
            except OSError as exc:
                logger.error("ws relay: cannot reach %s: %s", self.consumer_socket, exc)
                conn.sendall(encode_frame(OP_CLOSE, struct.pack(">H", 1011)))
                return
            send_lock = threading.Lock()
            pump = threading.Thread(
                target=self._unix_to_ws, args=(unix, conn, send_lock), daemon=True
            )
            pump.start()
            self._ws_to_unix(conn, unix, send_lock)
        except OSError:
            pass
        finally:
            for s in (unix, conn):
                if s is not None:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    s.close()

    @staticmethod
    def _ws_to_unix(conn, unix, send_lock):
        while True:
            frame = read_frame(conn)
            if frame is None:
                return
            opcode, _fin, payload = frame
            if opcode in (OP_CONT, OP_TEXT, OP_BINARY):
                unix.sendall(payload)
            elif opcode == OP_PING:
                with send_lock:
                    conn.sendall(encode_frame(OP_PONG, payload))
            elif opcode == OP_CLOSE:
                with send_lock:
                    conn.sendall(encode_frame(OP_CLOSE, payload[:2]))
                return

    @staticmethod
    def _unix_to_ws(unix, conn, send_lock):
        try:
            while True:
                data = unix.recv(RELAY_CHUNK)
                if not data:
                    break
                with send_lock:
                    conn.sendall(encode_frame(OP_BINARY, data))
        except OSError:
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_RD)  # unblock the ws->unix reader
            except OSError:
                pass
