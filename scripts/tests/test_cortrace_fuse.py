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

    def ListFields(self):
        return []


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


class FakeDecodeProc:
    """Popen stand-in for cortrace-decode: emits log lines, returns an exit code."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


class FakeJob:
    def __init__(self):
        self.returncode = 0

    def wait(self):
        return 0


def make_popen(tmp_path, started, decode_rc=0, notes=b"\x01\x02"):
    """subprocess.Popen replacement: decode creates the outputs, nxtrace jobs
    record when they started relative to the decode finishing."""

    def popen(cmd, **_kw):
        if "--itm-note-out" in cmd:
            (tmp_path / "notes_t.bin").write_bytes(notes)
            (tmp_path / "hw_t.perfetto").write_bytes(b"HW")
            lines = ["noise\n", "itm notes: port 1 -> 2 B\n", "error: x\n"]
            return FakeDecodeProc(lines, decode_rc)
        started.append(list(cmd))
        if "-o" in cmd:  # nxtrace capture -> note perfetto at the note clock
            (tmp_path / "note_t.raw.pftrace").write_bytes(b"[1]")
        return FakeJob()

    return popen


def base_args(tmp_path, *extra):
    return [
        "--raw", "r.bin", "--elf", "x.elf", "--pynuttx", str(tmp_path),
        "--out-dir", str(tmp_path), "--tag", "t", "--nm", "true",
    ] + list(extra)  # fmt: skip


def test_decode_failure_exits(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", make_popen(tmp_path, started, 1))
    with pytest.raises(SystemExit) as e:
        cf.main(base_args(tmp_path))
    assert "decode failed" in str(e.value)


def test_empty_note_stream_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", make_popen(tmp_path, [], notes=b""))
    with pytest.raises(SystemExit) as e:
        cf.main(base_args(tmp_path))
    assert "no note bytes" in str(e.value)


def test_note_passes_are_launched_from_the_notes_line(monkeypatch, tmp_path):
    """Both nxtrace passes are launched from the "itm notes: port" line, i.e.
    before the decode process has been waited on."""
    events = []
    started = []
    base = make_popen(tmp_path, started)

    def popen(cmd, **kw):
        proc = base(cmd, **kw)
        if "--itm-note-out" in cmd:
            real_wait = proc.wait

            def wait():
                events.append("decode-wait")
                return real_wait()

            proc.wait = wait
        else:
            events.append("note-job")
        return proc

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", popen)
    monkeypatch.setattr(cf, "load_pb2", lambda _p: FAKE_PB2)

    def fake_run(cmd, **_k):
        if "align_check.py" in " ".join(str(c) for c in cmd):
            (tmp_path / "offset_t.txt").write_text("1000", encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cf, "run", fake_run)
    assert cf.main(base_args(tmp_path)) == 0
    assert events.count("note-job") == 2
    assert len(started) == 2


def test_full_pipeline_shifts_note_trace_and_fuses(monkeypatch, tmp_path):
    shifts = []
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", make_popen(tmp_path, []))
    monkeypatch.setattr(cf, "load_pb2", lambda _p: FAKE_PB2)
    monkeypatch.setattr(cf, "shift_timestamps", lambda m, off: shifts.append(off))
    calls = []

    def fake_run(cmd, **_kw):
        calls.append([str(c) for c in cmd])
        if "align_check.py" in " ".join(calls[-1]):
            (tmp_path / "offset_t.txt").write_text("1000", encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cf, "run", fake_run)
    rc = cf.main(base_args(tmp_path, "--tcbmap", "map.txt", "--open"))
    assert rc == 0
    assert shifts == [-1000]  # note_ns - offset = hw_ns
    assert (tmp_path / "fused_t.perfetto").read_bytes() == b"HW" + b"[1001]"
    assert not (tmp_path / "note_t.raw.pftrace").exists()  # temp file removed
    assert any("perfetto_open.py" in c[1] for c in calls)


def test_alignment_failure_leaves_note_trace_unshifted(monkeypatch, tmp_path):
    shifts = []
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", make_popen(tmp_path, []))
    monkeypatch.setattr(cf, "load_pb2", lambda _p: FAKE_PB2)
    monkeypatch.setattr(cf, "shift_timestamps", lambda m, off: shifts.append(off))
    monkeypatch.setattr(
        cf, "run", lambda cmd, **_k: types.SimpleNamespace(returncode=1)
    )
    cf.main(base_args(tmp_path))
    assert not shifts  # offset 0: nothing to shift
    assert (tmp_path / "note_t.pftrace").exists()
    assert not (tmp_path / "fused_t.perfetto").exists()


def test_missing_note_trace_is_reported(monkeypatch, tmp_path):
    def popen(cmd, **kw):
        if "--itm-note-out" in cmd:
            return make_popen(tmp_path, [])(cmd, **kw)
        return FakeJob()  # nxtrace "ran" but wrote nothing

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", popen)
    monkeypatch.setattr(
        cf, "run", lambda cmd, **_k: types.SimpleNamespace(returncode=1)
    )
    with pytest.raises(SystemExit) as e:
        cf.main(base_args(tmp_path))
    assert "no note trace" in str(e.value)


def test_nxtrace_commands_use_freq_and_pid_names(tmp_path):
    a = cf.parse_args(
        ["--raw", "r", "--elf", "e", "--pynuttx", str(tmp_path), "--tcbmap", "m.txt"]
    )
    paths = {"notes_bin": "n.bin", "note_raw": "raw.pf"}
    dump, pftrace = cf.nxtrace_commands(a, paths)
    assert dump[-3:] == ["dump", "file", "n.bin"]
    assert pftrace[-4:] == ["-o", "raw.pf", "file", "n.bin"]
    assert "--pid-names" in dump and "--pid-names" in pftrace
    assert dump[dump.index("--freq") + 1] == "150000000"


def test_shift_timestamps_on_a_real_message_tree():
    # pylint: disable=no-member
    pytest.importorskip("google.protobuf")
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    fdp = descriptor_pb2.FileDescriptorProto(name="t.proto", package="t")
    ev = fdp.message_type.add(name="Event")
    ev.field.add(name="timestamp", number=1, type=4, label=1)
    ev.field.add(name="pid", number=2, type=4, label=1)
    pkt = fdp.message_type.add(name="Packet")
    pkt.field.add(name="timestamp", number=1, type=4, label=1)
    pkt.field.add(name="last_read_event_timestamp", number=2, type=4, label=1)
    pkt.field.add(name="event", number=3, type=11, label=3, type_name=".t.Event")
    pkt.field.add(name="sequence", number=4, type=4, label=1)
    root = fdp.message_type.add(name="Root")
    root.field.add(name="packet", number=1, type=11, label=3, type_name=".t.Packet")
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fdp)
    root_cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("t.Root"))

    msg = root_cls()
    p = msg.packet.add(timestamp=100, last_read_event_timestamp=90, sequence=7)
    p.event.add(timestamp=10, pid=5)
    p.event.add(timestamp=20, pid=6)
    msg.packet.add(sequence=8)  # no timestamp: stays unset

    cf.shift_timestamps(msg, 1000)
    assert p.timestamp == 1100 and p.last_read_event_timestamp == 1090
    assert [e.timestamp for e in p.event] == [1010, 1020]
    assert [e.pid for e in p.event] == [5, 6]  # only timestamps move
    assert p.sequence == 7
    assert not msg.packet[1].HasField("timestamp")
    cf.shift_timestamps(msg, -1000)
    assert p.timestamp == 100 and [e.timestamp for e in p.event] == [10, 20]


def test_shift_trace_file_round_trips_through_pb2(tmp_path):
    src = tmp_path / "in.pf"
    src.write_bytes(json.dumps([3, 4]).encode())
    dst = tmp_path / "out.pf"
    cf.shift_trace_file(str(src), str(dst), 5, pb2=FAKE_PB2)
    assert json.loads(dst.read_bytes()) == [3, 4]
