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
    with pytest.raises(ValueError, match="offset-ns is required"):
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
