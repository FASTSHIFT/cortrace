"""Schema-light protobuf rewriting for Perfetto traces.

A Perfetto trace is a stream of length-delimited TracePackets (field 1). Fusing
two traces needs only two edits to a trace we did not write: move every
absolute timestamp by a constant, and renumber the packet sequence ids so two
traces do not share incremental state. Both are done here on the wire format,
with a table of the few field numbers involved, so no .proto bindings (and no
protobuf package) are needed. Fields that are not edited are copied verbatim.

Field numbers are from perfetto/protos/perfetto/trace/perfetto_trace.proto:
  Trace.packet = 1
  TracePacket: ftrace_events = 1, clock_snapshot = 6, timestamp = 8,
               trusted_packet_sequence_id = 10
  ClockSnapshot.clocks = 1;  Clock.timestamp = 2
  FtraceEventBundle: event = 2, last_read_event_timestamp = 9
  FtraceEvent.timestamp = 1
"""

VARINT = 0
LEN = 2

_SHIFT = "shift"

# TracePacket field -> _SHIFT (a varint holding an absolute timestamp) or a
# nested {field: ...} table (a length-delimited sub-message to descend into).
_TIMESTAMPS = {
    8: _SHIFT,
    6: {1: {2: _SHIFT}},
    1: {2: {1: _SHIFT}, 9: _SHIFT},
}
_SEQ_ID_FIELD = 10
_TRACE_PACKET_FIELD = 1
_U64 = 1 << 64


def read_varint(buf, pos):
    """Return (value, next_pos) for the varint at buf[pos]."""
    shift = 0
    value = 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def put_varint(value):
    """Encode a non-negative int as a protobuf varint."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _checked(value):
    if not 0 <= value < _U64:
        raise ValueError(f"timestamp {value} out of range after shifting")
    return value


def _rewrite_message(buf, table, offset):
    """Copy a message, shifting the varints that `table` marks as timestamps."""
    out = bytearray()
    pos = 0
    end = len(buf)
    while pos < end:
        start = pos
        tag, pos = read_varint(buf, pos)
        field, wtype = tag >> 3, tag & 7
        spec = table.get(field)
        if wtype == VARINT:
            value, pos = read_varint(buf, pos)
            if spec == _SHIFT:
                out += put_varint(tag) + put_varint(_checked(value + offset))
            else:
                out += buf[start:pos]
        elif wtype == LEN:
            size, body = read_varint(buf, pos)
            pos = body + size
            if isinstance(spec, dict):
                inner = _rewrite_message(buf[body:pos], spec, offset)
                out += put_varint(tag) + put_varint(len(inner)) + inner
            else:
                out += buf[start:pos]
        elif wtype == 1:  # fixed64
            pos += 8
            out += buf[start:pos]
        elif wtype == 5:  # fixed32
            pos += 4
            out += buf[start:pos]
        else:
            raise ValueError(f"unsupported wire type {wtype}")
    return bytes(out)


def _rewrite_packet(buf, offset, seq_add):
    """Rewrite one TracePacket: shift timestamps, renumber a non-zero seq id."""
    out = bytearray()
    pos = 0
    end = len(buf)
    while pos < end:
        start = pos
        tag, pos = read_varint(buf, pos)
        field, wtype = tag >> 3, tag & 7
        spec = _TIMESTAMPS.get(field)
        if wtype == VARINT:
            value, pos = read_varint(buf, pos)
            if field == _SEQ_ID_FIELD and value and seq_add:
                out += put_varint(tag) + put_varint(value + seq_add)
            elif spec == _SHIFT and offset:
                out += put_varint(tag) + put_varint(_checked(value + offset))
            else:
                out += buf[start:pos]
        elif wtype == LEN:
            size, body = read_varint(buf, pos)
            pos = body + size
            if isinstance(spec, dict) and offset:
                inner = _rewrite_message(buf[body:pos], spec, offset)
                out += put_varint(tag) + put_varint(len(inner)) + inner
            else:
                out += buf[start:pos]
        elif wtype == 1:
            pos += 8
            out += buf[start:pos]
        elif wtype == 5:
            pos += 4
            out += buf[start:pos]
        else:
            raise ValueError(f"unsupported wire type {wtype}")
    return bytes(out)


def rewrite_trace(data, offset_ns=0, seq_add=0):
    """Return `data` (a serialized Perfetto Trace) with every absolute timestamp
    moved by offset_ns and every non-zero trusted_packet_sequence_id raised by
    seq_add. Byte-identical to a protobuf round-trip of the edited message when
    the input was produced by a canonical serializer."""
    out = bytearray()
    pos = 0
    end = len(data)
    while pos < end:
        start = pos
        tag, pos = read_varint(data, pos)
        wtype = tag & 7
        if wtype == LEN:
            size, body = read_varint(data, pos)
            pos = body + size
            if tag >> 3 == _TRACE_PACKET_FIELD and (offset_ns or seq_add):
                inner = _rewrite_packet(data[body:pos], offset_ns, seq_add)
                out += put_varint(tag) + put_varint(len(inner)) + inner
            else:
                out += data[start:pos]
        elif wtype == VARINT:
            _, pos = read_varint(data, pos)
            out += data[start:pos]
        else:
            raise ValueError(f"unsupported top-level wire type {wtype}")
    return bytes(out)
