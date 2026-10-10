"""serve -- click "Start" in the Perfetto UI, get a decoded trace back.

Starts a fake Perfetto `traced` (record_bridge) behind a WebSocket relay
(wsrelay). In ui.perfetto.dev choose "Record new trace" -> "Linux" ->
WebSocket 127.0.0.1:<port> and press Start: the capture pipeline runs (grab,
decode or fuse) and the resulting trace is streamed back into the same page.

Everything is local and stdlib-only; no tracebox and no pynuttx are needed for
the hardware-only mode (--fuse needs nxtrace).
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

from . import capture, record_bridge
from .wsrelay import WsRelay


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="cortrace serve",
        description="serve a Perfetto Record target that captures on Start",
    )
    capture.add_capture_options(ap)
    ap.set_defaults(merge_format="flat")
    ap.add_argument("--port", type=int, default=8037, help="WebSocket port")
    ap.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="extra browser Origin to accept (repeatable)",
    )
    ap.add_argument("--quiet", action="store_true")
    a, extra = ap.parse_known_args(argv)
    if a.merge_format != "flat":
        ap.error("serve streams TracePackets to the UI: use --merge-format flat")
    a = capture.finish_args(ap, a)
    a.extra = extra
    return a


def make_capture_cb(a):
    """EnableTracing callback: run the capture pipeline, return its packets."""

    def run():
        a.tag = time.strftime("%Y%m%d-%H%M%S")  # one file set per Start
        result = capture.run(a, extra=a.extra)
        return record_bridge.load_trace_packets(result)

    return run


def main(argv=None):
    a = parse_args(argv)
    bridge = record_bridge.Bridge(capture_cb=make_capture_cb(a), verbose=not a.quiet)
    sock_dir = tempfile.mkdtemp(prefix="cortrace-serve-")
    os.chmod(sock_dir, 0o700)
    sock_path = os.path.join(sock_dir, "consumer.sock")
    relay = WsRelay(sock_path, port=a.port, extra_origins=a.allow_origin).start()
    print(
        f"[cortrace-serve] open https://ui.perfetto.dev -> Record new trace -> "
        f"Linux -> WebSocket 127.0.0.1:{relay.port}, then press Start",
        file=sys.stderr,
    )
    try:
        record_bridge.serve_forever(bridge, sock_path)
    finally:
        relay.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)
    return 0
