"""Unit tests for align_check.py (hw <-> note switch alignment)."""

import sys
import types
from pathlib import Path

import pytest

import align_check as ac


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_load_tcbmap_parses_name_to_pid(tmp_path):
    p = write(
        tmp_path,
        "tcb.txt",
        "# header\n\n0x20001000\t3\tworker_compute\n0x20002000\t4\tworker_io\nbad line\n",
    )
    assert ac.load_tcbmap(p) == {"worker_compute": 3, "worker_io": 4}


def test_load_tcbmap_none_is_empty():
    assert ac.load_tcbmap(None) == {}


def test_hw_switches_tsv_uses_pid_suffix_then_tcbmap_then_name(tmp_path):
    p = write(
        tmp_path,
        "runs.tsv",
        "300\tidle (pid 0)\n100\tworker_io\n200\tmystery\n",
    )
    out = ac.hw_switches_tsv(p, {"worker_io": 4})
    # sorted by time; fallbacks: tcbmap pid, then the raw name
    assert out == [(100, 4), (200, "mystery"), (300, 0)]


def test_note_switches_keeps_only_resume_type(tmp_path):
    p = write(
        tmp_path,
        "note.txt",
        "[1000] cpu=0 pid=2 type=3\n"
        "[1500] cpu=0 pid=2 type=2\n"
        "garbage\n"
        "[2000] cpu=0 pid=5 type=3\n",
    )
    assert ac.note_switches(p, 3) == [(1000, 2), (2000, 5)]
    assert ac.note_switches(p, 2) == [(1500, 2)]


def test_sequence_pair_exact_offset_and_residuals():
    hw = [(100, 1), (200, 2), (300, 1)]
    notes = [(1100, 1), (1200, 2), (1302, 1)]
    off, res = ac.sequence_pair(hw, notes)
    assert off == 1000
    assert res == [(100, 0), (200, 0), (300, 2)]


@pytest.mark.parametrize(
    "hw,notes",
    [
        ([(1, 1), (2, 2)], [(1, 1)]),  # different length
        ([(1, 1), (2, 2)], [(1, 1), (2, 3)]),  # different thread order
    ],
)
def test_sequence_pair_rejects_mismatch(hw, notes):
    assert ac.sequence_pair(hw, notes) == (None, [])


def test_global_fit_recovers_offset_with_lost_notes():
    # hw has every switch; the note stream lost one (so sequence_pair can't be used)
    hw = [(i * 1000, 1 + (i % 3)) for i in range(1, 40)]
    off_true = 5_000_000
    notes = [(t + off_true, p) for t, p in hw if t != 7000]
    assert ac.sequence_pair(hw, notes) == (None, [])
    off, res = ac.global_fit(hw, notes, 300)
    assert off == off_true
    assert len(res) == len(hw) - 1
    assert all(abs(d) <= 300 for _, d in res)


def test_global_fit_no_overlap_returns_none():
    hw = [(0, 1), (100, 2)]
    notes = [(0, 9)]  # pid never seen in hw
    off, res = ac.global_fit(hw, notes, 10)
    assert off is None and res == []


def run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["align_check.py"] + argv)
    return ac.main()


def test_main_pairs_one_to_one_and_writes_offset(tmp_path, monkeypatch, capsys):
    runs = write(
        tmp_path, "runs.tsv", "100\tidle (pid 0)\n200\tw (pid 3)\n300\tidle (pid 0)\n"
    )
    note = write(
        tmp_path,
        "note.txt",
        "[1100] cpu=0 pid=0 type=3\n[1200] cpu=0 pid=3 type=3\n[1300] cpu=0 pid=0 type=3\n",
    )
    off = str(tmp_path / "off.txt")
    rc = run_main(monkeypatch, ["--hw-runs", runs, "--note", note, "--offset-out", off])
    assert rc == 0
    assert Path(off).read_text(encoding="utf-8") == "1000"
    assert "one-to-one" in capsys.readouterr().out


def test_main_falls_back_to_global_fit(tmp_path, monkeypatch, capsys):
    runs = write(
        tmp_path,
        "runs.tsv",
        "".join(f"{i * 1000}\tt (pid {i % 2})\n" for i in range(1, 30)),
    )
    # drop one note -> sequences differ
    lines = [
        f"[{i * 1000 + 777}] cpu=0 pid={i % 2} type=3\n"
        for i in range(1, 30)
        if i != 10
    ]
    note = write(tmp_path, "note.txt", "".join(lines))
    off = str(tmp_path / "off.txt")
    rc = run_main(
        monkeypatch,
        ["--hw-runs", runs, "--note", note, "--offset-out", off, "--tol-us", "1"],
    )
    assert rc == 0
    assert Path(off).read_text(encoding="utf-8") == "777"
    out = capsys.readouterr().out
    assert "one-to-one" not in out
    assert "unmatched hw" in out


def test_main_requires_an_hw_input(tmp_path, monkeypatch):
    note = write(tmp_path, "note.txt", "[1] cpu=0 pid=0 type=3\n")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, ["--note", note])


def test_main_nothing_to_compare(tmp_path, monkeypatch):
    runs = write(tmp_path, "runs.tsv", "100\tidle (pid 0)\n")
    note = write(tmp_path, "note.txt", "no notes here\n")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, ["--hw-runs", runs, "--note", note])


def test_main_no_common_offset_returns_1(tmp_path, monkeypatch):
    runs = write(tmp_path, "runs.tsv", "100\tidle (pid 0)\n200\tw (pid 1)\n")
    note = write(tmp_path, "note.txt", "[5] cpu=0 pid=9 type=3\n")
    assert run_main(monkeypatch, ["--hw-runs", runs, "--note", note]) == 1


def test_default_pb2_follows_pynuttx_env(monkeypatch):
    monkeypatch.delenv("PYNUTTX", raising=False)
    assert ac.default_pb2() is None
    monkeypatch.setenv("PYNUTTX", "/some/pynuttx")
    assert ac.default_pb2() == "/some/pynuttx/nxtrace/perfetto_trace_pb2.py"


def test_main_hw_without_pb2_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("PYNUTTX", raising=False)
    note = write(tmp_path, "note.txt", "[1] cpu=0 pid=0 type=3\n")
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, ["--hw", "x.perfetto", "--note", note])
    assert "--pb2" in str(e.value)


class _Msg:
    """Tiny protobuf-message stand-in: HasField is true for populated fields."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def HasField(self, name):
        return name in self.__dict__


def _pb2_with(packets):
    class Trace:
        TYPE = 1

        def __init__(self):
            self.packet = []

        def ParseFromString(self, _data):
            self.packet = packets

    return types.SimpleNamespace(Trace=Trace)


def test_hw_switches_reads_threads_track(tmp_path):
    track = _Msg(track_descriptor=_Msg(uuid=7, name="Threads"))
    other = _Msg(track_descriptor=_Msg(uuid=8, name="Other"))

    def ev(uuid, name, ts, typ=1):
        te = _Msg(track_uuid=uuid, type=typ, name=name, TYPE_SLICE_BEGIN=1)
        return _Msg(track_event=te, timestamp=ts)

    packets = [
        track,
        other,
        ev(7, "a (pid 3)", 200),
        ev(7, "worker", 100),
        ev(8, "x (pid 9)", 50),  # wrong track
        ev(7, "end", 300, typ=2),  # not a slice begin
    ]
    f = tmp_path / "hw.perfetto"
    f.write_bytes(b"")
    out = ac.hw_switches(str(f), _pb2_with(packets), {"worker": 4})
    assert out == [(100, 4), (200, 3)]


def test_hw_switches_without_threads_track_exits(tmp_path):
    f = tmp_path / "hw.perfetto"
    f.write_bytes(b"")
    pb2 = _pb2_with([_Msg(track_descriptor=_Msg(uuid=1, name="Other"))])
    with pytest.raises(SystemExit):
        ac.hw_switches(str(f), pb2, {})
