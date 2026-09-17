#!/usr/bin/env python3
"""perfetto_record_bridge -- a fake Perfetto `traced` consumer endpoint.

R0 of docs/03-perfetto-record-bridge.md: prove the Consumer IPC link so the
ui.perfetto.dev "Record new trace -> Linux -> Start" flow connects to us and
receives a trace. For R0 we serve a PRE-GENERATED .perfetto (its packets) in
response to ReadBuffers; later phases wire EnableTracing to a live capture.

Topology (see doc 03 S2):
  UI (Consumer) --ws://127.0.0.1:8037/traced--> tracebox websocket_bridge
                --/tmp/perfetto-consumer (UNIX socket)--> THIS fake traced

So run alongside:
  tracebox websocket_bridge         # ws:8037 <-> /tmp/perfetto-consumer
  perfetto_record_bridge.py trace.perfetto   # this file, owns the UNIX socket

Wire protocol (src/ipc/buffered_frame_deserializer.h):
  each frame on the socket is  [uint32 LE size][proto-encoded IPCFrame].

We hand-encode the handful of protobuf messages involved (IPCFrame +
ConsumerPort) instead of pulling protoc: the fields are few and simple. The
TracePacket bytes are lifted straight out of the .perfetto file (a Trace = a
stream of length-delimited TracePackets in field 1), so we don't need the
perfetto proto schema at all -- we re-slice the file's field-1 entries and put
each packet's raw bytes into a ReadBuffersResponse.Slice.

This is a protocol-probe (R0), intentionally minimal and not production code.
"""

import argparse
import os
import socket
import struct
import subprocess
import sys
import threading

CONSUMER_SOCK = "/tmp/perfetto-consumer"

# ---- minimal protobuf wire helpers ---------------------------------------
# Wire types: 0=varint, 2=length-delimited. That's all we need.


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field, wt):
    return _varint((field << 3) | wt)


def _field_varint(field, value):
    return _tag(field, 0) + _varint(value)


def _field_bytes(field, data):
    return _tag(field, 2) + _varint(len(data)) + data


def _field_string(field, s):
    return _field_bytes(field, s.encode("utf-8"))


def _read_varint(buf, pos):
    shift = 0
    result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _parse_fields(buf):
    """Yield (field_number, wire_type, value) where value is int (varint) or
    bytes (length-delimited). Skips other wire types we don't use."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _read_varint(buf, pos)
        field = key >> 3
        wt = key & 7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
            yield field, wt, val
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            yield field, wt, buf[pos : pos + ln]
            pos += ln
        elif wt == 5:
            yield field, wt, buf[pos : pos + 4]
            pos += 4
        elif wt == 1:
            yield field, wt, buf[pos : pos + 8]
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wt} at field {field}")


# ---- ConsumerPort method table -------------------------------------------
# Method ids are arbitrary but must be consistent between BindServiceReply and
# what we match on InvokeMethod. Names must match consumer_port.proto RPCs.
CONSUMER_METHODS = [
    (1, "EnableTracing"),
    (2, "DisableTracing"),
    (3, "ReadBuffers"),
    (4, "FreeBuffers"),
    (5, "Flush"),
    (6, "StartTracing"),
    (7, "ChangeTraceConfig"),
    (8, "GetTraceStats"),
    (9, "ObserveEvents"),
    (10, "QueryServiceState"),
    (11, "QueryCapabilities"),
]
_METHOD_BY_ID = dict(CONSUMER_METHODS)
SERVICE_ID = 1


# ---- .perfetto -> list of TracePacket byte blobs --------------------------
def load_trace_packets(path):
    """A .perfetto file is a Trace proto = repeated TracePacket in field 1.
    Return the raw (already-encoded) bytes of each TracePacket, so we can put
    them straight into ReadBuffers slices without any schema."""
    data = open(path, "rb").read()
    packets = []
    for field, wt, val in _parse_fields(data):
        if field == 1 and wt == 2:
            packets.append(val)
    return packets


# ---- IPCFrame encode helpers ----------------------------------------------
def encode_bind_service_reply(request_id, _service_name):
    # _service_name is the requested service (only ConsumerPort here); the
    # reply advertises the method table regardless, so it's unused.
    # methods: repeated MethodInfo{id=1 varint, name=2 string}
    methods_blob = b""
    for mid, mname in CONSUMER_METHODS:
        mi = _field_varint(1, mid) + _field_string(2, mname)
        methods_blob += _field_bytes(3, mi)
    reply = (
        _field_varint(1, 1)  # success = true
        + _field_varint(2, SERVICE_ID)  # service_id
        + methods_blob
    )
    frame = _field_varint(2, request_id) + _field_bytes(
        4, reply
    )  # msg_bind_service_reply
    return frame


def encode_invoke_reply(request_id, reply_proto=b"", has_more=False, success=True):
    inner = _field_varint(1, 1 if success else 0)
    if has_more:
        inner += _field_varint(2, 1)
    if reply_proto:
        inner += _field_bytes(3, reply_proto)
    frame = _field_varint(2, request_id) + _field_bytes(
        6, inner
    )  # msg_invoke_method_reply
    return frame


def encode_query_service_state_response():
    """QueryServiceStateResponse { TracingServiceState service_state = 1 }.
    The UI reads tracing_service_version (field 5) and supports_tracing_sessions
    (field 7) to fill "Traced version"/"Traced state"; an empty reply makes it
    show "Failed to decode QueryServiceStateResponse". Provide a minimal valid
    state so the record flow proceeds."""
    # TracingServiceState:
    #   num_sessions = 3 (int32), num_sessions_started = 4 (int32),
    #   tracing_service_version = 5 (string), supports_tracing_sessions = 7 (bool)
    state = (
        _field_varint(3, 0)
        + _field_varint(4, 0)
        + _field_string(5, "cortrace-record-bridge 0.1 (fake traced)")
        + _field_varint(7, 1)
    )
    # QueryServiceStateResponse.service_state = 1
    return _field_bytes(1, state)


# Perfetto's IPC has a hard per-message cap (kIPCBufferSize = 128 KiB in
# basic_types.h). A frame larger than that is rejected and the socket is
# auto-disconnected. So ReadBuffers MUST be chunked: it's a streaming RPC, so
# we send many ReadBuffersResponse frames (InvokeMethodReply has_more=true),
# each under the cap, and a final has_more=false to signal EOF.
IPC_MAX_PAYLOAD = 96 * 1024  # under 128 KiB, leaving room for frame overhead


def iter_readbuffers_chunks(packets_bytes, max_payload=IPC_MAX_PAYLOAD):
    """Yield ReadBuffersResponse payloads, each an encoded
    ReadBuffersResponse{ repeated Slice slices = 2 } kept under max_payload.
    Slice{ bytes data = 1; bool last_slice_for_packet = 2 }, one whole
    TracePacket per slice."""
    last = _field_varint(2, 1)
    parts = []
    cur = 0
    for pkt in packets_bytes:
        slice_field = _field_bytes(2, _field_bytes(1, pkt) + last)
        if cur + len(slice_field) > max_payload and parts:
            yield b"".join(parts)
            parts = []
            cur = 0
        parts.append(slice_field)
        cur += len(slice_field)
    if parts:
        yield b"".join(parts)


# ---- frame IO -------------------------------------------------------------
def send_frame(conn, frame_bytes):
    conn.sendall(struct.pack("<I", len(frame_bytes)) + frame_bytes)


class Bridge:
    def __init__(self, packets=None, capture_cb=None, verbose=True):
        # R0: static packets. R1: capture_cb() runs a real capture+decode on
        # EnableTracing and returns the freshly decoded packet list.
        self.packets = packets or []
        self.capture_cb = capture_cb
        self.verbose = verbose
        # R1 async capture: EnableTracing kicks this off, ReadBuffers waits on it.
        self._capture_thread = None
        self._capture_done = threading.Event()
        self._capture_lock = threading.Lock()
        if capture_cb is None:
            self._capture_done.set()  # static R0: packets already present

    def log(self, *a):
        if self.verbose:
            print("[record-bridge]", *a, file=sys.stderr, flush=True)

    def handle_frame(self, conn, frame):
        request_id = 0
        bind_name = None
        invoke = None
        for field, wt, val in _parse_fields(frame):
            if field == 2 and wt == 0:
                request_id = val
            elif field == 3 and wt == 2:  # msg_bind_service
                for f2, w2, v2 in _parse_fields(val):
                    if f2 == 1 and w2 == 2:
                        bind_name = v2.decode("utf-8", "replace")
            elif field == 5 and wt == 2:  # msg_invoke_method
                invoke = val

        if bind_name is not None:
            self.log(f"BindService({bind_name}) req={request_id}")
            send_frame(conn, encode_bind_service_reply(request_id, bind_name))
            return

        if invoke is not None:
            method_id = 0
            for f2, w2, v2 in _parse_fields(invoke):
                if f2 == 2 and w2 == 0:
                    method_id = v2
            name = _METHOD_BY_ID.get(method_id, f"#{method_id}")
            self.log(f"InvokeMethod({name}) req={request_id}")
            self.dispatch(conn, request_id, name)
            return

        self.log(f"unknown frame req={request_id} (ignored)")

    def _start_capture(self, conn, request_id):
        """Run the capture+decode in the background so EnableTracing can return
        immediately (the UI state machine expects Enable to just 'start' the
        session; it then times out durationMs or the user hits Stop, and only
        then does ReadBuffers drain). Blocking inside EnableTracing wedges the
        UI in STOPPING."""

        # Mutual exclusion: the UI may reconnect and fire several EnableTracing
        # in quick succession. Only ONE capture may run at a time (concurrent
        # stream_grab instances fight over the same UDP port -> bind failure).
        def worker():
            try:
                self.log("  capture: running live capture+decode...")
                self.packets = self.capture_cb()
                self.log(f"  capture done: {len(self.packets)} packets ready")
            except Exception as e:  # noqa: BLE001
                self.log(f"  capture FAILED: {e}")
                self.packets = []
            finally:
                self._capture_done.set()
                # The EnableTracing reply is DEFERRED until tracing ends (that's
                # the protocol: EnableTracingResponse{disabled} == "trace over").
                # Sending it now tells the UI recording is done -> it will then
                # issue ReadBuffers. Sending it early (at EnableTracing time)
                # made the UI think tracing ended instantly and it closed the
                # connection before our data was ready.
                try:
                    send_frame(
                        conn,
                        encode_invoke_reply(
                            request_id, _field_varint(1, 1), has_more=False
                        ),
                    )
                    self.log("  sent EnableTracingResponse{disabled} (trace over)")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.log("  could not send EnableTracingResponse (conn gone)")

        with self._capture_lock:
            if self._capture_thread is not None and self._capture_thread.is_alive():
                self.log("  capture already running; ignoring duplicate start")
                return
            self._capture_done.clear()
            self._capture_thread = threading.Thread(target=worker, daemon=True)
            self._capture_thread.start()

    def dispatch(self, conn, request_id, name):
        if name == "ReadBuffers":
            # Wait for the background capture to finish, then stream its packets
            # in <128KiB chunks (IPC cap), one InvokeMethodReply per chunk with
            # has_more=true, then a final has_more=false EOF.
            if not self._capture_done.is_set():
                self.log("  ReadBuffers: waiting for capture to finish...")
                self._capture_done.wait()
            nchunks = 0
            for chunk in iter_readbuffers_chunks(self.packets):
                send_frame(conn, encode_invoke_reply(request_id, chunk, has_more=True))
                nchunks += 1
            send_frame(conn, encode_invoke_reply(request_id, b"", has_more=False))
            self.log(
                f"  -> streamed {len(self.packets)} packets in {nchunks} chunks, EOF"
            )
        elif name == "EnableTracing":
            # DO NOT reply now. Per consumer_port.proto, EnableTracingResponse
            # is sent when tracing STOPS. Kick off the capture; the worker
            # sends the deferred reply when it finishes. (R0 static mode has no
            # capture_cb, so reply immediately as "already done".)
            if self.capture_cb is not None:
                self._start_capture(conn, request_id)
            else:
                send_frame(
                    conn,
                    encode_invoke_reply(
                        request_id, _field_varint(1, 1), has_more=False
                    ),
                )
        elif name in (
            "DisableTracing",
            "FreeBuffers",
            "StartTracing",
            "ChangeTraceConfig",
            "Flush",
            "GetTraceStats",
            "QueryCapabilities",
        ):
            # Empty-but-successful reply is enough.
            send_frame(conn, encode_invoke_reply(request_id, b"", has_more=False))
        elif name == "QueryServiceState":
            # Streaming RPC: one reply carrying a valid TracingServiceState,
            # then EOF. Empty payload -> UI "Failed to decode".
            resp = encode_query_service_state_response()
            send_frame(conn, encode_invoke_reply(request_id, resp, has_more=False))
        elif name == "ObserveEvents":
            # Streaming RPC: one empty reply with EOF.
            send_frame(conn, encode_invoke_reply(request_id, b"", has_more=False))
        else:
            send_frame(conn, encode_invoke_reply(request_id, b"", has_more=False))

    def serve_conn(self, conn):
        buf = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                # frame = [u32 LE size][IPCFrame]
                while len(buf) >= 4:
                    size = struct.unpack("<I", buf[:4])[0]
                    if len(buf) < 4 + size:
                        break
                    frame = buf[4 : 4 + size]
                    buf = buf[4 + size :]
                    self.handle_frame(conn, frame)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            conn.close()
            self.log("connection closed")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="fake Perfetto traced consumer endpoint (R0 static / R1 live)"
    )
    ap.add_argument(
        "trace",
        nargs="?",
        help="R0: pre-generated .perfetto to serve on ReadBuffers",
    )
    ap.add_argument(
        "--capture-cmd",
        help="R1: shell command run on EnableTracing that must WRITE a "
        ".perfetto to --capture-out; its packets are then streamed back",
    )
    ap.add_argument(
        "--capture-out",
        default="/tmp/record_bridge_live.perfetto",
        help="R1: path the --capture-cmd writes the .perfetto to",
    )
    ap.add_argument("--sock", default=CONSUMER_SOCK, help="UNIX socket path")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def make_capture_cb(capture_cmd, capture_out):
    """Build the EnableTracing capture callback: run capture_cmd (which must
    write a .perfetto to capture_out) and return its TracePackets."""

    def _run_capture():
        if os.path.exists(capture_out):
            os.unlink(capture_out)
        print(f"[record-bridge] $ {capture_cmd}", file=sys.stderr, flush=True)
        r = subprocess.run(capture_cmd, shell=True, check=False)
        if r.returncode != 0 or not os.path.isfile(capture_out):
            raise RuntimeError(
                f"capture-cmd failed (rc={r.returncode}) or no {capture_out}"
            )
        return load_trace_packets(capture_out)

    return _run_capture


def build_bridge(a):
    """From parsed args, build the Bridge (R0 static packets or R1 capture_cb).
    Raises SystemExit for an invalid R0 invocation. No sockets here (testable)."""
    if a.capture_cmd:
        print("[record-bridge] R1 live mode: capture on EnableTracing", file=sys.stderr)
        cb = make_capture_cb(a.capture_cmd, a.capture_out)
        return Bridge(capture_cb=cb, verbose=not a.quiet)
    if not a.trace or not os.path.isfile(a.trace):
        raise SystemExit(
            "R0 mode needs an existing <trace>.perfetto (or use --capture-cmd)"
        )
    packets = load_trace_packets(a.trace)
    print(
        f"[record-bridge] R0 static: {len(packets)} packets from {a.trace}",
        file=sys.stderr,
    )
    return Bridge(packets=packets, verbose=not a.quiet)


def main(argv=None):
    a = parse_args(argv)
    bridge = build_bridge(a)

    if os.path.exists(a.sock):
        os.unlink(a.sock)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(a.sock)
    srv.listen(4)
    print(f"[record-bridge] listening on {a.sock} (Ctrl-C to stop)", file=sys.stderr)

    try:
        while True:
            conn, _ = srv.accept()
            bridge.log("new connection")
            threading.Thread(
                target=bridge.serve_conn, args=(conn,), daemon=True
            ).start()
    except KeyboardInterrupt:
        print("\n[record-bridge] stopping", file=sys.stderr)
    finally:
        srv.close()
        if os.path.exists(a.sock):
            os.unlink(a.sock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
