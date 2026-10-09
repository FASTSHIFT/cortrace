"""Tests for cortrace_fuse: Perfetto merge, argument handling, command line."""

import json
import types

import pytest

import cortrace_fuse as cf


class FakePacket:
    def __init__(self, seq):
        self.trusted_packet_sequence_id = seq


class FakeTrace:
    """Stand-in for perfetto_trace_pb2.Trace: JSON on the wire, one field."""

    def __init__(self):
        self.packet = []

    def ParseFromString(self, data):
        self.packet = [FakePacket(s) for s in json.loads(data)]

    def SerializeToString(self):
        return json.dumps([p.trusted_packet_sequence_id for p in self.packet]).encode()


FAKE_PB2 = types.SimpleNamespace(Trace=FakeTrace)


def test_fuse_appends_remapped_note_trace_and_keeps_hw_bytes(tmp_path):
    hw = tmp_path / "hw.perfetto"
    hw_bytes = b"\x00\x01hardware-bytes\xff" * 1000
    hw.write_bytes(hw_bytes)
    note = tmp_path / "note.pftrace"
    note.write_bytes(json.dumps([1, 2, 0, 2]).encode())
    out = tmp_path / "fused.perfetto"

    remapped = cf.fuse(str(hw), str(note), str(out), pb2=FAKE_PB2)

    data = out.read_bytes()
    assert remapped == 3  # sequence id 0 (legacy) is left alone
    assert data.startswith(hw_bytes)  # hardware file is copied untouched
    assert json.loads(data[len(hw_bytes) :]) == [1001, 1002, 0, 1002]


def test_fuse_streams_large_hw_file(tmp_path):
    hw = tmp_path / "hw.perfetto"
    size = cf.CHUNK + 1024 * 1024  # more than one chunk
    hw.write_bytes(b"\xab" * size)
    note = tmp_path / "note.pftrace"
    note.write_bytes(b"[]")
    out = tmp_path / "fused.perfetto"
    cf.fuse(str(hw), str(note), str(out), pb2=FAKE_PB2)
    assert out.stat().st_size == size + len(b"[]")


def test_missing_pynuttx_is_an_argument_error(monkeypatch, capsys):
    monkeypatch.delenv("PYNUTTX", raising=False)
    with pytest.raises(SystemExit) as e:
        cf.parse_args(["--raw", "r.bin", "--elf", "x.elf"])
    assert e.value.code == 2
    assert "--pynuttx" in capsys.readouterr().err


def test_pynuttx_from_environment_and_paths_made_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("PYNUTTX", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    a = cf.parse_args(["--raw", "r.bin", "--elf", "x.elf", "--tcbmap", "t.txt"])
    assert a.pynuttx == str(tmp_path)
    assert a.raw == str(tmp_path / "r.bin")
    assert a.elf == str(tmp_path / "x.elf")
    assert a.tcbmap == str(tmp_path / "t.txt")
    assert a.out_dir == str(tmp_path / "perftrace")


def test_decode_command_carries_time_base_and_outputs(tmp_path):
    a = cf.parse_args(["--raw", "r.bin", "--elf", "x.elf", "--pynuttx", str(tmp_path)])
    paths = {"notes_bin": "n.bin", "hw": "hw.pf", "runs": "runs.tsv"}
    cmd = cf.decode_command(a, paths, "syms.nm")
    assert "--hybrid-time" in cmd and "--itm-note-unwrap" in cmd
    assert cmd[cmd.index("--itm-note-out") + 1] == "n.bin"
    assert cmd[cmd.index("--nx-runs-out") + 1] == "runs.tsv"
    assert cmd[cmd.index("--tsgen-hz") + 1] == "75000000.0"
    assert "--nx-tcbmap" not in cmd
    a.tcbmap = "map.txt"
    assert "--nx-tcbmap" in cf.decode_command(a, paths, "syms.nm")


def test_decode_failure_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cf.subprocess,
        "run",
        lambda cmd, **_kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    )
    with pytest.raises(SystemExit) as e:
        cf.main(
            [
                "--raw",
                "r.bin",
                "--elf",
                "x.elf",
                "--pynuttx",
                str(tmp_path),
                "--out-dir",
                str(tmp_path),
                "--nm",
                "true",
            ]
        )
    assert "decode failed" in str(e.value)


def test_empty_note_stream_is_reported(monkeypatch, tmp_path):
    def fake_run(cmd, **_kw):
        if "--itm-note-out" in cmd:
            (tmp_path / "notes_t.bin").write_bytes(b"")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cf.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        cf.main(
            [
                "--raw",
                "r.bin",
                "--elf",
                "x.elf",
                "--pynuttx",
                str(tmp_path),
                "--out-dir",
                str(tmp_path),
                "--tag",
                "t",
                "--nm",
                "true",
            ]
        )
    assert "no note bytes" in str(e.value)


def test_full_pipeline_with_stubbed_tools(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        joined = " ".join(str(c) for c in cmd)
        if "--itm-note-out" in cmd:
            (tmp_path / "notes_t.bin").write_bytes(b"\x01\x02")
            (tmp_path / "hw_t.perfetto").write_bytes(b"HW")
        elif "align_check.py" in joined:
            (tmp_path / "offset_t.txt").write_text("1000", encoding="utf-8")
        elif "-o" in cmd:  # nxtrace capture -> note perfetto
            (tmp_path / "note_t.pftrace").write_bytes(b"[1]")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cf.subprocess, "run", fake_run)
    monkeypatch.setattr(cf, "load_pb2", lambda _p: FAKE_PB2)
    rc = cf.main(
        [
            "--raw",
            "r.bin",
            "--elf",
            "x.elf",
            "--pynuttx",
            str(tmp_path),
            "--out-dir",
            str(tmp_path),
            "--tag",
            "t",
            "--nm",
            "true",
            "--tcbmap",
            "map.txt",
            "--open",
        ]
    )
    assert rc == 0
    fused = (tmp_path / "fused_t.perfetto").read_bytes()
    assert fused == b"HW" + b"[1001]"
    nxtrace = [c for c in calls if "-o" in c][0]
    assert nxtrace[nxtrace.index("--ts-offset-ns") + 1] == "-1000"
    assert any("perfetto_open.py" in str(c) for c in calls)


def test_alignment_failure_leaves_note_trace_unshifted(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(cmd)
        if "--itm-note-out" in cmd:
            (tmp_path / "notes_t.bin").write_bytes(b"\x01")
        if "align_check.py" in " ".join(str(c) for c in cmd):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cf.subprocess, "run", fake_run)
    cf.main(
        [
            "--raw",
            "r.bin",
            "--elf",
            "x.elf",
            "--pynuttx",
            str(tmp_path),
            "--out-dir",
            str(tmp_path),
            "--tag",
            "t",
            "--nm",
            "true",
        ]
    )
    assert not (tmp_path / "fused_t.perfetto").exists()
    assert all("--ts-offset-ns" not in c for c in calls)
