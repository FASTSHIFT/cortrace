"""OpenOcdTelnet against a tiny fake OpenOCD telnet server."""

import socket
import threading

import pytest

from cortrace.probe import OpenOcdTelnet, ProbeError


class FakeOpenOcd:
    """Answers `mdb ADDR COUNT` like OpenOCD does (16 bytes per line)."""

    def __init__(self, memory):
        self.memory = memory  # {addr: byte}
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.server.accept()
        conn.sendall(b"Open On-Chip Debugger\r\n> ")
        buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                conn.sendall(self.answer(line.decode().strip()) + b"> ")
        conn.close()

    def answer(self, command):
        word, addr, count = command.split()
        assert word == "mdb"
        addr, count = int(addr, 0), int(count)
        lines = []
        for base in range(addr, addr + count, 16):
            row = [
                self.memory[a]
                for a in range(base, min(base + 16, addr + count))
                if a in self.memory
            ]
            if row:
                lines.append(f"0x{base:08x}: " + " ".join(f"{b:02x}" for b in row))
        return ("\r\n".join(lines) + "\r\n").encode()


def test_reads_bytes_across_lines():
    memory = {0x2000_0000 + i: i for i in range(40)}
    server = FakeOpenOcd(memory)
    probe = OpenOcdTelnet(f"127.0.0.1:{server.port}")
    assert probe.read_bytes(0x2000_0000, 40) == bytes(range(40))
    assert probe.read_bytes(0x2000_0004, 4) == bytes([4, 5, 6, 7])
    probe.close()


def test_a_refused_read_gives_none():
    server = FakeOpenOcd({0x2000_0000: 1})
    probe = OpenOcdTelnet(f"127.0.0.1:{server.port}")
    assert probe.read_bytes(0x2000_0000, 4) is None
    assert probe.read_bytes(0x3000_0000, 4) is None
    probe.close()


def test_connection_failure_is_a_probe_error():
    with pytest.raises(ProbeError, match="cannot connect"):
        OpenOcdTelnet("127.0.0.1:1")
    with pytest.raises(ProbeError, match="cannot connect"):
        OpenOcdTelnet("127.0.0.1:notaport")


def test_a_silent_server_times_out():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    with pytest.raises(ProbeError, match="did not answer"):
        OpenOcdTelnet(f"127.0.0.1:{server.getsockname()[1]}", timeout=0.2)
    server.close()
