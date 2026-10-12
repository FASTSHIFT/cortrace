"""OpenOcdTelnet against a tiny fake OpenOCD telnet server."""

import socket
import threading

import pytest

from cortrace.probe import OpenOcdTelnet, ProbeError


class FakeOpenOcd:
    """Answers `mdb ADDR COUNT` like OpenOCD does (16 bytes per line)."""

    def __init__(self, memory, fail=None):
        self.memory = memory  # {addr: byte}
        self.words = {}  # {addr: 32-bit value} for mdw / mww
        self.log = []
        self.fail = fail  # command word that answers with an error
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
        word, *rest = command.split()
        self.log.append(command)
        if word in ("halt", "resume"):
            return b"" if self.fail != word else b"Error: no target\r\n"
        if word == "mww":
            self.words[int(rest[0], 0)] = int(rest[1], 0)
            return b"" if self.fail != word else b"Error: write failed\r\n"
        if word == "mdw":
            addr = int(rest[0], 0)
            if addr not in self.words:
                return b"\r\n"
            return f"0x{addr:08x}: {self.words[addr]:08x} \r\n".encode()
        assert word == "mdb"
        addr, count = int(rest[0], 0), int(rest[1])
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


def test_registers_are_written_and_read_back():
    server = FakeOpenOcd({})
    probe = OpenOcdTelnet(f"127.0.0.1:{server.port}")
    probe.halt()
    probe.write32(0xE0001020, 0x20000410)
    assert probe.read32(0xE0001020) == 0x20000410
    probe.resume()
    assert server.log == [
        "halt",
        "mww 0xe0001020 0x20000410",
        "mdw 0xe0001020",
        "resume",
    ]
    probe.close()


def test_unreadable_register_is_a_probe_error():
    server = FakeOpenOcd({})
    probe = OpenOcdTelnet(f"127.0.0.1:{server.port}")
    with pytest.raises(ProbeError, match="cannot read 0xe0001020"):
        probe.read32(0xE0001020)
    probe.close()


@pytest.mark.parametrize("which", ["halt", "resume", "mww"])
def test_openocd_errors_are_probe_errors(which):
    server = FakeOpenOcd({}, fail=which)
    probe = OpenOcdTelnet(f"127.0.0.1:{server.port}")
    with pytest.raises(ProbeError, match="failed"):
        {
            "halt": probe.halt,
            "resume": probe.resume,
            "mww": lambda: probe.write32(4, 5),
        }[which]()
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
