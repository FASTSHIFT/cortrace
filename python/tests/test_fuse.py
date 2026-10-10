"""Tests for cortrace_fuse: Perfetto merge, argument handling, command line."""

import json
import tarfile

import pytest
import wirehelp as wh

from cortrace import fuse as cf


def test_fused_name_follows_the_format():
    assert cf.fused_name("t", "archive") == "fused_t.tar"
    assert cf.fused_name("t", "flat") == "fused_t.perfetto"


def test_installed_nxtrace_needs_no_pynuttx_option(monkeypatch):
    monkeypatch.delenv("PYNUTTX", raising=False)
    monkeypatch.setattr(cf.importlib.util, "find_spec", lambda _n: object())
    a = cf.parse_args(["--raw", "r.bin", "--elf", "x.elf", "--out-dir", "o"])
    assert a.pynuttx is None


def test_out_dir_is_never_defaulted(monkeypatch, capsys):
    monkeypatch.delenv("CORTRACE_OUT_DIR", raising=False)
    with pytest.raises(SystemExit) as e:
        cf.parse_args(["--raw", "r.bin", "--elf", "x.elf", "--pynuttx", "/p"])
    assert e.value.code == 2
    assert "--out-dir is required" in capsys.readouterr().err


def test_missing_nxtrace_is_an_argument_error(monkeypatch, capsys):
    monkeypatch.delenv("PYNUTTX", raising=False)
    monkeypatch.setattr(cf.importlib.util, "find_spec", lambda _n: None)
    with pytest.raises(SystemExit) as e:
        cf.parse_args(["--raw", "r.bin", "--elf", "x.elf", "--out-dir", "o"])
    assert e.value.code == 2
    assert "--pynuttx" in capsys.readouterr().err


def test_pynuttx_from_environment_and_paths_made_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("PYNUTTX", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CORTRACE_OUT_DIR", "perftrace")
    a = cf.parse_args(["--raw", "r.bin", "--elf", "x.elf", "--tcbmap", "t.txt"])
    assert a.pynuttx == str(tmp_path)
    assert a.raw == str(tmp_path / "r.bin")
    assert a.elf == str(tmp_path / "x.elf")
    assert a.tcbmap == str(tmp_path / "t.txt")
    assert a.out_dir == str(tmp_path / "perftrace")


def test_decode_command_carries_time_base_and_outputs(tmp_path):
    a = cf.parse_args(
        [
            "--raw",
            "r.bin",
            "--elf",
            "x.elf",
            "--out-dir",
            "o",
            "--pynuttx",
            str(tmp_path),
        ]
    )
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
            (tmp_path / "note_t.pftrace").write_bytes(
                wh.trace(wh.packet(ts=5000, seq=1))
            )
        return FakeJob()

    return popen


def base_args(tmp_path, *extra):
    raw = tmp_path / "r.bin"
    raw.write_bytes(b"raw")
    return [
        "--raw", str(raw), "--elf", "x.elf", "--pynuttx", str(tmp_path),
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

    def fake_align(_argv):
        (tmp_path / "offset_t.txt").write_text("1000", encoding="utf-8")
        return 0

    monkeypatch.setattr(cf, "align_main", fake_align)
    assert cf.main(base_args(tmp_path)) == 0
    assert events.count("note-job") == 2
    assert len(started) == 2


def aligned_pipeline(monkeypatch, tmp_path, aligned=None, opened=None):
    """Run-ready fakes: decode/nxtrace create their files, align finds offset 1000."""
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", make_popen(tmp_path, []))

    def fake_align(argv):
        if aligned is not None:
            aligned.append(argv)
        (tmp_path / "offset_t.txt").write_text("1000", encoding="utf-8")
        return 0

    monkeypatch.setattr(cf, "align_main", fake_align)
    if opened is not None:
        monkeypatch.setattr(cf, "perfetto_open_main", opened.append)


def test_full_pipeline_writes_an_archive_with_the_offset_in_its_manifest(
    monkeypatch, tmp_path
):
    aligned, opened = [], []
    aligned_pipeline(monkeypatch, tmp_path, aligned, opened)
    rc = cf.main(base_args(tmp_path, "--tcbmap", "map.txt", "--open"))
    assert rc == 0
    with tarfile.open(tmp_path / "fused_t.tar") as tar:
        manifest = json.load(tar.extractfile("perfetto_manifest.json"))
        assert tar.extractfile("hw.pftrace").read() == b"HW"
        note = tar.extractfile("note.pftrace").read()
    # note_ns - offset = hw_ns, so Perfetto must move the note trace by -offset
    clocks = manifest["perfetto_manifest"]["files"][1]["clocks"]
    assert clocks["offset_ns"] == -1000
    # the note trace is stored exactly as nxtrace wrote it, on its own clock
    assert wh.parse(note)[0]["ts"] == 5000
    assert wh.parse((tmp_path / "note_t.pftrace").read_bytes())[0]["ts"] == 5000
    assert "--tcbmap" in aligned[0] and "--hw-runs" in aligned[0]
    assert opened and opened[0][0].endswith("fused_t.tar") and opened[0][-1] == "--keep"
    assert not (tmp_path / "hw_t.perfetto").exists()  # the archive holds it


def test_flat_format_moves_the_note_timestamps_and_merges_in_place(
    monkeypatch, tmp_path
):
    aligned_pipeline(monkeypatch, tmp_path)
    assert cf.main(base_args(tmp_path, "--merge-format", "flat")) == 0
    fused = (tmp_path / "fused_t.perfetto").read_bytes()
    assert fused.startswith(b"HW")
    packet = wh.parse(fused[2:])[0]
    assert packet["ts"] == 5000 - 1000  # note_ns - offset = hw_ns
    assert packet["seq"] == 1001
    assert wh.parse((tmp_path / "note_t.pftrace").read_bytes())[0]["ts"] == 5000
    assert not (tmp_path / "hw_t.perfetto").exists()  # merged in place, not copied
    assert not (tmp_path / "fused_t.tar").exists()


def test_keep_parts_keeps_the_hardware_file(monkeypatch, tmp_path):
    aligned_pipeline(monkeypatch, tmp_path)
    assert cf.main(base_args(tmp_path, "--keep-parts")) == 0
    assert (tmp_path / "hw_t.perfetto").read_bytes() == b"HW"
    assert (tmp_path / "fused_t.tar").exists()


def test_archive_falls_back_to_flat_when_the_disk_cannot_hold_the_copy(
    monkeypatch, tmp_path
):
    aligned_pipeline(monkeypatch, tmp_path)
    free = iter([10**12])  # the up-front space check passes, later ones do not
    monkeypatch.setattr(cf.runstore, "free_bytes", lambda _p: next(free, 1))
    assert cf.main(base_args(tmp_path)) == 0
    assert (tmp_path / "fused_t.perfetto").read_bytes().startswith(b"HW")
    assert not (tmp_path / "fused_t.tar").exists()


def test_missing_raw_and_low_disk_stop_before_any_work(monkeypatch, tmp_path):
    args = base_args(tmp_path)
    (tmp_path / "r.bin").unlink()
    with pytest.raises(SystemExit) as e:
        cf.main(args)
    assert "--raw not found" in str(e.value)
    (tmp_path / "r.bin").write_bytes(b"x" * 1000)
    monkeypatch.setattr(cf.runstore, "free_bytes", lambda p: 10)
    with pytest.raises(SystemExit) as e:
        cf.main(args)
    assert "not enough free space" in str(e.value)


def test_alignment_failure_writes_no_merged_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", make_popen(tmp_path, []))
    monkeypatch.setattr(cf, "align_main", lambda _argv: 1)
    cf.main(base_args(tmp_path))
    # no offset: Perfetto could not place the note trace, so nothing is merged
    assert wh.parse((tmp_path / "note_t.pftrace").read_bytes())[0]["ts"] == 5000
    assert (tmp_path / "hw_t.perfetto").exists()
    assert not (tmp_path / "fused_t.perfetto").exists()
    assert not (tmp_path / "fused_t.tar").exists()
    assert "UNALIGNED" in capsys.readouterr().out


def test_missing_note_trace_is_reported(monkeypatch, tmp_path):
    def popen(cmd, **kw):
        if "--itm-note-out" in cmd:
            return make_popen(tmp_path, [])(cmd, **kw)
        return FakeJob()  # nxtrace "ran" but wrote nothing

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", popen)
    monkeypatch.setattr(cf, "align_main", lambda _argv: 1)
    with pytest.raises(SystemExit) as e:
        cf.main(base_args(tmp_path))
    assert "no note trace" in str(e.value)


def test_nxtrace_commands_use_freq_and_pid_names(tmp_path):
    a = cf.parse_args(
        [
            "--raw", "r", "--elf", "e", "--pynuttx", str(tmp_path),
            "--tcbmap", "m.txt", "--out-dir", "o",
        ]  # fmt: skip
    )
    paths = {"notes_bin": "n.bin", "note_pf": "raw.pf"}
    dump, pftrace = cf.nxtrace_commands(a, paths)
    assert dump[-3:] == ["dump", "file", "n.bin"]
    assert pftrace[-4:] == ["-o", "raw.pf", "file", "n.bin"]
    assert "--pid-names" in dump and "--pid-names" in pftrace
    assert dump[dump.index("--freq") + 1] == "150000000"


def test_nxtrace_failure_is_reported_with_its_log(monkeypatch, tmp_path):
    """A missing nxtrace dependency used to end as 'nothing to compare'; the
    cause (nxtrace's stderr) must reach the user."""

    class FailingJob(FakeJob):
        def __init__(self, stderr):
            super().__init__()
            stderr.write("ModuleNotFoundError: No module named 'google'\n")
            stderr.flush()

        def wait(self):
            return 1

    base = make_popen(tmp_path, [])

    def popen(cmd, **kw):
        if "--itm-note-out" in cmd:
            return base(cmd, **kw)
        return FailingJob(kw["stderr"])

    monkeypatch.setattr(cf.subprocess, "run", lambda *a, **_k: None)
    monkeypatch.setattr(cf.subprocess, "Popen", popen)
    with pytest.raises(SystemExit) as e:
        cf.main(base_args(tmp_path))
    message = str(e.value)
    assert "nxtrace failed" in message and "No module named 'google'" in message
    assert str(tmp_path / "nxtrace_t.log") in message


def test_tail_lines(tmp_path):
    f = tmp_path / "log"
    f.write_text("".join(f"line{i}\n" for i in range(30)), encoding="utf-8")
    assert cf.tail_lines(str(f), 2) == "line28\nline29\n"
    assert cf.tail_lines(str(tmp_path / "missing")) == ""
