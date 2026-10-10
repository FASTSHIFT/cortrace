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

Where the offset comes from. Give the offset yourself, or give every trace that
is to be aligned a switch log (switches=PATH) and let merge fit it: a switch
log is a text file with one line per context switch-in, "<ns>\\t<name> (pid N)",
on that trace's own clock. The same thread's switches in two logs are paired
and the constant clock offset between the traces follows (see `cortrace
align`). Any producer that can write this file plugs in: a hardware decoder
(cortrace-decode --nx-runs-out), nxtrace (--format switches), a QEMU converter.

usage: cortrace merge -o merged.tar --trace hw.perfetto,machine=hw \\
           --trace note.pftrace,machine=note,offset-ns=-16968614631670
       cortrace merge -o merged.tar --trace hw.perfetto,machine=hw,switches=hw.tsv \\
           --trace note.pftrace,machine=note,switches=note.tsv
"""

import argparse
import io
import json
import os
import statistics
import sys
import tarfile
from dataclasses import dataclass
from typing import Optional

from . import align, wire

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
    switches: Optional[str] = None  # switch log to fit the offset from


def trace_spec(text):
    """argparse type for --trace: PATH[,machine=NAME][,offset-ns=N][,switches=PATH]."""
    path, *options = text.split(",")
    if not path:
        raise argparse.ArgumentTypeError("empty trace path")
    machine = os.path.splitext(os.path.basename(path))[0]
    offset = None
    switches = None
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
        elif key == "switches":
            switches = value
        else:
            raise argparse.ArgumentTypeError(
                f"unknown trace option '{key}' (use machine=, offset-ns= or switches=)"
            )
    return Trace(path, machine, offset, switches)


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
        # Perfetto drops events it cannot place, so an unknown offset must not
        # be left out silently: it is given, or it can be fitted.
        if t.offset_ns is None and not (t.switches and traces[0].switches):
            raise ValueError(
                f"{t.path}: give offset-ns, or switches= on this trace and on "
                "the reference so the offset can be fitted"
            )
    for t in traces:
        if not os.path.isfile(t.path):
            raise ValueError(f"trace not found: {t.path}")
        if t.switches and not os.path.isfile(t.switches):
            raise ValueError(f"switch log not found: {t.switches}")


def resolve_offsets(traces, tcbmap=None, tol_us=30.0, log=None):
    """Fit offset_ns for the traces that gave switches= instead of offset-ns.

    The offset is other_ns - reference_ns for the same switches, negated into
    the manifest meaning (positive = later). `log` receives one summary line
    per fitted trace.
    """
    pending = [t for t in traces[1:] if t.offset_ns is None]
    if not pending:
        return
    name2pid = align.load_tcbmap(tcbmap)
    ref = align.load_switch_log(traces[0].switches, name2pid)
    for t in pending:
        other = align.load_switch_log(t.switches, name2pid)
        if not ref or not other:
            raise ValueError(
                f"{t.machine}: nothing to align on (reference {len(ref)} switches, "
                f"{t.machine} {len(other)})"
            )
        off, res, how = align.fit_offset(ref, other, tol_us * 1000)
        if off is None:
            raise ValueError(
                f"{t.machine}: no common offset with {traces[0].machine} (the two "
                "switch logs do not match; do they cover the same run, and name "
                "the threads the same way? see --tcbmap)"
            )
        t.offset_ns = -off
        if log:
            spread = statistics.pstdev([d for _, d in res]) / 1e3
            log(
                f"aligned {t.machine} to {traces[0].machine}: {len(res)}/{len(ref)} "
                f"switches ({how}), offset {off} ns, residual sd {spread:.3f} us"
            )


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


def merge(
    traces, out, fmt="archive", inplace=False, tcbmap=None, tol_us=30.0, log=None
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Merge `traces` into `out` (see the module docstring); returns `out`.

    inplace only applies to the flat format. Traces given switches= instead of
    offset-ns get their offset fitted first (tcbmap and tol_us tune the fit,
    `log` receives its summary).
    """
    validate(traces)
    resolve_offsets(traces, tcbmap, tol_us, log)
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
        metavar="PATH[,machine=NAME][,offset-ns=N][,switches=LOG]",
        help="an input trace; the first is the reference. Repeat for each "
        "trace (at least two). offset-ns moves the trace later on the "
        "reference's timeline; switches= names a context-switch log from which "
        "the offset is fitted instead (needed on the reference too)",
    )
    ap.add_argument(
        "--tcbmap",
        default=None,
        help="thread name -> pid map (`cortrace tcbmap`), for switch logs whose "
        "threads are not written as 'name (pid N)'",
    )
    ap.add_argument(
        "--tol-us",
        type=float,
        default=30.0,
        help="match tolerance when fitting an offset (default: %(default)s)",
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
        merge(
            a.trace,
            a.out,
            a.format,
            tcbmap=a.tcbmap,
            tol_us=a.tol_us,
            log=lambda line: print(line, file=sys.stderr),
        )
    except ValueError as exc:
        sys.exit(f"cortrace merge: {exc}")
    print(a.out)
    return 0
