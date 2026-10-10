#!/usr/bin/env python3
"""cortrace_fuse -- one raw parallel-trace capture -> hardware trace + NuttX note
trace, fused on ONE time axis.

The target writes scheduler notes to an ITM stimulus port
(CONFIG_ARMV7M_NOTE_ITM) so they leave through the same parallel TPIU as ETM
and DWT. A single capture therefore carries all three on one time base:

  raw capture  ->  cortrace-decode --raw --nx-switch-stream 1
                     |-- hardware Perfetto (ETM call stacks + thread lanes)
                     |-- --itm-note-out notes.bin (the NuttX note byte stream)
                     `-- --nx-runs-out runs.tsv   (hardware thread switches)
  notes.bin    ->  nxtrace (pynuttx) -> note Perfetto (.pftrace) + text dump
  align_check  ->  constant clock offset between the two (one-to-one pairing)
  merge        ->  the two traces on one timeline: by default a TAR with a
                   Perfetto manifest (fused_<tag>.tar), the traces untouched and
                   Perfetto applying the offset; --merge-format flat gives one
                   fused_<tag>.perfetto with the note timestamps moved instead

This tool does NOT capture: give it a raw file from whatever front end you
use (an FPGA streamer, a probe, ...). Outputs land in --out-dir.

usage: cortrace fuse --raw raw.bin --elf nuttx [--tcbmap map.txt] [--pynuttx DIR]
  nxtrace is taken from the installed pynuttx package, or from --pynuttx /
  $PYNUTTX (a pynuttx checkout). --cortrace-decode defaults to $CORTRACE_DECODE,
  else cortrace-decode on PATH.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time

from . import merge as merge_mod
from . import runstore
from ._paths import find_binary
from .align import main as align_main
from .perfetto_open import main as perfetto_open_main


def fused_name(tag, fmt):
    """File name of the merged trace for a merge format."""
    return f"fused_{tag}.tar" if fmt == "archive" else f"fused_{tag}.perfetto"


def merge_traces(a, paths, offset):
    """Merge hardware and note traces; returns the merged file's path.

    `offset` is note_ns - hw_ns (the fit from `align`), so the note trace is
    moved by -offset on the hardware axis. The archive format copies the
    hardware trace into the TAR; when the disk cannot hold that copy we fall
    back to the flat format, which merges in place.
    """
    traces = [
        merge_mod.Trace(paths["hw"], "hw"),
        merge_mod.Trace(paths["note_pf"], "note", -offset),
    ]
    fmt = a.merge_format
    if fmt == "archive":
        need = sum(os.path.getsize(t.path) for t in traces)
        if runstore.free_bytes(a.out_dir) < need * runstore.HEADROOM:
            print(
                "not enough free space for the archive (it copies the hardware "
                "trace); writing the flat format instead",
                file=sys.stderr,
            )
            fmt = "flat"
    out = os.path.join(a.out_dir, fused_name(a.tag, fmt))
    inplace = fmt == "flat" and not a.keep_parts
    merge_mod.merge(traces, out, fmt, inplace=inplace)
    if fmt == "archive" and not a.keep_parts:
        os.unlink(paths["hw"])  # the archive holds it now
    return out


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--raw", required=True, help="raw parallel-trace capture")
    ap.add_argument("--elf", required=True, help="firmware ELF")
    ap.add_argument(
        "--pynuttx",
        default=os.environ.get("PYNUTTX"),
        help="pynuttx checkout providing nxtrace (default: $PYNUTTX; not needed "
        "when pynuttx is pip-installed)",
    )
    ap.add_argument(
        "--cortrace-decode",
        default=find_binary("CORTRACE_DECODE", "cortrace-decode"),
        help="cortrace-decode binary (default: $CORTRACE_DECODE, else PATH, "
        "else the source tree's build dir)",
    )
    ap.add_argument("--nm", default="arm-none-eabi-nm", help="nm for the ELF symbols")
    ap.add_argument("--width", type=int, choices=(4, 2, 1), default=4)
    ap.add_argument(
        "--sysclk-hz", type=float, default=150e6, help="CPU clock (note timestamps)"
    )
    ap.add_argument(
        "--tsgen-hz",
        type=float,
        default=75e6,
        help="ETM TSGEN clock (hw wall-clock base)",
    )
    ap.add_argument(
        "--itm-port", type=int, default=1, help="CONFIG_ARMV7M_NOTE_ITM_PORT"
    )
    ap.add_argument("--tcbmap", default=None, help="nx_tcbmap.py output (thread names)")
    ap.add_argument(
        "--out-dir",
        default=os.environ.get("CORTRACE_OUT_DIR"),
        help="directory for all outputs (required; default: $CORTRACE_OUT_DIR)",
    )
    ap.add_argument(
        "--keep-parts",
        action="store_true",
        help="keep hw_<tag>.perfetto next to the merged file (costs a second "
        "copy of the hardware trace)",
    )
    ap.add_argument(
        "--merge-format",
        choices=merge_mod.FORMATS,
        default="archive",
        help="archive: fused_<tag>.tar, traces untouched, Perfetto applies the "
        "clock offset (default); flat: one fused_<tag>.perfetto with the note "
        "timestamps moved, for viewers that cannot read archives",
    )
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--open", action="store_true", help="open the result in Perfetto")
    ap.add_argument(
        "--no-fuse",
        action="store_true",
        help="do not merge the hardware and note traces",
    )
    a = ap.parse_args(argv)
    if not a.out_dir:
        ap.error("--out-dir is required (or set $CORTRACE_OUT_DIR)")
    if a.pynuttx:
        a.pynuttx = os.path.abspath(a.pynuttx)
    elif importlib.util.find_spec("nxtrace") is None:
        ap.error(
            "nxtrace not found: pip install pynuttx, or pass --pynuttx DIR / set $PYNUTTX"
        )
    # nxtrace may run with cwd=pynuttx, so every path it sees must be absolute.
    a.raw = os.path.abspath(a.raw)
    a.elf = os.path.abspath(a.elf)
    a.out_dir = os.path.abspath(a.out_dir)
    if a.tcbmap:
        a.tcbmap = os.path.abspath(a.tcbmap)
    return a


def decode_command(a, paths, syms):
    cmd = [
        a.cortrace_decode,
        a.raw,
        syms,
        "--elf",
        a.elf,
        "--raw",
        "--trace-width",
        str(a.width),
        "--memory-limit-mb",
        "0",
        "--nx-switch-stream",
        "1",
        "--hybrid-time",
        "--tsgen-hz",
        str(a.tsgen_hz),
        "--sysclk-hz",
        str(a.sysclk_hz),
        "--itm-note-port",
        str(a.itm_port),
        "--itm-note-unwrap",
        "--itm-note-out",
        paths["notes_bin"],
        "--perf",
        paths["hw"],
        "--nx-runs-out",
        paths["runs"],
    ]
    if a.tcbmap:
        cmd += ["--nx-tcbmap", a.tcbmap]
    return cmd


def start_decode(cmd, on_notes_ready):
    """Run cortrace-decode, echoing its key lines; call on_notes_ready() as soon
    as the note stream file is complete (the decode keeps going for a while)."""
    print("[cortrace-fuse] $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )

    def pump():
        for line in proc.stdout:
            if any(
                k in line
                for k in ("itm notes", "DWT stream", "stream health", "wrote", "error")
            ):
                print("  " + line.strip(), file=sys.stderr)
            if line.startswith("itm notes: port"):
                on_notes_ready()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return proc, thread


def nxtrace_commands(a, paths):
    """nxtrace invocations over the extracted note stream: a text dump (for the
    clock fit) and the note Perfetto trace, both on the note clock."""
    nx = [sys.executable, "-m", "nxtrace", "capture", "--elf", a.elf]
    nx += ["--freq", str(int(a.sysclk_hz))]
    if a.tcbmap:
        nx += ["--pid-names", a.tcbmap]
    dump = nx + ["--format", "dump", "file", paths["notes_bin"]]
    pftrace = nx + ["-o", paths["note_pf"], "file", paths["notes_bin"]]
    return dump, pftrace


def tail_lines(path, count=12):
    """Last `count` lines of a text file (empty string if unreadable)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-count:])
    except OSError:
        return ""


def main(argv=None):
    a = parse_args(argv)
    if not os.path.isfile(a.raw):
        sys.exit(f"--raw not found: {a.raw}")
    runstore.require_space(
        a.out_dir, runstore.estimate_decode_bytes(os.path.getsize(a.raw)), "the decode"
    )
    os.makedirs(a.out_dir, exist_ok=True)
    paths = {
        key: os.path.join(a.out_dir, name.format(tag=a.tag))
        for key, name in (
            ("hw", "hw_{tag}.perfetto"),
            ("notes_bin", "notes_{tag}.bin"),
            ("note_pf", "note_{tag}.pftrace"),
            ("note_txt", "note_{tag}.txt"),
            ("note_log", "nxtrace_{tag}.log"),
            ("runs", "hwruns_{tag}.tsv"),
            ("offset", "offset_{tag}.txt"),
        )
    }

    syms = os.path.join(tempfile.gettempdir(), f"cortrace_fuse_{os.getpid()}.nm")
    with open(syms, "w", encoding="utf-8") as f:
        subprocess.run([a.nm, "-n", a.elf], stdout=f, check=True)

    env = dict(os.environ)
    if a.pynuttx:
        env["PYTHONPATH"] = a.pynuttx + os.pathsep + env.get("PYTHONPATH", "")
    if os.path.exists(paths["note_pf"]):
        os.unlink(paths["note_pf"])  # nxtrace appends to an existing output file
    note_jobs = []

    def start_note_jobs():
        """The note stream is complete long before the hardware decode ends, so
        decode the notes (two nxtrace passes) while the ETM decode still runs."""
        dump, pftrace = nxtrace_commands(a, paths)
        txt = open(paths["note_txt"], "w", encoding="utf-8")
        log = open(paths["note_log"], "w", encoding="utf-8")
        note_jobs.append(
            (subprocess.Popen(dump, cwd=a.pynuttx or None, env=env, stdout=txt,
                              stderr=log), txt)
        )  # fmt: skip
        note_jobs.append(
            (subprocess.Popen(pftrace, cwd=a.pynuttx or None, env=env,
                              stdout=subprocess.DEVNULL, stderr=log), log)
        )  # fmt: skip

    proc, pump_thread = start_decode(decode_command(a, paths, syms), start_note_jobs)
    returncode = proc.wait()
    pump_thread.join()
    failed = 0
    for job, handle in note_jobs:
        failed = failed or job.wait()
        handle.close()
    if failed:
        sys.exit(
            f"nxtrace failed (exit {failed}); is pynuttx installed with its "
            f"dependencies? Last lines of {paths['note_log']}:\n"
            + tail_lines(paths["note_log"])
        )
    if returncode or not os.path.isfile(paths["notes_bin"]):
        sys.exit("decode failed")
    if os.path.getsize(paths["notes_bin"]) == 0:
        sys.exit(
            f"no note bytes on ITM port {a.itm_port} -- is CONFIG_ARMV7M_NOTE_ITM "
            "set and ITM TER enabled?"
        )

    # Fit the constant offset between the two clocks (same capture, so the
    # switches pair up one-to-one; the offset is only the clock origin).
    align = [
        "--hw-runs",
        paths["runs"],
        "--note",
        paths["note_txt"],
        "--offset-out",
        paths["offset"],
    ]
    if a.tcbmap:
        align += ["--tcbmap", a.tcbmap]
    align_ok = align_main(align) == 0 and os.path.isfile(paths["offset"])
    offset = None
    if align_ok:
        with open(paths["offset"], encoding="utf-8") as f:
            offset = int(f.read())

    # The note trace stays on its own clock; the merge places it on the
    # hardware axis (note_ns - offset = hw_ns).
    if not os.path.isfile(paths["note_pf"]):
        sys.exit("nxtrace produced no note trace")

    fused = None
    if offset is not None and not a.no_fuse:
        fused = merge_traces(a, paths, offset)

    if offset is None:
        note_axis = "(UNALIGNED: no clock offset found)"
    else:
        note_axis = f"(own clock; placed {-offset} ns onto the hardware axis)"
    print()
    if fused:
        print(f"fused    : {fused}")
    if not fused or os.path.isfile(paths["hw"]):
        print(f"hardware : {paths['hw']}")
    print(f"note     : {paths['note_pf']}  {note_axis}")
    print(f"note text: {paths['note_txt']}\nraw notes: {paths['notes_bin']}")
    if a.open:
        for f in [fused] if fused else [paths["hw"], paths["note_pf"]]:
            perfetto_open_main([f, "--keep"])
    return 0
