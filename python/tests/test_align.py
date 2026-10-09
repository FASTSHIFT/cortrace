"""Unit tests for align_check.py (hw <-> note switch alignment)."""

import sys
from pathlib import Path

import pytest

from cortrace import align as ac


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
    assert not ac.load_tcbmap(None)


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
    assert off is None and not res


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


def test_main_nothing_to_compare(tmp_path, monkeypatch):
    runs = write(tmp_path, "runs.tsv", "100\tidle (pid 0)\n")
    note = write(tmp_path, "note.txt", "no notes here\n")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, ["--hw-runs", runs, "--note", note])


def test_main_no_common_offset_returns_1(tmp_path, monkeypatch):
    runs = write(tmp_path, "runs.tsv", "100\tidle (pid 0)\n200\tw (pid 1)\n")
    note = write(tmp_path, "note.txt", "[5] cpu=0 pid=9 type=3\n")
    assert run_main(monkeypatch, ["--hw-runs", runs, "--note", note]) == 1


def test_main_requires_hw_runs(tmp_path, monkeypatch):
    note = write(tmp_path, "note.txt", "[1] cpu=0 pid=0 type=3\n")
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, ["--note", note])
    assert e.value.code == 2
