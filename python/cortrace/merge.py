"""merge -- put Perfetto traces from different producers on one timeline.

cortrace decodes the hardware trace; an OS layer (nxtrace for NuttX, others
later) writes another trace on its own clock. Neither tool needs to know how
the other writes timestamps: the second trace is moved onto the first one's
axis by a constant offset, and Perfetto itself applies it.

Two output formats:

  archive (default)   a TAR holding the traces untouched plus a
                      perfetto_manifest.json that names one "machine" per trace
                      and relates their clocks with offset_ns. trace_processor
                      and the Perfetto UI open it directly; nothing is rewritten
                      and nothing is copied twice. See
                      https://perfetto.dev/docs/analysis/merging-traces
  flat                one Perfetto file: the other traces' timestamps are moved
                      by the offset on the wire format and the packets appended.
                      For viewers that do not read archives, and for
                      `cortrace serve`, which streams packets to the UI.

The first --trace is the reference (the timeline everything is placed on); each
further one carries --offset-ns, the Perfetto manifest meaning: positive moves
that trace LATER on the reference's timeline.

usage: cortrace merge -o merged.tar --trace hw.perfetto,machine=hw \\
           --trace note.pftrace,machine=note,offset-ns=-16968614631670
"""

import argparse
import io
import json
import os
import sys
import tarfile
from dataclasses import dataclass
from typing import Optional

from . import wire

MANIFEST_NAME = "perfetto_manifest.json"
SEQ_ID_STEP = 1000  # each appended trace gets its own range of sequence ids
CHUNK = 16 * 1024 * 1024
FORMATS = ("archive", "flat")
CLOCK = "BOOTTIME"  # the clock every trace claims by default


@dataclass
class Trace:
    """One input trace."""

    path: str
    machine: str
    offset_ns: Optional[int] = None  # None for the reference trace


def trace_spec(text):
    """argparse type for --trace: PATH[,machine=NAME][,offset-ns=N]."""
    path, *options = text.split(",")
    if not path:
        raise argparse.ArgumentTypeError("empty trace path")
    machine = os.path.splitext(os.path.basename(path))[0]
    offset = None
    for option in options:
        key, sep, value = option.partition("=")
        if not sep:
            raise argparse.ArgumentTypeError(f"expected key=value, got '{option}'")
        if key == "machine":
            machine = value
        elif key == "offset-ns":
            try:
                offset = int(value)
            except ValueError as exc:
                raise argparse.ArgumentTypeError(
                    f"offset-ns must be an integer, got '{value}'"
                ) from exc
        else:
            raise argparse.ArgumentTypeError(
                f"unknown trace option '{key}' (use machine= or offset-ns=)"
            )
    return Trace(path, machine, offset)


def validate(traces):
    """Raise ValueError if the traces cannot be merged."""
    if len(traces) < 2:
        raise ValueError("need at least two traces to merge")
    if traces[0].offset_ns is not None:
        raise ValueError(
            "the first trace is the reference and takes no offset-ns; give the "
            "offset on the others"
        )
    names = [t.machine for t in traces]
    if "" in names or len(set(names)) != len(names):
        raise ValueError(
            "every trace needs its own non-empty machine name (traces sharing a "
            f"name are one machine): {names}"
        )
    for t in traces[1:]:
        if t.offset_ns is None:
            # Perfetto drops events it cannot place, so an unknown offset must
            # not be left out silently.
            raise ValueError(f"{t.path}: offset-ns is required")
    for t in traces:
        if not os.path.isfile(t.path):
            raise ValueError(f"trace not found: {t.path}")


def member_name(trace):
    return f"{trace.machine}.pftrace"


def build_manifest(traces):
    """The perfetto_manifest.json document for `traces` (first = reference)."""
    ref = member_name(traces[0])
    files = [{"path": ref, "machine": {"name": traces[0].machine}}]
    for t in traces[1:]:
        files.append(
            {
                "path": member_name(t),
                "machine": {"name": t.machine},
                "clocks": {
                    "clock": CLOCK,
                    "sync_to": {"file": ref, "clock": CLOCK},
                    "offset_ns": t.offset_ns,
                },
            }
        )
    return {
        "perfetto_manifest": {
            "version": 1,
            "trace_time": {"clock": CLOCK, "file": ref},
            "files": files,
        }
    }


def write_archive(traces, out):
    """TAR with the manifest first, then the traces, byte for byte."""
    manifest = json.dumps(build_manifest(traces), indent=2).encode("utf-8")
    with tarfile.open(out, "w") as tar:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        for t in traces:
            tar.add(t.path, arcname=member_name(t))


def write_flat(traces, out, inplace=False):
    """One Perfetto file: the reference followed by the other traces, shifted.

    A Perfetto trace is a sequence of TracePackets, so concatenation is valid
    as long as the packet sequences stay distinct (packets sharing a
    trusted_packet_sequence_id share incremental state); each appended trace
    therefore gets its own sequence-id range. The others are small, so they
    are rewritten in memory while the big reference is only copied.

    inplace=True appends to the reference file and renames it to `out`.
    """
    appended = []
    for i, t in enumerate(traces[1:], start=1):
        with open(t.path, "rb") as f:
            appended.append(
                wire.rewrite_trace(
                    f.read(), offset_ns=t.offset_ns, seq_add=SEQ_ID_STEP * i
                )
            )
    if inplace:
        with open(traces[0].path, "ab") as dst:
            for data in appended:
                dst.write(data)
        os.replace(traces[0].path, out)
        return
    with open(out, "wb") as dst:
        with open(traces[0].path, "rb") as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        for data in appended:
            dst.write(data)


def merge(traces, out, fmt="archive", inplace=False):
    """Merge `traces` into `out` (see the module docstring); returns `out`.

    inplace only applies to the flat format.
    """
    validate(traces)
    if fmt == "archive":
        write_archive(traces, out)
    elif fmt == "flat":
        write_flat(traces, out, inplace=inplace)
    else:
        raise ValueError(f"unknown format '{fmt}'")
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="cortrace merge",
        description="Merge Perfetto traces onto one timeline.",
    )
    ap.add_argument(
        "--trace",
        action="append",
        type=trace_spec,
        required=True,
        metavar="PATH[,machine=NAME][,offset-ns=N]",
        help="an input trace; the first is the reference. Repeat for each "
        "trace (at least two)",
    )
    ap.add_argument("-o", "--out", required=True, help="output file")
    ap.add_argument(
        "--format",
        choices=FORMATS,
        default="archive",
        help="archive: TAR + manifest, traces untouched (default); flat: one "
        "Perfetto file with the timestamps moved",
    )
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    try:
        merge(a.trace, a.out, a.format)
    except ValueError as exc:
        sys.exit(f"cortrace merge: {exc}")
    print(a.out)
    return 0
