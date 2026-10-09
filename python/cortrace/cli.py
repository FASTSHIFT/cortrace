"""`cortrace` command line: one entry point for decode, capture and fusion."""

import os
import sys

from . import __version__

HELP = """usage: cortrace <command> [options]

commands:
  decode   run cortrace-decode (ETM/DWT/ITM -> Perfetto), arguments passed through
  capture  grab a raw capture from the FPGA streamer, decode (or --fuse) and open
  serve    Perfetto "Record" target: press Start in the UI to capture and view
  fuse     one raw capture -> hardware trace + NuttX notes fused on one time axis
  align    fit the clock offset between a hardware and a note thread-switch list
  tcbmap   dump the live pid -> thread-name map through OpenOCD
  open     open a Perfetto trace in ui.perfetto.dev
  fpga     FPGA streamer tools: ctrl (CSR writes), net (find the link), health
  version  print the version

Run `cortrace <command> --help` for the options of a command.
"""


def _decode(args):
    from ._paths import find_binary

    binary = find_binary("CORTRACE_DECODE", "cortrace-decode")
    try:
        os.execv(binary, [binary] + args)
    except OSError as exc:
        print(f"cortrace: cannot run {binary}: {exc}", file=sys.stderr)
        return 127
    return 0  # not reached


def _lazy(module, attr="main"):
    def run(args):
        import importlib

        return getattr(importlib.import_module(f"cortrace.{module}"), attr)(args)

    return run


def _fpga(args):
    tools = {"ctrl": "fpga.ctrl", "net": "fpga.net", "health": "fpga.health"}
    if not args or args[0] not in tools:
        print(f"usage: cortrace fpga {{{'|'.join(tools)}}} [options]", file=sys.stderr)
        return 2
    return _lazy(tools[args[0]])(args[1:])


COMMANDS = {
    "decode": _decode,
    "capture": _lazy("capture"),
    "serve": _lazy("serve"),
    "fuse": _lazy("fuse"),
    "align": _lazy("align"),
    "tcbmap": _lazy("tcbmap"),
    "open": _lazy("perfetto_open"),
    "fpga": _fpga,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0 if argv else 2
    cmd = argv[0]
    if cmd in ("version", "--version", "-V"):
        print(f"cortrace {__version__}")
        return 0
    handler = COMMANDS.get(cmd)
    if handler is None:
        print(f"cortrace: unknown command '{cmd}'\n\n{HELP}", end="", file=sys.stderr)
        return 2
    return handler(argv[1:]) or 0
