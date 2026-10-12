"""Debug-probe access for the host tools: read target memory.

Only what the tools need. The one implementation talks to an OpenOCD that is
already running (its telnet port); a second OpenOCD would take the probe away
and clear the DWT configuration that the trace depends on.
"""

import re
import socket

PROMPT = b"> "
_WORD = re.compile(r"0x[0-9a-fA-F]+:\s+([0-9a-fA-F]{8})")
_LINE = re.compile(r"\s*0x([0-9a-fA-F]+):\s+((?:[0-9a-fA-F]{2}\s*)+)")


class ProbeError(Exception):
    """The probe could not be reached or did not answer."""


class OpenOcdTelnet:
    """Read target memory through a running OpenOCD's telnet server."""

    def __init__(self, hostport, timeout=5.0):
        host, _, port = hostport.rpartition(":")
        try:
            self._sock = socket.create_connection(
                (host or "127.0.0.1", int(port)), timeout=timeout
            )
        except (OSError, ValueError) as exc:
            raise ProbeError(f"cannot connect to OpenOCD at {hostport}: {exc}") from exc
        self._read_until_prompt()

    def close(self):
        """Close the connection."""
        self._sock.close()

    def _read_until_prompt(self):
        buf = b""
        try:
            while not buf.endswith(PROMPT):
                chunk = self._sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except socket.timeout as exc:
            raise ProbeError("OpenOCD did not answer in time") from exc
        return buf.replace(b"\x00", b"").decode(errors="replace")

    def command(self, text):
        """Run one OpenOCD command, return its output."""
        self._sock.sendall(text.encode() + b"\n")
        return self._read_until_prompt()

    def _checked(self, text):
        out = self.command(text)
        if "rror" in out:
            raise ProbeError(f"OpenOCD: `{text}` failed: {out.strip()}")
        return out

    def read32(self, addr):
        """The 32-bit register or word at `addr`."""
        out = self._checked(f"mdw 0x{addr:08x}")
        match = _WORD.search(out)
        if not match:
            raise ProbeError(f"cannot read 0x{addr:08x}: {out.strip()}")
        return int(match.group(1), 16)

    def write32(self, addr, value):
        """Write a 32-bit register or word."""
        self._checked(f"mww 0x{addr:08x} 0x{value:08x}")

    def halt(self):
        """Stop the core."""
        self._checked("halt")

    def resume(self):
        """Let the core run again."""
        self._checked("resume")

    def read_bytes(self, addr, size):
        """`size` bytes at `addr`, or None if the target refused the read."""
        out = {}
        for line in self.command(f"mdb 0x{addr:08x} {size}").splitlines():
            match = _LINE.match(line)
            if not match:
                continue
            base = int(match.group(1), 16)
            for i, tok in enumerate(match.group(2).split()):
                out[base + i] = int(tok, 16)
        if any(addr + i not in out for i in range(size)):
            return None
        return bytes(out[addr + i] for i in range(size))
