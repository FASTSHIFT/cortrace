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

usage: cortrace_fuse.py --raw raw.bin --elf nuttx --pynuttx DIR [--tcbmap map.txt]
  --pynuttx defaults to $PYNUTTX (a pynuttx checkout providing `nxtrace`),
  --cortrace-decode to $CORTRACE_DECODE, else <repo>/build-rel/cortrace-decode.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_DECODE = os.environ.get(
    "CORTRACE_DECODE", os.path.join(REPO, "build-rel", "cortrace-decode")
)
PERFETTO_OPEN = os.path.join(HERE, "perfetto_open.py")
CHUNK = 16 * 1024 * 1024


def run(cmd, **kw):
    print("[cortrace-fuse] $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    return subprocess.run(cmd, check=False, **kw)


def load_pb2(pynuttx):
    path = os.path.join(pynuttx, "nxtrace", "perfetto_trace_pb2.py")
    spec = importlib.util.spec_from_file_location("perfetto_trace_pb2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fuse(hw, note_pf, out, pb2=None, pynuttx=None):
    """Merge two Perfetto traces into one file.

    A Perfetto trace is a sequence of TracePackets, so concatenating files is a
    valid merge as long as the packet sequences stay distinct: packets sharing
    a trusted_packet_sequence_id share incremental state, and the note trace's
    trace_config / clock_snapshot / state-clearing packets would otherwise land
    on the same sequence as the millions of hardware track events. The note
    trace is small, so its sequence ids are remapped (leaving the big hardware
    file's bytes untouched).
    """
    if pb2 is None:
        pb2 = load_pb2(pynuttx)
    trace = pb2.Trace()
    with open(note_pf, "rb") as f:
        trace.ParseFromString(f.read())
    remapped = 0
    for pkt in trace.packet:
        if pkt.trusted_packet_sequence_id:  # 0 = legacy "no sequence", leave alone
            pkt.trusted_packet_sequence_id += 1000
            remapped += 1
    with open(out, "wb") as dst:
        with open(hw, "rb") as src:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        dst.write(trace.SerializeToString())
    return remapped


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--raw", required=True, help="raw parallel-trace capture")
    ap.add_argument("--elf", required=True, help="firmware ELF")
    ap.add_argument(
        "--pynuttx",
        default=os.environ.get("PYNUTTX"),
        help="pynuttx checkout providing the nxtrace package (default: $PYNUTTX)",
    )
    ap.add_argument(
        "--cortrace-decode",
        default=DEFAULT_DECODE,
        help="cortrace-decode binary (default: $CORTRACE_DECODE, else "
        "<repo>/build-rel/cortrace-decode)",
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
    ap.add_argument("--out-dir", default="perftrace")
    ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--open", action="store_true", help="open the result in Perfetto")
    ap.add_argument(
        "--no-fuse",
        action="store_true",
        help="do not write the merged fused_<tag>.perfetto",
    )
    a = ap.parse_args(argv)
    if not a.pynuttx:
        ap.error("pynuttx checkout not given: pass --pynuttx DIR or set $PYNUTTX")
    # nxtrace runs with cwd=pynuttx, so every path it sees must be absolute.
    a.pynuttx = os.path.abspath(a.pynuttx)
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


def main(argv=None):
    a = parse_args(argv)
    os.makedirs(a.out_dir, exist_ok=True)
    paths = {
        key: os.path.join(a.out_dir, name.format(tag=a.tag))
        for key, name in (
            ("hw", "hw_{tag}.perfetto"),
            ("notes_bin", "notes_{tag}.bin"),
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

    dec = run(decode_command(a, paths, syms), capture_output=True, text=True)
    for line in (dec.stderr + dec.stdout).splitlines():
        if any(
            k in line
            for k in ("itm notes", "DWT stream", "stream health", "wrote", "error")
        ):
            print("  " + line.strip(), file=sys.stderr)
    if dec.returncode or not os.path.isfile(paths["notes_bin"]):
        sys.exit("decode failed")
    if os.path.getsize(paths["notes_bin"]) == 0:
        sys.exit(
            f"no note bytes on ITM port {a.itm_port} -- is CONFIG_ARMV7M_NOTE_ITM "
            "set and ITM TER enabled?"
        )

    env = dict(
        os.environ,
        PYTHONPATH=a.pynuttx + os.pathsep + os.environ.get("PYTHONPATH", ""),
    )
    nx = [sys.executable, "-m", "nxtrace", "capture", "--elf", a.elf]
    nx += ["--freq", str(int(a.sysclk_hz))]
    if a.tcbmap:
        nx += ["--pid-names", a.tcbmap]

    # 1) text dump of the notes, used to fit the hw<->note clock offset.
    with open(paths["note_txt"], "w", encoding="utf-8") as f:
        run(
            nx + ["--format", "dump", "file", paths["notes_bin"]],
            cwd=a.pynuttx,
            env=env,
            stdout=f,
            stderr=subprocess.DEVNULL,
        )

    # 2) fit the constant offset between the two clocks (same capture, so the
    #    switches pair up one-to-one; the offset is only the clock origin).
    align = [
        sys.executable,
        os.path.join(HERE, "align_check.py"),
        "--hw-runs",
        paths["runs"],
        "--note",
        paths["note_txt"],
        "--offset-out",
        paths["offset"],
    ]
    if a.tcbmap:
        align += ["--tcbmap", a.tcbmap]
    align_ok = run(align).returncode == 0 and os.path.isfile(paths["offset"])
    offset = None
    if align_ok:
        with open(paths["offset"], encoding="utf-8") as f:
            offset = int(f.read())

    # 3) note Perfetto, moved onto the hardware time axis when the fit worked.
    if os.path.exists(paths["note_pf"]):
        os.unlink(paths["note_pf"])  # nxtrace appends to an existing output file
    nxo = list(nx)
    if offset is not None:
        nxo += ["--ts-offset-ns", str(-offset)]  # note_ns - offset = hw_ns
    run(
        nxo + ["-o", paths["note_pf"], "file", paths["notes_bin"]],
        cwd=a.pynuttx,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # 4) one file with both: concatenation is a valid Perfetto merge.
    fused = None
    if offset is not None and not a.no_fuse:
        fused = paths["fused"]
        fuse(paths["hw"], paths["note_pf"], fused, pynuttx=a.pynuttx)

    note_axis = "(on the hardware time axis)" if offset is not None else "(UNALIGNED)"
    print(f"\nhardware : {paths['hw']}\nnote     : {paths['note_pf']}  {note_axis}")
    if fused:
        print(f"fused    : {fused}")
    print(f"note text: {paths['note_txt']}\nraw notes: {paths['notes_bin']}")
    if a.open:
        for f in [fused] if fused else [paths["hw"], paths["note_pf"]]:
            run([sys.executable, PERFETTO_OPEN, f, "--keep"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
