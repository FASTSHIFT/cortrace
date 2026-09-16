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
_METHOD_BY_ID = {i: n for i, n in CONSUMER_METHODS}
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
def encode_bind_service_reply(request_id, service_name):
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
    frame = _field_varint(2, request_id) + _field_bytes(4, reply)  # msg_bind_service_reply
    return frame


def encode_invoke_reply(request_id, reply_proto=b"", has_more=False, success=True):
    inner = _field_varint(1, 1 if success else 0)
    if has_more:
        inner += _field_varint(2, 1)
    if reply_proto:
        inner += _field_bytes(3, reply_proto)
    frame = _field_varint(2, request_id) + _field_bytes(6, inner)  # msg_invoke_method_reply
    return frame


def encode_readbuffers_response(packets_bytes):
    """ReadBuffersResponse { repeated Slice slices = 2; }
    Slice { bytes data = 1; bool last_slice_for_packet = 2; }
    One slice per whole TracePacket, each marked last_slice_for_packet."""
    blob = b""
    for pkt in packets_bytes:
        slice_msg = _field_bytes(1, pkt) + _field_varint(2, 1)
        blob += _field_bytes(2, slice_msg)
    return blob


# ---- frame IO -------------------------------------------------------------
def send_frame(conn, frame_bytes):
    conn.sendall(struct.pack("<I", len(frame_bytes)) + frame_bytes)


class Bridge:
    def __init__(self, packets, verbose=True):
        self.packets = packets
        self.verbose = verbose

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

    def dispatch(self, conn, request_id, name):
        if name == "ReadBuffers":
            # Stream all packets in one response, then EOF (has_more=false).
            resp = encode_readbuffers_response(self.packets)
            send_frame(conn, encode_invoke_reply(request_id, resp, has_more=True))
            send_frame(conn, encode_invoke_reply(request_id, b"", has_more=False))
            self.log(f"  -> streamed {len(self.packets)} packets, EOF")
        elif name in ("EnableTracing", "DisableTracing", "FreeBuffers",
                      "StartTracing", "ChangeTraceConfig", "Flush",
                      "GetTraceStats", "QueryCapabilities"):
            # Empty-but-successful reply is enough for R0.
            send_frame(conn, encode_invoke_reply(request_id, b"", has_more=False))
        elif name in ("QueryServiceState", "ObserveEvents"):
            # Streaming RPCs: send one empty reply with EOF.
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


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="fake Perfetto traced consumer endpoint (R0 protocol probe)"
    )
    ap.add_argument("trace", help="pre-generated .perfetto to serve on ReadBuffers")
    ap.add_argument("--sock", default=CONSUMER_SOCK, help="UNIX socket path")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.trace):
        sys.exit(f"trace not found: {a.trace}")
    packets = load_trace_packets(a.trace)
    print(f"[record-bridge] loaded {len(packets)} TracePackets from {a.trace}",
          file=sys.stderr)

    if os.path.exists(a.sock):
        os.unlink(a.sock)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(a.sock)
    srv.listen(4)
    print(f"[record-bridge] listening on {a.sock} (Ctrl-C to stop)", file=sys.stderr)

    bridge = Bridge(packets, verbose=not a.quiet)
    try:
        while True:
            conn, _ = srv.accept()
            bridge.log("new connection")
            threading.Thread(target=bridge.serve_conn, args=(conn,), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[record-bridge] stopping", file=sys.stderr)
    finally:
        srv.close()
        if os.path.exists(a.sock):
            os.unlink(a.sock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
