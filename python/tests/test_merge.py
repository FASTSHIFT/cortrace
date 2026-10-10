"""Tests for cortrace merge: archive (Perfetto manifest) and flat outputs."""

import argparse
import json
import tarfile

import pytest
import wirehelp as wh

from cortrace import merge


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def two_traces(tmp_path, offset=-5000):
    hw = write(tmp_path, "hw.perfetto", b"\x00hardware-bytes\xff" * 100)
    note = write(tmp_path, "note.pftrace", wh.trace(wh.packet(ts=9000, seq=1)))
    return [merge.Trace(hw, "hw"), merge.Trace(note, "note", offset)]


def test_trace_spec_defaults_and_options():
    t = merge.trace_spec("/x/y/hw_run.perfetto")
    assert (t.path, t.machine, t.offset_ns) == ("/x/y/hw_run.perfetto", "hw_run", None)
    t = merge.trace_spec("n.pftrace,machine=note,offset-ns=-42")
    assert (t.machine, t.offset_ns) == ("note", -42)


@pytest.mark.parametrize(
    "text,message",
    [
        ("", "empty trace path"),
        ("a,machine", "key=value"),
        ("a,color=red", "unknown trace option"),
        ("a,offset-ns=soon", "must be an integer"),
    ],
)
def test_trace_spec_rejects_bad_input(text, message):
    with pytest.raises(argparse.ArgumentTypeError) as e:
        merge.trace_spec(text)
    assert message in str(e.value)


def test_validate_rules(tmp_path):
    ok = two_traces(tmp_path)
    merge.validate(ok)
    with pytest.raises(ValueError, match="at least two"):
        merge.validate(ok[:1])
    with pytest.raises(ValueError, match="reference"):
        merge.validate([merge.Trace(ok[0].path, "hw", 5), ok[1]])
    with pytest.raises(ValueError, match="machine name"):
        merge.validate([ok[0], merge.Trace(ok[1].path, "hw", 1)])
    with pytest.raises(ValueError, match="give offset-ns"):
        merge.validate([ok[0], merge.Trace(ok[1].path, "note")])
    with pytest.raises(ValueError, match="not found"):
        merge.validate([ok[0], merge.Trace(str(tmp_path / "nope"), "note", 1)])


def test_manifest_relates_the_clocks_to_the_reference():
    traces = [merge.Trace("a", "hw"), merge.Trace("b", "note", -123)]
    doc = merge.build_manifest(traces)["perfetto_manifest"]
    assert doc["version"] == 1
    assert doc["trace_time"] == {"clock": "BOOTTIME", "file": "hw.pftrace"}
    ref, other = doc["files"][0], doc["files"][1]
    assert ref == {"path": "hw.pftrace", "machine": {"name": "hw"}}
    assert other["path"] == "note.pftrace" and other["machine"] == {"name": "note"}
    assert other["clocks"] == {
        "clock": "BOOTTIME",
        "sync_to": {"file": "hw.pftrace", "clock": "BOOTTIME"},
        "offset_ns": -123,
    }


def test_archive_holds_the_manifest_first_and_the_traces_untouched(tmp_path):
    traces = two_traces(tmp_path, offset=777)
    out = tmp_path / "merged.tar"
    merge.merge(traces, str(out), "archive")
    with tarfile.open(out) as tar:
        assert tar.getnames() == [merge.MANIFEST_NAME, "hw.pftrace", "note.pftrace"]
        manifest = json.load(tar.extractfile(merge.MANIFEST_NAME))
        for t in traces:
            with open(t.path, "rb") as f:
                assert tar.extractfile(f"{t.machine}.pftrace").read() == f.read()
    clocks = manifest["perfetto_manifest"]["files"][1]["clocks"]
    assert clocks["offset_ns"] == 777


def test_flat_moves_the_other_traces_and_renumbers_their_sequences(tmp_path):
    traces = two_traces(tmp_path, offset=-5000)
    hw_bytes = open(traces[0].path, "rb").read()
    out = tmp_path / "merged.perfetto"
    merge.merge(traces, str(out), "flat")
    data = out.read_bytes()
    assert data.startswith(hw_bytes)  # the reference is copied untouched
    packet = wh.parse(data[len(hw_bytes) :])[0]
    assert packet["ts"] == 9000 - 5000 and packet["seq"] == 1 + merge.SEQ_ID_STEP
    assert (tmp_path / "hw.perfetto").exists()  # not in place: reference kept


def test_flat_inplace_appends_to_the_reference_and_renames_it(tmp_path):
    traces = two_traces(tmp_path)
    hw_bytes = open(traces[0].path, "rb").read()
    out = tmp_path / "merged.perfetto"
    merge.merge(traces, str(out), "flat", inplace=True)
    assert not (tmp_path / "hw.perfetto").exists()
    assert out.read_bytes().startswith(hw_bytes)


def test_flat_streams_a_reference_larger_than_one_chunk(tmp_path):
    hw = write(tmp_path, "hw.perfetto", b"\xab" * (merge.CHUNK + 1024))
    note = write(tmp_path, "note.pftrace", b"")
    out = tmp_path / "merged.perfetto"
    merge.merge([merge.Trace(hw, "hw"), merge.Trace(note, "note", 0)], str(out), "flat")
    assert out.stat().st_size == merge.CHUNK + 1024


def test_third_trace_gets_its_own_sequence_range(tmp_path):
    traces = two_traces(tmp_path)
    third = write(tmp_path, "t3.pftrace", wh.trace(wh.packet(ts=1, seq=1)))
    traces.append(merge.Trace(third, "third", 0))
    out = tmp_path / "m.perfetto"
    merge.merge(traces, str(out), "flat")
    seqs = [
        p["seq"]
        for p in wh.parse(out.read_bytes()[len(open(traces[0].path, "rb").read()) :])
    ]
    assert seqs == [1 + merge.SEQ_ID_STEP, 1 + 2 * merge.SEQ_ID_STEP]


def test_unknown_format_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown format"):
        merge.merge(two_traces(tmp_path), str(tmp_path / "o"), "zip")


def test_command_line(tmp_path, capsys):
    traces = two_traces(tmp_path)
    out = tmp_path / "cli.tar"
    rc = merge.main(
        [
            "--trace", traces[0].path + ",machine=hw",
            "--trace", traces[1].path + ",machine=note,offset-ns=-5000",
            "-o", str(out),
        ]
    )  # fmt: skip
    assert rc == 0 and capsys.readouterr().out.strip() == str(out)
    with tarfile.open(out) as tar:
        assert merge.MANIFEST_NAME in tar.getnames()


def test_command_line_reports_problems_as_one_line(tmp_path):
    traces = two_traces(tmp_path)
    with pytest.raises(SystemExit) as e:
        merge.main(["--trace", traces[0].path, "-o", str(tmp_path / "x")])
    assert "at least two" in str(e.value)


# ---- offsets fitted from switch logs -----------------------------------------


def switch_logs(tmp_path, shift=500, other_rows=None):
    """Two switch logs of one run; the second is the first moved by `shift`."""
    rows = [
        (100, "a (pid 1)"),
        (250, "b (pid 2)"),
        (400, "a (pid 1)"),
        (700, "b (pid 2)"),
    ]
    ref = tmp_path / "ref.tsv"
    ref.write_text("".join(f"{t}\t{n}\n" for t, n in rows), encoding="utf-8")
    other = tmp_path / "other.tsv"
    other_rows = (
        other_rows if other_rows is not None else [(t + shift, n) for t, n in rows]
    )
    other.write_text("".join(f"{t}\t{n}\n" for t, n in other_rows), encoding="utf-8")
    return str(ref), str(other)


def fitted_traces(tmp_path, **kw):
    ref_log, other_log = switch_logs(tmp_path, **kw)
    traces = two_traces(tmp_path, offset=None)
    traces[0].switches = ref_log
    traces[1].switches = other_log
    return traces


def test_trace_spec_takes_a_switch_log():
    t = merge.trace_spec("n.pftrace,machine=note,switches=n.tsv")
    assert (t.switches, t.offset_ns) == ("n.tsv", None)


def test_validate_accepts_switch_logs_in_place_of_an_offset(tmp_path):
    merge.validate(fitted_traces(tmp_path))
    traces = fitted_traces(tmp_path)
    traces[0].switches = None  # the reference needs one too
    with pytest.raises(ValueError, match="give offset-ns"):
        merge.validate(traces)
    traces = fitted_traces(tmp_path)
    traces[1].switches = str(tmp_path / "missing.tsv")
    with pytest.raises(ValueError, match="switch log not found"):
        merge.validate(traces)


def test_the_offset_is_fitted_and_put_in_the_manifest(tmp_path):
    traces = fitted_traces(tmp_path, shift=500)
    lines = []
    out = tmp_path / "fitted.tar"
    merge.merge(traces, str(out), "archive", log=lines.append)
    with tarfile.open(out) as tar:
        manifest = json.load(tar.extractfile(merge.MANIFEST_NAME))
    # the other trace reads 500 ns later than the reference for the same
    # switches, so Perfetto must move it 500 ns earlier
    assert manifest["perfetto_manifest"]["files"][1]["clocks"]["offset_ns"] == -500
    assert len(lines) == 1
    assert "4/4 switches (order)" in lines[0] and "offset 500 ns" in lines[0]


def test_flat_format_uses_the_fitted_offset(tmp_path):
    traces = fitted_traces(tmp_path, shift=500)  # the note packet is at ts 9000
    out = tmp_path / "fitted.perfetto"
    merge.merge(traces, str(out), "flat")
    hw_len = len(open(traces[0].path, "rb").read())
    assert wh.parse(out.read_bytes()[hw_len:])[0]["ts"] == 9000 - 500


def test_a_lost_switch_falls_back_to_the_nearest_neighbour_fit(tmp_path):
    rows = [(100, "a (pid 1)"), (250, "b (pid 2)"), (700, "b (pid 2)")]  # one lost
    traces = fitted_traces(
        tmp_path, shift=500, other_rows=[(t + 500, n) for t, n in rows]
    )
    lines = []
    merge.resolve_offsets(traces, log=lines.append)
    assert traces[1].offset_ns == -500 and "(fit)" in lines[0]


def test_logs_that_do_not_match_are_an_error(tmp_path):
    traces = fitted_traces(tmp_path, other_rows=[(100000, "x (pid 9)")])
    with pytest.raises(ValueError, match="no common offset"):
        merge.resolve_offsets(traces)


def test_an_empty_switch_log_is_an_error(tmp_path):
    traces = fitted_traces(tmp_path, other_rows=[])
    with pytest.raises(ValueError, match="nothing to align on"):
        merge.resolve_offsets(traces)


def test_threads_written_by_name_are_matched_through_the_tcbmap(tmp_path):
    ref = tmp_path / "ref.tsv"
    ref.write_text("100\tworker_a\n300\tworker_b\n", encoding="utf-8")
    other = tmp_path / "other.tsv"
    other.write_text("1100\ta (pid 1)\n1300\tb (pid 2)\n", encoding="utf-8")
    tcb = tmp_path / "tcb.txt"
    tcb.write_text("0x1\t1\tworker_a\n0x2\t2\tworker_b\n", encoding="utf-8")
    traces = two_traces(tmp_path, offset=None)
    traces[0].switches, traces[1].switches = str(ref), str(other)
    merge.resolve_offsets(traces, tcbmap=str(tcb))
    assert traces[1].offset_ns == -1000


def test_explicit_offsets_are_not_refitted(tmp_path):
    traces = fitted_traces(tmp_path)
    traces[1].offset_ns = 42
    merge.resolve_offsets(traces)
    assert traces[1].offset_ns == 42


def test_command_line_aligns_and_reports_on_stderr(tmp_path, capsys):
    ref_log, other_log = switch_logs(tmp_path, shift=500)
    hw, note = two_traces(tmp_path, offset=None)
    out = tmp_path / "cli_fit.tar"
    rc = merge.main(
        [
            "--trace", f"{hw.path},machine=hw,switches={ref_log}",
            "--trace", f"{note.path},machine=note,switches={other_log}",
            "-o", str(out),
        ]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert rc == 0 and captured.out.strip() == str(out)
    assert "aligned note to hw" in captured.err
