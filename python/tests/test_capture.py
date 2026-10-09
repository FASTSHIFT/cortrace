import types

import pytest
from cortrace import capture


def parse(*args):
    return capture.parse_args(["--elf", "x.elf"] + list(args))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("CORTRACE_OUT_DIR", raising=False)
    monkeypatch.delenv("CORTRACE_IFACE", raising=False)


def test_output_dir_must_be_given_explicitly(capsys):
    with pytest.raises(SystemExit) as e:
        parse("--iface", "eth0")
    assert e.value.code == 2
    assert "--out-dir is required" in capsys.readouterr().err


def test_output_dir_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CORTRACE_OUT_DIR", str(tmp_path / "o"))
    a, _ = parse("--iface", "eth0")
    assert a.out_dir == str(tmp_path / "o")


def test_iface_needed_unless_raw_in(tmp_path, capsys):
    with pytest.raises(SystemExit):
        parse("--out-dir", str(tmp_path))
    assert "--iface" in capsys.readouterr().err
    a, _ = parse("--out-dir", str(tmp_path), "--raw-in", "r.bin")
    assert a.iface is None


def test_unknown_options_are_forwarded(tmp_path):
    a, extra = parse("--out-dir", str(tmp_path), "--raw-in", "r", "--pynuttx", "/p")
    assert extra == ["--pynuttx", "/p"]
    assert a.tag  # defaulted to a timestamp


def args(tmp_path, **kw):
    a, _ = parse("--out-dir", str(tmp_path), "--iface", "eth0", "--tag", "t")
    for key, value in kw.items():
        setattr(a, key, value)
    return a


@pytest.mark.parametrize(
    "base,expect",
    [("cycle", "--cycle-time"), ("etm", "--etm-time"), ("hybrid", "--hybrid-time")],
)
def test_decode_command_time_bases(tmp_path, base, expect):
    a = args(tmp_path, time_base=base, tsgen_hz=75e6, phase="1,0", tcbmap="m.txt")
    cmd = capture.decode_command(a, "raw", "out.pf", "syms")
    assert expect in cmd and cmd[cmd.index("--perf") + 1] == "out.pf"
    assert cmd[cmd.index("--phase") + 1] == "1,0"
    assert cmd[cmd.index("--nx-tcbmap") + 1] == "m.txt"
    assert cmd[cmd.index("--trace-width") + 1] == "4"


@pytest.mark.parametrize("base", ["etm", "hybrid"])
def test_decode_command_needs_tsgen_for_etm_clocks(tmp_path, base):
    with pytest.raises(SystemExit) as e:
        capture.decode_command(args(tmp_path, time_base=base), "r", "o", "s")
    assert "--tsgen-hz" in str(e.value)


def test_grab_sets_width_then_streams(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(capture.ctrl, "main", lambda a: calls.append(a) or 0)
    monkeypatch.setattr(
        capture,
        "run_cmd",
        lambda cmd, **_k: calls.append(cmd) or types.SimpleNamespace(returncode=0),
    )
    monkeypatch.setenv("CORTRACE_GRAB", "/opt/cortrace-grab")
    capture.grab(args(tmp_path, width=2, secs=3.0), "raw.bin")
    assert calls[0] == ["--iface", "eth0", "set-width", "2"]
    assert calls[1] == ["/opt/cortrace-grab", "eth0", "3.0", "raw.bin", "256", "512"]


@pytest.mark.parametrize("which", ["width", "grab"])
def test_grab_failures_abort(monkeypatch, tmp_path, which):
    monkeypatch.setattr(capture.ctrl, "main", lambda a: 1 if which == "width" else 0)
    monkeypatch.setattr(
        capture,
        "run_cmd",
        lambda cmd, **_k: types.SimpleNamespace(returncode=1),
    )
    with pytest.raises(SystemExit) as e:
        capture.grab(args(tmp_path, width=4), "raw.bin")
    assert ("set-width" if which == "width" else "cortrace-grab") in str(e.value)


def fake_decode(ok=True):
    def run_cmd(cmd, **_k):
        if ok and "--perf" in cmd:
            open(cmd[cmd.index("--perf") + 1], "wb").close()
        return types.SimpleNamespace(returncode=0 if ok else 1, stderr="", stdout="")

    return run_cmd


def test_run_hw_only_from_raw_file(monkeypatch, tmp_path):
    raw = tmp_path / "r.bin"
    raw.write_bytes(b"x")
    monkeypatch.setattr(capture, "run_cmd", fake_decode())
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **_k: None)
    a = args(tmp_path / "out", fuse=False)
    result = capture.run(a, raw_in=str(raw))
    assert result == str(tmp_path / "out" / "hw_t.perfetto")


def test_run_reports_decode_failure_and_missing_raw(monkeypatch, tmp_path):
    raw = tmp_path / "r.bin"
    raw.write_bytes(b"x")
    monkeypatch.setattr(capture, "run_cmd", fake_decode(ok=False))
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **_k: None)
    with pytest.raises(SystemExit) as e:
        capture.run(args(tmp_path / "o"), raw_in=str(raw))
    assert "decode failed" in str(e.value)
    with pytest.raises(SystemExit) as e:
        capture.run(args(tmp_path / "o"), raw_in=str(tmp_path / "missing"))
    assert "not found" in str(e.value)


def test_run_captures_when_no_raw_given(monkeypatch, tmp_path):
    grabbed = []
    monkeypatch.setattr(capture, "grab", lambda a, raw: grabbed.append(raw))
    monkeypatch.setattr(capture, "decode_hw", lambda a, raw: raw + ".pf")
    result = capture.run(args(tmp_path / "o", fuse=False))
    assert grabbed == [str(tmp_path / "o" / "raw_t.bin")]
    assert result.endswith("raw_t.bin.pf")


def test_fuse_mode_hands_over_and_finds_the_fused_file(monkeypatch, tmp_path):
    seen = []

    def fake_fuse(argv):
        seen.append(argv)
        (tmp_path / "o" / "fused_t.perfetto").write_bytes(b"")
        return 0

    monkeypatch.setattr(capture.fuse_mod, "main", fake_fuse)
    monkeypatch.setattr(capture, "grab", lambda a, raw: None)
    a = args(tmp_path / "o", fuse=True, tcbmap="m.txt", tsgen_hz=75e6, width=None)
    assert capture.run(a, extra=["--pynuttx", "/p"]).endswith("fused_t.perfetto")
    argv = seen[0]
    assert argv[argv.index("--raw") + 1].endswith("raw_t.bin")
    assert argv[argv.index("--tcbmap") + 1] == "m.txt"
    assert argv[argv.index("--tsgen-hz") + 1] == "75000000.0"
    assert argv[-2:] == ["--pynuttx", "/p"]


def test_fuse_without_alignment_falls_back_to_hardware_trace(monkeypatch, tmp_path):
    monkeypatch.setattr(capture.fuse_mod, "main", lambda argv: 0)
    monkeypatch.setattr(capture, "grab", lambda a, raw: None)
    result = capture.run(args(tmp_path / "o", fuse=True, width=None))
    assert result.endswith("hw_t.perfetto")


def test_fuse_failure_aborts(monkeypatch, tmp_path):
    monkeypatch.setattr(capture.fuse_mod, "main", lambda argv: 3)
    monkeypatch.setattr(capture, "grab", lambda a, raw: None)
    with pytest.raises(SystemExit):
        capture.run(args(tmp_path / "o", fuse=True, width=None))


def test_main_prints_result_and_opens(monkeypatch, tmp_path, capsys):
    opened = []
    monkeypatch.setattr(capture, "run", lambda a, raw_in=None, extra=(): "/x/y.pf")
    monkeypatch.setattr(capture, "perfetto_open_main", lambda a: opened.append(a) or 0)
    base = ["--out-dir", str(tmp_path), "--elf", "e", "--raw-in", "r"]
    assert capture.main(base) == 0
    assert capsys.readouterr().out.strip() == "/x/y.pf" and not opened
    assert capture.main(base + ["--open"]) == 0
    assert opened == [["/x/y.pf", "--keep"]]
