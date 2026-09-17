"""Unit tests for perfetto_record_bridge: the hand-rolled protobuf/IPC codec
and the ReadBuffers chunking that keeps frames under Perfetto's 128KiB cap."""

import struct

import pytest

import perfetto_record_bridge as rb


# ---- varint ---------------------------------------------------------------
@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 16384, 2**31, 2**35])
def test_varint_roundtrip(n):
    enc = rb._varint(n)
    dec, pos = rb._read_varint(enc, 0)
    assert dec == n
    assert pos == len(enc)


def test_varint_small_values_single_byte():
    for n in range(128):
        assert len(rb._varint(n)) == 1


# ---- field encoders + parse_fields ----------------------------------------
def test_field_varint_parses_back():
    blob = rb._field_varint(2, 12345)
    fields = list(rb._parse_fields(blob))
    assert fields == [(2, 0, 12345)]


def test_field_bytes_parses_back():
    blob = rb._field_bytes(3, b"\x01\x02\x03")
    fields = list(rb._parse_fields(blob))
    assert fields == [(3, 2, b"\x01\x02\x03")]


def test_field_string_parses_back():
    blob = rb._field_string(4, "ConsumerPort")
    ((field, wt, val),) = rb._parse_fields(blob)
    assert field == 4 and wt == 2
    assert val.decode() == "ConsumerPort"


def test_parse_fields_multiple_and_order():
    blob = rb._field_varint(2, 7) + rb._field_bytes(4, b"hi") + rb._field_varint(1, 1)
    assert list(rb._parse_fields(blob)) == [(2, 0, 7), (4, 2, b"hi"), (1, 0, 1)]


def test_parse_fields_skips_fixed32_and_fixed64():
    # wire type 5 (fixed32) and 1 (fixed64) must advance correctly.
    blob = rb._tag(5, 5) + b"\xaa\xbb\xcc\xdd" + rb._field_varint(6, 9)
    fields = list(rb._parse_fields(blob))
    # last field must still parse correctly after skipping the fixed32
    assert (6, 0, 9) in fields


# ---- BindServiceReply ------------------------------------------------------
def test_bind_service_reply_advertises_all_methods():
    frame = rb.encode_bind_service_reply(1, "ConsumerPort")
    # request_id (field 2) present
    top = dict((f, v) for f, wt, v in rb._parse_fields(frame))
    assert top[2] == 1  # request_id
    reply = top[4]  # msg_bind_service_reply
    methods = {}
    success = None
    for f, wt, v in rb._parse_fields(reply):
        if f == 1:
            success = v
        elif f == 3:  # MethodInfo
            mid = mname = None
            for f2, w2, v2 in rb._parse_fields(v):
                if f2 == 1:
                    mid = v2
                elif f2 == 2:
                    mname = v2.decode()
            methods[mname] = mid
    assert success == 1
    # every ConsumerPort method we declared must be advertised, with matching ids
    name_to_id = {n: i for i, n in rb.CONSUMER_METHODS}
    assert set(methods) == set(name_to_id)
    assert methods == name_to_id


# ---- invoke reply ----------------------------------------------------------
def test_invoke_reply_has_more_flag():
    fr = rb.encode_invoke_reply(9, b"payload", has_more=True)
    top = dict((f, v) for f, wt, v in rb._parse_fields(fr))
    assert top[2] == 9  # request_id
    inner = top[6]  # msg_invoke_method_reply
    got = dict((f, v) for f, wt, v in rb._parse_fields(inner))
    assert got[1] == 1  # success
    assert got[2] == 1  # has_more
    assert got[3] == b"payload"


def test_invoke_reply_eof_omits_has_more():
    fr = rb.encode_invoke_reply(9, b"", has_more=False)
    inner = dict((f, v) for f, wt, v in rb._parse_fields(fr))[6]
    got = dict((f, v) for f, wt, v in rb._parse_fields(inner))
    assert 2 not in got  # has_more not set


# ---- QueryServiceStateResponse --------------------------------------------
def test_query_service_state_has_version_and_flag():
    resp = rb.encode_query_service_state_response()
    # service_state = field 1
    state = dict((f, v) for f, wt, v in rb._parse_fields(resp))[1]
    fields = dict((f, v) for f, wt, v in rb._parse_fields(state))
    assert 5 in fields  # tracing_service_version (string)
    assert fields[7] == 1  # supports_tracing_sessions


# ---- ReadBuffers chunking --------------------------------------------------
def _count_slices(chunk):
    return sum(1 for f, wt, v in rb._parse_fields(chunk) if f == 2 and wt == 2)


def test_chunks_preserve_all_slices():
    packets = [bytes([i % 256]) * 10 for i in range(1000)]
    chunks = list(rb.iter_readbuffers_chunks(packets))
    assert sum(_count_slices(c) for c in chunks) == len(packets)


def test_chunks_respect_size_cap():
    # large packets force multiple chunks; each must stay under the cap.
    packets = [b"x" * 4096 for _ in range(200)]
    chunks = list(rb.iter_readbuffers_chunks(packets, max_payload=64 * 1024))
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 64 * 1024


def test_chunk_slices_carry_packet_bytes():
    packets = [b"\xde\xad\xbe\xef", b"\x01\x02"]
    (chunk,) = list(rb.iter_readbuffers_chunks(packets))
    recovered = []
    for f, wt, v in rb._parse_fields(chunk):  # each Slice
        if f == 2:
            data = None
            last = None
            for f2, w2, v2 in rb._parse_fields(v):
                if f2 == 1:
                    data = v2
                elif f2 == 2:
                    last = v2
            recovered.append(data)
            assert last == 1  # last_slice_for_packet
    assert recovered == packets


def test_empty_packets_yields_no_chunks():
    assert not list(rb.iter_readbuffers_chunks([]))


# ---- load_trace_packets ----------------------------------------------------
def test_load_trace_packets_reads_field1(tmp_path):
    # Build a fake Trace: two TracePackets in field 1.
    p0 = b"\x11\x22\x33"
    p1 = b"\x44"
    trace = rb._field_bytes(1, p0) + rb._field_bytes(1, p1)
    f = tmp_path / "t.perfetto"
    f.write_bytes(trace)
    assert rb.load_trace_packets(str(f)) == [p0, p1]


def test_load_trace_packets_ignores_non_field1(tmp_path):
    trace = (
        rb._field_bytes(1, b"A") + rb._field_varint(9, 123) + rb._field_bytes(1, b"B")
    )
    f = tmp_path / "t.perfetto"
    f.write_bytes(trace)
    assert rb.load_trace_packets(str(f)) == [b"A", b"B"]


# ---- frame framing ---------------------------------------------------------
def test_send_frame_prefixes_le_length():
    class FakeConn:
        def __init__(self):
            self.buf = b""

        def sendall(self, data):
            self.buf += data

    c = FakeConn()
    rb.send_frame(c, b"hello")
    assert c.buf[:4] == struct.pack("<I", 5)
    assert c.buf[4:] == b"hello"


# ---- Bridge dispatch/handle_frame with a fake connection ------------------
class FakeConn:
    """Captures frames written by the bridge and lets us decode them."""

    def __init__(self):
        self.out = b""

    def sendall(self, data):
        self.out += data

    def frames(self):
        """Split the captured stream into [u32 LE size][frame] records."""
        out = []
        buf = self.out
        while len(buf) >= 4:
            size = struct.unpack("<I", buf[:4])[0]
            out.append(buf[4 : 4 + size])
            buf = buf[4 + size :]
        return out


def _make_frame(request_id, *, bind=None, invoke_method_id=None):
    body = rb._field_varint(2, request_id)
    if bind is not None:
        body += rb._field_bytes(3, rb._field_bytes(1, bind.encode()))
    if invoke_method_id is not None:
        invoke = rb._field_varint(1, rb.SERVICE_ID) + rb._field_varint(
            2, invoke_method_id
        )
        body += rb._field_bytes(5, invoke)
    return body


def _reply_inner(frame):
    top = dict((f, v) for f, wt, v in rb._parse_fields(frame))
    return dict((f, v) for f, wt, v in rb._parse_fields(top[6]))


def test_bridge_bind_service_via_handle_frame():
    br = rb.Bridge(packets=[b"\x01"], verbose=False)
    conn = FakeConn()
    br.handle_frame(conn, _make_frame(1, bind="ConsumerPort"))
    frames = conn.frames()
    assert len(frames) == 1
    top = dict((f, v) for f, wt, v in rb._parse_fields(frames[0]))
    assert 4 in top  # msg_bind_service_reply


def test_bridge_readbuffers_static_streams_and_eof():
    packets = [b"\xaa\xbb", b"\xcc"]
    br = rb.Bridge(packets=packets, verbose=False)
    conn = FakeConn()
    rb_id = {n: i for i, n in rb.CONSUMER_METHODS}["ReadBuffers"]
    br.handle_frame(conn, _make_frame(5, invoke_method_id=rb_id))
    frames = conn.frames()
    # last frame is EOF (has_more unset); earlier frames carry slices
    total_slices = 0
    saw_eof = False
    for fr in frames:
        inner = _reply_inner(fr)
        if 2 in inner and inner[2] == 1:
            total_slices += _count_slices(inner[3])
        else:
            saw_eof = True
    assert total_slices == len(packets)
    assert saw_eof


def test_bridge_query_service_state_reply_decodes():
    br = rb.Bridge(packets=[b"\x01"], verbose=False)
    conn = FakeConn()
    qid = {n: i for i, n in rb.CONSUMER_METHODS}["QueryServiceState"]
    br.handle_frame(conn, _make_frame(2, invoke_method_id=qid))
    inner = _reply_inner(conn.frames()[0])
    state = dict((f, v) for f, wt, v in rb._parse_fields(inner[3]))[1]
    fields = dict((f, v) for f, wt, v in rb._parse_fields(state))
    assert 5 in fields  # version string present


def test_bridge_generic_methods_reply_success():
    br = rb.Bridge(packets=[b"\x01"], verbose=False)
    ids = {n: i for i, n in rb.CONSUMER_METHODS}
    for name in ("DisableTracing", "FreeBuffers", "Flush", "GetTraceStats"):
        conn = FakeConn()
        br.handle_frame(conn, _make_frame(3, invoke_method_id=ids[name]))
        inner = _reply_inner(conn.frames()[0])
        assert inner[1] == 1  # success


def test_bridge_capture_cb_runs_on_enable_and_feeds_readbuffers():
    # R1 path without hardware: capture_cb returns synthetic packets.
    calls = {"n": 0}

    def fake_capture():
        calls["n"] += 1
        return [b"\x01\x02", b"\x03"]

    br = rb.Bridge(capture_cb=fake_capture, verbose=False)
    ids = {n: i for i, n in rb.CONSUMER_METHODS}

    # EnableTracing: starts background capture, defers reply until done.
    conn_en = FakeConn()
    br.handle_frame(conn_en, _make_frame(2, invoke_method_id=ids["EnableTracing"]))
    br._capture_done.wait(timeout=5)
    assert calls["n"] == 1
    # deferred EnableTracingResponse{disabled} was sent on completion
    inner = _reply_inner(conn_en.frames()[0])
    assert inner[1] == 1  # disabled = true

    # ReadBuffers now streams the captured packets.
    conn_rb = FakeConn()
    br.handle_frame(conn_rb, _make_frame(3, invoke_method_id=ids["ReadBuffers"]))
    total = 0
    for fr in conn_rb.frames():
        di = _reply_inner(fr)
        if di.get(2) == 1:
            total += _count_slices(di[3])
    assert total == 2


def test_bridge_concurrent_capture_is_mutually_excluded():
    import threading

    started = threading.Event()
    release = threading.Event()

    def slow_capture():
        started.set()
        release.wait(timeout=5)
        return [b"\x01"]

    br = rb.Bridge(capture_cb=slow_capture, verbose=False)
    ids = {n: i for i, n in rb.CONSUMER_METHODS}
    br.handle_frame(FakeConn(), _make_frame(2, invoke_method_id=ids["EnableTracing"]))
    assert started.wait(timeout=5)
    # a second EnableTracing while one is running must not start another
    t0 = br._capture_thread
    br.handle_frame(FakeConn(), _make_frame(2, invoke_method_id=ids["EnableTracing"]))
    assert br._capture_thread is t0  # same thread, no new capture
    release.set()
    br._capture_done.wait(timeout=5)


# ---- serve_conn integration over a real socketpair ------------------------
def test_serve_conn_handles_framed_stream():
    import socket as _socket
    import threading

    br = rb.Bridge(packets=[b"\xaa", b"\xbb"], verbose=False)
    srv_sock, cli_sock = _socket.socketpair()

    t = threading.Thread(target=br.serve_conn, args=(srv_sock,), daemon=True)
    t.start()

    # Send BindService then ReadBuffers as two framed messages.
    rb_id = {n: i for i, n in rb.CONSUMER_METHODS}["ReadBuffers"]
    for body in (
        _make_frame(1, bind="ConsumerPort"),
        _make_frame(2, invoke_method_id=rb_id),
    ):
        cli_sock.sendall(struct.pack("<I", len(body)) + body)

    # Read replies until we see the ReadBuffers EOF, then close.
    cli_sock.settimeout(5)
    got = b""

    def read_frame():
        nonlocal got
        while len(got) < 4:
            got += cli_sock.recv(65536)
        size = struct.unpack("<I", got[:4])[0]
        while len(got) < 4 + size:
            got += cli_sock.recv(65536)
        fr = got[4 : 4 + size]
        got = got[4 + size :]
        return fr

    read_frame()  # BindServiceReply
    total = 0
    saw_eof = False
    while not saw_eof:
        inner = _reply_inner(read_frame())
        if inner.get(2) == 1:
            total += _count_slices(inner[3])
        else:
            saw_eof = True
    assert total == 2
    cli_sock.close()
    srv_sock.close()
    t.join(timeout=5)


# ---- arg parsing / bridge construction ------------------------------------
def test_parse_args_r0_and_r1():
    a0 = rb.parse_args(["some.perfetto"])
    assert a0.trace == "some.perfetto" and a0.capture_cmd is None
    a1 = rb.parse_args(["--capture-cmd", "do it", "--capture-out", "/tmp/x.perfetto"])
    assert a1.capture_cmd == "do it" and a1.capture_out == "/tmp/x.perfetto"


def test_build_bridge_r0_static(tmp_path):
    trace = tmp_path / "t.perfetto"
    trace.write_bytes(rb._field_bytes(1, b"AB") + rb._field_bytes(1, b"C"))
    br = rb.build_bridge(rb.parse_args([str(trace), "--quiet"]))
    assert br.capture_cb is None
    assert br.packets == [b"AB", b"C"]


def test_build_bridge_r0_missing_trace_exits():
    with pytest.raises(SystemExit):
        rb.build_bridge(rb.parse_args([]))


def test_build_bridge_r1_has_capture_cb():
    br = rb.build_bridge(rb.parse_args(["--capture-cmd", "true", "--quiet"]))
    assert br.capture_cb is not None
    assert br.packets == []


def test_make_capture_cb_success(tmp_path):
    out = tmp_path / "cap.perfetto"
    # a command that writes a valid one-packet Trace to out
    payload = rb._field_bytes(1, b"\x2a").hex()
    cmd = f"python3 -c \"open(r'{out}','wb').write(bytes.fromhex('{payload}'))\""
    cb = rb.make_capture_cb(cmd, str(out))
    assert cb() == [b"\x2a"]


def test_make_capture_cb_failure_raises(tmp_path):
    out = tmp_path / "never.perfetto"
    cb = rb.make_capture_cb("false", str(out))  # exits non-zero, writes nothing
    with pytest.raises(RuntimeError):
        cb()
