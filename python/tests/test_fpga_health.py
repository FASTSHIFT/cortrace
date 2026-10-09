import pytest
from fakefpga import FakeFpga

from cortrace.fpga import health


def put(mem, addr, value, size=1):
    for i, byte in enumerate(value.to_bytes(size, "little")):
        mem[addr + i] = byte


@pytest.fixture
def board(monkeypatch):
    mem = {}
    fake = FakeFpga(mem)
    monkeypatch.setattr(health, "PORT", fake.readout_port)
    monkeypatch.setattr(health, "CTRL_PORT", fake.ctrl_port)
    monkeypatch.setattr(health.time, "sleep", lambda _s: None)
    yield mem, fake
    fake.close()


def run(*args):
    return health.main(["127.0.0.1"] + list(args))


def healthy(mem):
    put(mem, health.A_MAGIC, 0xDB)
    # have_first=0 pkt_active=1 traceclk=1 trace_lock=1 sys_lock=1 fsm=2
    put(mem, health.A_LIVE, 0b0111_1010)


def test_healthy_link(board, capsys):
    mem, _ = board
    healthy(mem)
    assert run() == 0
    out = capsys.readouterr().out
    assert (
        "no sticky errors latched" in out
        and "TRACECLK active + trace MMCM locked" in out
    )


def test_sticky_error_is_reported_with_advice(board, capsys):
    mem, _ = board
    healthy(mem)
    put(mem, health.A_LIVE, 0b1111_1010)  # have_first
    put(mem, health.A_FIRST_CODE, 0x0401, 2)
    put(mem, health.A_FIRST_TIME, 125_000, 4)
    assert run() == 2
    out = capsys.readouterr().out
    assert "FIRST_ERR = 0x0401" in out and "ARP deadlock" in out


@pytest.mark.parametrize(
    "code,text", [(0x0301, "drain"), (0x0101, "check STM32 ETM config")]
)
def test_other_error_codes_get_their_advice(board, capsys, code, text):
    mem, _ = board
    healthy(mem)
    put(mem, health.A_LIVE, 0b1111_1010)
    put(mem, health.A_FIRST_CODE, code, 2)
    assert run() == 2
    assert text in capsys.readouterr().out


def test_clock_trouble_is_called_out(board, capsys):
    mem, _ = board
    put(mem, health.A_MAGIC, 0xDB)
    put(mem, health.A_LIVE, 0b0000_0010)  # no locks, no TRACECLK
    assert run() == 0
    out = capsys.readouterr().out
    assert "system MMCM not locked" in out and "no TRACECLK activity" in out
    put(mem, health.A_LIVE, 0b0010_1010)  # TRACECLK but trace MMCM unlocked
    assert run() == 0
    assert "trace MMCM not locked" in capsys.readouterr().out


def test_wrong_magic_warns(board, capsys):
    mem, _ = board
    put(mem, health.A_MAGIC, 0x00)
    assert run() == 0
    assert "magic = 0x00" in capsys.readouterr().out


def test_reset_flag_pulses_soft_reset_first(board, capsys):
    mem, fake = board
    healthy(mem)
    assert run("reset") == 0
    assert (health.REG_SOFTRST, 1) in fake.csr_writes
    assert "soft reset pulsed" in capsys.readouterr().out


def test_ddr3_page(board, capsys):
    mem, _ = board
    put(mem, health.A_DDR3_MAGIC, 0xD3)
    put(mem, health.A_DDR3_FLAGS, 0b101)  # calib + mig calib, no error
    put(mem, health.A_DDR3_PASSB, 7, 4)
    put(mem, health.A_BUILD_ID, 1_700_000_000, 4)
    assert run("ddr3") == 0
    assert "DDR3 PASS" in capsys.readouterr().out
    put(mem, health.A_DDR3_PASSB, 0, 4)
    assert run("ddr3") == 0
    assert "no verified bursts" in capsys.readouterr().out
    put(mem, health.A_DDR3_FLAGS, 0)
    assert run("ddr3") == 2
    assert "NOT calibrated" in capsys.readouterr().out
    put(mem, health.A_DDR3_FLAGS, 0b111)  # calib + sticky error
    put(mem, health.A_DDR3_ERRC, 3, 4)
    assert run("ddr3") == 2
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "all-zero" in out
    put(mem, health.A_DDR3_GOTLO, 5)
    assert run("ddr3") == 2
    assert "first word wrong" in capsys.readouterr().out
    put(mem, health.A_DDR3_MAGIC, 0)
    assert run("ddr3") == 1


def test_blackbox_page(board, capsys):
    mem, _ = board
    put(mem, 0xFF50, 0xB0)
    put(mem, 0xFF51, 0b101)  # calib + TRACECLK active
    put(mem, 0xFF52, 100, 4)  # words written
    put(mem, 0xFF56, 2, 4)  # lost
    assert run("blackbox") == 0
    out = capsys.readouterr().out
    assert "BLACK BOX RECORDING" in out and "capture-side bytes dropped" in out
    put(mem, 0xFF52, 0, 4)
    assert run("bb") == 0
    assert "0 words written" in capsys.readouterr().out
    put(mem, 0xFF51, 0b001)
    assert run("bb") == 0
    assert "no TRACECLK" in capsys.readouterr().out
    put(mem, 0xFF51, 0)
    assert run("bb") == 2
    put(mem, 0xFF50, 0)
    assert run("bb") == 1


def test_silent_fpga_is_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(health, "PORT", 9)  # nobody listens
    real_socket = health.socket.socket

    class QuickSocket(real_socket):
        def settimeout(self, _value):
            super().settimeout(0.05)

    monkeypatch.setattr(health.socket, "socket", QuickSocket)
    assert health.main(["127.0.0.1"]) == 1
    assert health.main(["127.0.0.1", "ddr3"]) == 1
    assert health.main(["127.0.0.1", "bb"]) == 1
    assert "no reply on :5001" in capsys.readouterr().out
