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
  fuse         ->  note trace shifted onto the hardware axis, appended to the
                   hardware file as fused_<tag>.perfetto

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

from . import runstore, wire
from ._paths import find_binary
from .align import main as align_main
from .perfetto_open import main as perfetto_open_main

CHUNK = 16 * 1024 * 1024
SEQ_ID_OFFSET = 1000  # note-trace packet sequence ids are raised by this


def fuse(hw, note_pf, out, inplace=False):
    """Merge two Perfetto traces into one file.

    A Perfetto trace is a sequence of TracePackets, so concatenating files is a
    valid merge as long as the packet sequences stay distinct: packets sharing
    a trusted_packet_sequence_id share incremental state, and the note trace's
    trace_config / clock_snapshot / state-clearing packets would otherwise land
    on the same sequence as the millions of hardware track events. The note
    trace is small, so its sequence ids are renumbered (leaving the big
    hardware file's bytes untouched).

    inplace=True appends the note packets to `hw` and renames it to `out`, so
    the (often 350+ MB) hardware trace is not copied and not stored twice.
    """
    with open(note_pf, "rb") as f:
        note = wire.rewrite_trace(f.read(), seq_add=SEQ_ID_OFFSET)
    if inplace:
        with open(hw, "ab") as dst:
            dst.write(note)
        os.replace(hw, out)
        return
    with open(out, "wb") as dst:
        with open(hw, "rb") as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        dst.write(note)


def shift_trace_file(src, dst, offset_ns):
    """Write src (a Perfetto trace) to dst with all timestamps moved by offset_ns."""
    with open(src, "rb") as f:
        data = f.read()
    with open(dst, "wb") as f:
        f.write(wire.rewrite_trace(data, offset_ns=offset_ns))


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
        help="keep hw_<tag>.perfetto next to fused_<tag>.perfetto (costs a second "
        "copy of the hardware trace)",
    )
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--open", action="store_true", help="open the result in Perfetto")
    ap.add_argument(
        "--no-fuse",
        action="store_true",
        help="do not write the merged fused_<tag>.perfetto",
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
    pftrace = nx + ["-o", paths["note_raw"], "file", paths["notes_bin"]]
    return dump, pftrace


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
            ("note_raw", "note_{tag}.raw.pftrace"),
            ("note_pf", "note_{tag}.pftrace"),
            ("note_txt", "note_{tag}.txt"),
            ("runs", "hwruns_{tag}.tsv"),
            ("offset", "offset_{tag}.txt"),
            ("fused", "fused_{tag}.perfetto"),
        )
    }

    syms = os.path.join(tempfile.gettempdir(), f"cortrace_fuse_{os.getpid()}.nm")
    with open(syms, "w", encoding="utf-8") as f:
        subprocess.run([a.nm, "-n", a.elf], stdout=f, check=True)

    env = dict(os.environ)
    if a.pynuttx:
        env["PYTHONPATH"] = a.pynuttx + os.pathsep + env.get("PYTHONPATH", "")
    if os.path.exists(paths["note_raw"]):
        os.unlink(paths["note_raw"])  # nxtrace appends to an existing output file
    note_jobs = []

    def start_note_jobs():
        """The note stream is complete long before the hardware decode ends, so
        decode the notes (two nxtrace passes) while the ETM decode still runs."""
        dump, pftrace = nxtrace_commands(a, paths)
        txt = open(paths["note_txt"], "w", encoding="utf-8")
        note_jobs.append(
            (subprocess.Popen(dump, cwd=a.pynuttx or None, env=env, stdout=txt,
                              stderr=subprocess.DEVNULL), txt)
        )  # fmt: skip
        note_jobs.append(
            (subprocess.Popen(pftrace, cwd=a.pynuttx or None, env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL), None)
        )  # fmt: skip

    proc, pump_thread = start_decode(decode_command(a, paths, syms), start_note_jobs)
    returncode = proc.wait()
    pump_thread.join()
    for job, txt in note_jobs:
        job.wait()
        if txt:
            txt.close()
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

    # Note Perfetto, moved onto the hardware time axis when the fit worked
    # (note_ns - offset = hw_ns).
    if not os.path.isfile(paths["note_raw"]):
        sys.exit("nxtrace produced no note trace")
    shift_trace_file(paths["note_raw"], paths["note_pf"], -(offset or 0))
    os.unlink(paths["note_raw"])

    # One file with both: concatenation is a valid Perfetto merge.
    fused = None
    if offset is not None and not a.no_fuse:
        fused = paths["fused"]
        fuse(paths["hw"], paths["note_pf"], fused, inplace=not a.keep_parts)

    note_axis = "(on the hardware time axis)" if offset is not None else "(UNALIGNED)"
    print()
    if fused:
        print(f"fused    : {fused}")
    if not fused or a.keep_parts:
        print(f"hardware : {paths['hw']}")
    print(f"note     : {paths['note_pf']}  {note_axis}")
    print(f"note text: {paths['note_txt']}\nraw notes: {paths['notes_bin']}")
    if a.open:
        for f in [fused] if fused else [paths["hw"], paths["note_pf"]]:
            perfetto_open_main([f, "--keep"])
    return 0
