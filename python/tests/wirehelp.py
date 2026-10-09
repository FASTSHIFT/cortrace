"""Build and inspect minimal Perfetto Trace bytes for tests (no protobuf needed)."""

from cortrace.wire import put_varint, read_varint


def _tag(field, wtype):
    return put_varint((field << 3) | wtype)


def fvarint(field, value):
    return _tag(field, 0) + put_varint(value)


def flen(field, body):
    return _tag(field, 2) + put_varint(len(body)) + body


def packet(*, ts=None, seq=None, clocks=(), events=(), last_read=None, other=b""):
    """One TracePacket: timestamp, sequence id, clock_snapshot, ftrace_events."""
    body = b""
    if events or last_read is not None:
        bundle = b"".join(flen(2, fvarint(1, e)) for e in events)
        if last_read is not None:
            bundle += fvarint(9, last_read)
        body += flen(1, bundle)
    if clocks:
        body += flen(
            6, b"".join(flen(1, fvarint(1, 6) + fvarint(2, c)) for c in clocks)
        )
    if ts is not None:
        body += fvarint(8, ts)
    if seq is not None:
        body += fvarint(10, seq)
    return flen(1, body + other)


def trace(*packets):
    return b"".join(packets)


def parse(data):
    """Decode a trace built by packet(): list of dicts with the edited fields."""
    out = []
    pos = 0
    while pos < len(data):
        _, pos = read_varint(data, pos)
        size, pos = read_varint(data, pos)
        out.append(_parse_packet(data[pos : pos + size]))
        pos += size
    return out


def _walk(buf):
    pos = 0
    while pos < len(buf):
        tag, pos = read_varint(buf, pos)
        field, wtype = tag >> 3, tag & 7
        if wtype == 0:
            value, pos = read_varint(buf, pos)
        elif wtype == 2:
            size, pos = read_varint(buf, pos)
            value = buf[pos : pos + size]
            pos += size
        elif wtype in (1, 5):
            width = 8 if wtype == 1 else 4
            value = buf[pos : pos + width]
            pos += width
        else:
            raise ValueError(wtype)
        yield field, value


def _parse_packet(buf):
    p = {"ts": None, "seq": None, "clocks": [], "events": [], "last_read": None}
    for field, value in _walk(buf):
        if field == 8:
            p["ts"] = value
        elif field == 10:
            p["seq"] = value
        elif field == 6:
            for f2, clock in _walk(value):
                if f2 == 1:
                    p["clocks"] += [v for f3, v in _walk(clock) if f3 == 2]
        elif field == 1:
            for f2, v in _walk(value):
                if f2 == 2:
                    p["events"] += [t for f3, t in _walk(v) if f3 == 1]
                elif f2 == 9:
                    p["last_read"] = v
    return p
