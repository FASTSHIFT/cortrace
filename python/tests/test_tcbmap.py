"""Unit tests for nx_tcbmap.py, incl. the resident-OpenOCD telnet read path."""

import socket
import sys
import threading
import types

import pytest

from cortrace import tcbmap as tm


class FakeOpenOcd:
    """Telnet-ish server: greets with a prompt, answers `mdw` from a memory dict."""

    def __init__(self, mem):
        self.mem = mem
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.cmds = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            c.sendall(b"Open On-Chip Debugger\r\n> ")
            buf = b""
            while not buf.endswith(b"\n"):
                d = c.recv(4096)
                if not d:
                    break
                buf += d
            cmd = buf.decode().strip()
            self.cmds.append(cmd)
            _, addr, cnt = cmd.split()
            addr, cnt = int(addr, 16), int(cnt)
            lines = []
            for i in range(0, cnt, 4):
                n = min(4, cnt - i)
                words = " ".join(
                    f"{self.mem.get(addr + 4 * (i + j), 0):08x}" for j in range(n)
                )
                lines.append(f"0x{addr + 4 * i:08x}: {words}")
            c.sendall(("\x00" + "\r\n".join(lines) + "\r\n> ").encode())
            c.close()

    def close(self):
        self.srv.close()


@pytest.fixture
def fake_oocd(monkeypatch):
    servers = []

    def make(mem):
        s = FakeOpenOcd(mem)
        servers.append(s)
        monkeypatch.setattr(tm, "OOCD_TELNET", f"127.0.0.1:{s.port}")
        return s

    yield make
    for s in servers:
        s.close()


def test_read_words_over_telnet_spans_multiple_rows(fake_oocd):
    mem = {0x20000000 + 4 * i: 0x1000 + i for i in range(6)}
    s = fake_oocd(mem)
    got = tm.oocd_read_words(0x20000000, 6)
    assert got == mem
    assert s.cmds == ["mdw 0x20000000 6"]


def test_fn_for_picks_containing_symbol():
    addrs, names = [0x100, 0x200, 0x300], ["a", "b", "c"]
    assert tm.fn_for(addrs, names, 0x2FF) == "b"
    assert tm.fn_for(addrs, names, 0x300) == "c"
    assert tm.fn_for(addrs, names, 0x50) == "?"


def test_sym_and_symtab_parse_nm_output(monkeypatch):
    nm_out = (
        b"08000100 T alpha\n"
        b"08000200 t beta\n"
        b"20000000 D g_pidhash\n"
        b"         U undefined_sym\n"
    )
    monkeypatch.setattr(tm.subprocess, "check_output", lambda *a, **k: nm_out)
    assert tm.sym("nm", "elf", "g_pidhash") == 0x20000000
    assert tm.sym("nm", "elf", "missing") is None
    addrs, names = tm.build_symtab("nm", "elf")
    assert addrs == [0x08000100, 0x08000200]
    assert names == ["alpha", "beta"]


MDW_OUT = "0x20000000: 20001000 00000002\n"


def test_read_words_one_shot_openocd_parses_stdout_and_stderr(monkeypatch):
    monkeypatch.setattr(tm, "OOCD_TELNET", None)
    seen = {}

    def fake_run(cmd, **_kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(stdout=MDW_OUT, stderr="0x20000008: zz 7\n")

    monkeypatch.setattr(tm.subprocess, "run", fake_run)
    got = tm.oocd_read_words(0x20000000, 3)
    assert got == {0x20000000: 0x20001000, 0x20000004: 2, 0x2000000C: 7}
    assert "mdw 0x20000000 3" in seen["cmd"]


def test_main_writes_tcbmap(monkeypatch, tmp_path):
    symbols = {"g_pidhash": 0x20000100, "g_npidhash": 0x20000104}
    monkeypatch.setattr(tm, "sym", lambda nm, elf, name: symbols.get(name))
    monkeypatch.setattr(
        tm, "build_symtab", lambda nm, elf: ([0x08000000, 0x08000100], ["a", "worker"])
    )
    tcb = 0x20002000
    mem = {
        0x20000100: 0x20000200,  # g_pidhash -> table
        0x20000104: 2,  # g_npidhash
        0x20000200: tcb,  # live TCB
        0x20000204: 0x1,  # not SRAM -> skipped
    }
    tcb_words = {tcb + 0x30: 3, tcb + 0x3C: 0x08000104}

    def fake_read(addr, count):
        if addr == tcb:
            return tcb_words
        return {addr + 4 * i: mem.get(addr + 4 * i, 0) for i in range(count)}

    monkeypatch.setattr(tm, "oocd_read_words", fake_read)
    out = tmp_path / "map.txt"
    monkeypatch.setattr(
        sys, "argv", ["nx_tcbmap.py", "--elf", "nuttx", "--out", str(out)]
    )
    tm.main()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[1] == f"0x{tcb:08x}\t3\tworker"
    assert len(lines) == 2


def test_main_exits_without_pidhash_symbols(monkeypatch):
    monkeypatch.setattr(tm, "sym", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["nx_tcbmap.py", "--elf", "nuttx"])
    with pytest.raises(SystemExit):
        tm.main()


def test_main_exits_on_bad_pidhash(monkeypatch):
    monkeypatch.setattr(tm, "sym", lambda nm, elf, name: 0x20000000)
    monkeypatch.setattr(tm, "build_symtab", lambda nm, elf: ([], []))
    monkeypatch.setattr(tm, "oocd_read_words", lambda addr, n: {})
    monkeypatch.setattr(sys, "argv", ["nx_tcbmap.py", "--elf", "nuttx"])
    with pytest.raises(SystemExit):
        tm.main()
