#!/usr/bin/env python3
"""perfetto_open -- open a Perfetto trace in ui.perfetto.dev with no manual
file wrangling.

Implements the P0 path of docs/01-perfetto-live-bridge.md, using Perfetto's
**postMessage** deep-link protocol (not the #!/?url= direct fetch).

WHY postMessage AND NOT #!/?url= (learned on-target, doc S2.1 update):
  The direct-URL form (https://ui.perfetto.dev/#!/?url=http://127.0.0.1:PORT/..)
  requires the trace to be served over HTTPS. Pointing it at a loopback HTTP
  server makes an HTTPS page (ui.perfetto.dev) fetch a PRIVATE address, which
  modern Chrome (Private Network Access, v130+) blocks BEFORE any request
  leaves the browser -> "TypeError: Failed to fetch", with nothing reaching our
  server. curl works (no PNA), the browser does not. Serving the CORS/PNA
  headers does not help because the block is client-side.

  postMessage sidesteps this entirely: we serve a tiny HOST PAGE from the same
  loopback origin. The host page fetch()es the trace from ITS OWN origin (same
  origin -> no PNA, no CORS), then window.open()s ui.perfetto.dev and hands the
  trace over as an ArrayBuffer via postMessage. No cross-origin private-network
  request ever happens; the trace bytes travel in-memory over the postMessage
  channel. This is Perfetto's documented path for exactly this situation.

Handshake (Perfetto-documented):
  host page --window.open--> ui.perfetto.dev
  host page --postMessage('PING') (repeat)--> UI
  UI        --'PONG'--> host page
  host page --postMessage({perfetto:{buffer,title}})--> UI  (renders)

Usage:
  perfetto_open.py <trace.perfetto> [--port 0] [--keep] [--no-open]
                   [--timeout 120]

  <trace.perfetto>   trace file to open (produced by cortrace-decode --perf)
  --port 0           loopback port (0 = pick a free one; default 0)
  --keep             keep serving after the trace is delivered (allow refresh);
                     default exits once the host page has fetched the trace
  --no-open          do not launch the browser; just print the host-page URL
  --timeout SECS     give up waiting for delivery after SECS (default 120;
                     0 = wait forever)
"""

import argparse
import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse

PERFETTO_UI = "https://ui.perfetto.dev"

# Host page served from our own loopback origin. It fetches the trace from the
# SAME origin (no PNA/CORS), then postMessages it into ui.perfetto.dev. Trace
# path and a "delivered" beacon path are substituted in.
_HOST_PAGE = """<!doctype html>
<html lang="en-us"><head><meta charset="utf-8">
<title>cortrace -> Perfetto</title></head><body>
<pre id="log">cortrace: opening trace in Perfetto...\\n</pre>
<button id="manual" style="display:none;font-size:14px">
Popup blocked - click to open the trace</button>
<script>
const ORIGIN = %(origin)s;
const TRACE_URL = %(trace_url)s;
const BEACON_URL = %(beacon_url)s;
const TITLE = %(title)s;
const log = (m) => { document.getElementById('log').innerText += m + "\\n"; };

async function fetchAndOpen() {
  log("fetching trace (same-origin, no PNA)...");
  const resp = await fetch(TRACE_URL);
  if (!resp.ok) { log("fetch failed: HTTP " + resp.status); return; }
  const buf = await resp.arrayBuffer();
  log("fetched " + buf.byteLength + " bytes; handing to Perfetto...");
  // Tell the local server the bytes are in the browser now.
  fetch(BEACON_URL).catch(() => {});
  openTrace(buf);
}

function openTrace(buf) {
  const win = window.open(ORIGIN);
  if (!win) {
    const b = document.getElementById('manual');
    b.style.display = 'block';
    b.onclick = () => openTrace(buf);
    log("popup blocked - click the button above.");
    return;
  }
  const timer = setInterval(() => win.postMessage('PING', ORIGIN), 50);
  const onMsg = (evt) => {
    if (evt.data !== 'PONG') return;
    clearInterval(timer);
    window.removeEventListener('message', onMsg);
    win.postMessage({perfetto: {buffer: buf, title: TITLE}}, ORIGIN);
    log("delivered. Perfetto is rendering the trace.");
  };
  window.addEventListener('message', onMsg);
}

document.getElementById('manual').onclick = fetchAndOpen;
fetchAndOpen();
</script></body></html>
"""


def _eprint(*args):
    print(*args, file=sys.stderr, flush=True)


def _pick_free_port(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _open_browser(url):
    for opener in ("xdg-open", "open"):
        path = shutil.which(opener)
        if path:
            subprocess.Popen(
                [path, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    try:
        import webbrowser

        return webbrowser.open(url)
    except Exception:
        return False


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the host page, the trace blob, and a delivery beacon.

    All state injected as class attributes by a fresh subclass per run.
    """

    host_html = b""
    trace_bytes = b""
    trace_path = "/trace"
    beacon_path = "/__delivered"
    delivered_event = None
    quiet = False

    # HTTP/1.1 + explicit Content-Length so the browser streams the body cleanly.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not self.quiet:
            _eprint("[perfetto_open] " + (fmt % args))

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        # Same-origin fetches don't need CORS, but harmless to allow.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, self.host_html, "text/html; charset=utf-8")
        elif path == self.trace_path:
            self._send(200, self.trace_bytes, "application/octet-stream")
        elif path == self.beacon_path:
            # Host page reports the trace is now in the browser's memory.
            self._send(204)
            if self.delivered_event is not None:
                self.delivered_event.set()
        else:
            self._send(404)


def serve_and_open(trace_file, port=0, keep=False, do_open=True, timeout=120):
    if not os.path.isfile(trace_file):
        _eprint(f"error: trace file not found: {trace_file}")
        return 2
    with open(trace_file, "rb") as f:
        blob = f.read()
    if not blob:
        _eprint(f"error: trace file is empty: {trace_file}")
        return 2

    bind_ip = "127.0.0.1"
    if port == 0:
        port = _pick_free_port(bind_ip)

    name = os.path.basename(trace_file)
    trace_path = "/" + urllib.parse.quote(name)
    beacon_path = "/__delivered"
    base = f"http://{bind_ip}:{port}"

    def _js(s):
        # JSON-encode a Python string into a safe JS string literal.
        import json

        return json.dumps(s)

    host_html = (
        _HOST_PAGE
        % {
            "origin": _js(PERFETTO_UI),
            "trace_url": _js(base + trace_path),
            "beacon_url": _js(base + beacon_path),
            "title": _js(name),
        }
    ).encode("utf-8")

    delivered = threading.Event()
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "host_html": host_html,
            "trace_bytes": blob,
            "trace_path": trace_path,
            "beacon_path": beacon_path,
            "delivered_event": delivered,
            "quiet": False,
        },
    )

    try:
        httpd = http.server.ThreadingHTTPServer((bind_ip, port), handler)
    except OSError as e:
        _eprint(f"error: cannot bind {bind_ip}:{port}: {e}")
        return 2

    host_url = base + "/"
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    _eprint(
        f"[perfetto_open] serving host page at {host_url} "
        f"(trace {name}, {len(blob)} bytes)"
    )

    if not do_open:
        print(host_url)
        _eprint("[perfetto_open] --no-open: open the URL above in a browser.")
    else:
        _eprint(f"[perfetto_open] opening {host_url}")
        if not _open_browser(host_url):
            _eprint("[perfetto_open] could not launch a browser; open manually:")
            print(host_url)

    rc = 0
    try:
        if do_open:
            wait = None if timeout == 0 else timeout
            if not delivered.wait(timeout=wait):
                _eprint(
                    f"[perfetto_open] timed out after {timeout}s waiting for the "
                    "host page to fetch the trace."
                )
                rc = 1
            else:
                _eprint("[perfetto_open] trace delivered to the browser.")
        if keep:
            _eprint("[perfetto_open] --keep: serving until Ctrl-C.")
            server_thread.join()
    except KeyboardInterrupt:
        _eprint("\n[perfetto_open] interrupted.")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return rc


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Open a Perfetto trace in ui.perfetto.dev via a local "
        "host page + postMessage (survives Chrome Private Network Access).",
    )
    p.add_argument("trace", help="trace file to open (e.g. out.perfetto)")
    p.add_argument("--port", type=int, default=0, help="loopback port (0 = free)")
    p.add_argument(
        "--keep",
        action="store_true",
        help="keep serving after delivery (allow refresh / re-open)",
    )
    p.add_argument(
        "--no-open",
        dest="do_open",
        action="store_false",
        help="print the host-page URL instead of launching a browser",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="seconds to wait for delivery (0 = forever)",
    )
    args = p.parse_args(argv)
    return serve_and_open(
        args.trace,
        port=args.port,
        keep=args.keep,
        do_open=args.do_open,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
