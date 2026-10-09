"""capture -- grab a raw trace from the FPGA streamer, then decode or fuse it.

  1. (optional) set the TPIU port width on the FPGA        (fpga.ctrl set-width)
  2. stream N seconds of UDP trace into a file              (cortrace-grab)
  3. hardware-only: decode raw -> Perfetto                  (cortrace-decode)
     --fuse:        decode + NuttX notes fused on one axis  (cortrace fuse)
  4. optionally open the result in ui.perfetto.dev

The output directory is always given by the user (--out-dir or
$CORTRACE_OUT_DIR): a 75 MB/s capture plus its decode is several hundred MB per
second of trace, so cortrace never picks a place on its own.

This tool does not arm the target's trace hardware (ETM/DWT/ITM); do that with
your debugger first and leave it attached.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time

from . import fuse as fuse_mod
from ._paths import find_binary
from .fpga import ctrl
from .perfetto_open import main as perfetto_open_main


def add_capture_options(ap):
    """Options shared by `capture` and `serve` (everything but --raw-in/--open)."""
    ap.add_argument(
        "--out-dir",
        default=os.environ.get("CORTRACE_OUT_DIR"),
        help="directory for raw captures and Perfetto files (required; "
        "default: $CORTRACE_OUT_DIR)",
    )
    ap.add_argument("--elf", required=True, help="firmware ELF")
    ap.add_argument(
        "--iface", default=os.environ.get("CORTRACE_IFACE"), help="capture NIC"
    )
    ap.add_argument("--secs", type=float, default=1.0, help="capture seconds")
    ap.add_argument(
        "--width",
        type=int,
        choices=(4, 2, 1),
        default=None,
        help="set the FPGA TPIU port width before capture (default: leave as-is)",
    )
    ap.add_argument("--tag", default=None, help="file name suffix (default: timestamp)")
    ap.add_argument(
        "--fuse", action="store_true", help="also decode the NuttX notes and fuse"
    )
    ap.add_argument("--nm", default="arm-none-eabi-nm", help="nm for the ELF symbols")
    ap.add_argument(
        "--time-base",
        choices=("cycle", "etm", "hybrid"),
        default="cycle",
        help="hardware-only time base (--fuse always uses hybrid)",
    )
    ap.add_argument("--sysclk-hz", type=float, default=150e6)
    ap.add_argument("--tsgen-hz", type=float, default=None)
    ap.add_argument("--nx-switch-stream", type=int, default=1)
    ap.add_argument("--tcbmap", default=None, help="thread-name map (cortrace tcbmap)")
    ap.add_argument("--phase", default=None, help="lock deframe phase, e.g. 1,0")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="cortrace capture",
        description="capture -> decode (or fuse) -> open in Perfetto",
    )
    add_capture_options(ap)
    ap.add_argument(
        "--raw-in", default=None, help="skip the capture; use this raw file"
    )
    ap.add_argument("--open", action="store_true", help="open the result in Perfetto")
    a, extra = ap.parse_known_args(argv)
    return finish_args(ap, a), extra


def finish_args(ap, a):
    """Validate shared options and fill derived ones."""
    if not a.out_dir:
        ap.error("--out-dir is required (or set $CORTRACE_OUT_DIR)")
    if not getattr(a, "raw_in", None) and not a.iface:
        ap.error("capture NIC not given: pass --iface or set $CORTRACE_IFACE")
    a.out_dir = os.path.abspath(a.out_dir)
    a.elf = os.path.abspath(a.elf)
    a.tag = a.tag or time.strftime("%Y%m%d-%H%M%S")
    return a


def log(msg):
    print(f"[cortrace-capture] {msg}", file=sys.stderr)


def run_cmd(cmd, **kw):
    log("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=False, **kw)


def grab(a, raw):
    """Stream a.secs seconds from the FPGA into `raw`."""
    if a.width is not None:
        iface_args = ["--iface", a.iface] if a.iface else []
        if ctrl.main(iface_args + ["set-width", str(a.width)]) != 0:
            sys.exit("set-width failed")
    grab_bin = find_binary("CORTRACE_GRAB", "cortrace-grab")
    cmd = [grab_bin, a.iface, str(a.secs), raw, "256", "512"]
    if run_cmd(cmd).returncode != 0:
        sys.exit("cortrace-grab failed")


def decode_command(a, raw, perf, syms):
    """cortrace-decode command line for a hardware-only decode."""
    width = a.width or 4
    if a.time_base == "cycle":
        base = ["--cycle-time", "--sysclk-hz", str(a.sysclk_hz)]
    elif a.time_base == "etm":
        if not a.tsgen_hz:
            sys.exit("--time-base etm needs --tsgen-hz")
        base = ["--etm-time", "--tsgen-hz", str(a.tsgen_hz)]
    else:
        if not a.tsgen_hz:
            sys.exit("--time-base hybrid needs --tsgen-hz")
        base = [
            "--hybrid-time",
            "--tsgen-hz",
            str(a.tsgen_hz),
            "--sysclk-hz",
            str(a.sysclk_hz),
        ]
    cmd = [
        find_binary("CORTRACE_DECODE", "cortrace-decode"),
        raw,
        syms,
        "--elf",
        a.elf,
        "--raw",
        "--trace-width",
        str(width),
        "--memory-limit-mb",
        "0",
        "--nx-switch-stream",
        str(a.nx_switch_stream),
    ]
    cmd += base + ["--perf", perf]
    if a.phase:
        cmd += ["--phase", a.phase]
    if a.tcbmap:
        cmd += ["--nx-tcbmap", a.tcbmap]
    return cmd


def decode_hw(a, raw):
    """Hardware-only decode; returns the Perfetto path."""
    perf = os.path.join(a.out_dir, f"hw_{a.tag}.perfetto")
    syms = os.path.join(tempfile.gettempdir(), f"cortrace_capture_{os.getpid()}.nm")
    with open(syms, "w", encoding="utf-8") as f:
        subprocess.run([a.nm, "-n", a.elf], stdout=f, check=True)
    proc = run_cmd(decode_command(a, raw, perf, syms), capture_output=True, text=True)
    for line in (proc.stderr + proc.stdout).splitlines():
        if any(k in line for k in ("stream health", "begins / ends", "wrote", "error")):
            log("  " + line.strip())
    if proc.returncode != 0 or not os.path.isfile(perf):
        sys.exit("decode failed")
    return perf


def fuse_args(a, raw, extra):
    """Arguments handing the raw capture to `cortrace fuse`."""
    args = ["--raw", raw, "--elf", a.elf, "--out-dir", a.out_dir, "--tag", a.tag]
    args += ["--width", str(a.width or 4), "--sysclk-hz", str(a.sysclk_hz)]
    args += ["--nm", a.nm]
    if a.tsgen_hz:
        args += ["--tsgen-hz", str(a.tsgen_hz)]
    if a.tcbmap:
        args += ["--tcbmap", a.tcbmap]
    return args + list(extra)


def run(a, raw_in=None, extra=()):
    """Run the whole pipeline; returns the path of the file to look at."""
    os.makedirs(a.out_dir, exist_ok=True)
    if raw_in:
        raw = os.path.abspath(raw_in)
        if not os.path.isfile(raw):
            sys.exit(f"--raw-in not found: {raw}")
    else:
        raw = os.path.join(a.out_dir, f"raw_{a.tag}.bin")
        grab(a, raw)
    if a.fuse:
        rc = fuse_mod.main(fuse_args(a, raw, extra))
        if rc != 0:
            sys.exit("fuse failed")
        fused = os.path.join(a.out_dir, f"fused_{a.tag}.perfetto")
        if os.path.isfile(fused):
            return fused
        log("clock alignment failed: no fused file, using the hardware trace")
        return os.path.join(a.out_dir, f"hw_{a.tag}.perfetto")
    return decode_hw(a, raw)


def main(argv=None):
    a, extra = parse_args(argv)
    result = run(a, raw_in=a.raw_in, extra=extra)
    print(result)
    if a.open:
        return perfetto_open_main([result, "--keep"])
    return 0
