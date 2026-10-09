import pytest
import wirehelp as wh
from cortrace import wire


def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 2**32, 2**63 + 5):
        enc = wire.put_varint(n)
        assert wire.read_varint(enc, 0) == (n, len(enc))


def test_varint_too_long_is_rejected():
    with pytest.raises(ValueError):
        wire.read_varint(b"\xff" * 11, 0)


def make():
    return wh.trace(
        wh.packet(ts=100, seq=1, clocks=[5, 6], events=[10, 20], last_read=90),
        wh.packet(seq=0, other=wh.fvarint(99, 7)),  # legacy seq 0, unknown field
        wh.packet(ts=2**40, seq=2),
    )


def test_shift_moves_every_timestamp_and_nothing_else():
    out = wire.rewrite_trace(make(), offset_ns=1000)
    p = wh.parse(out)
    assert p[0] == {
        "ts": 1100, "seq": 1, "clocks": [1005, 1006],
        "events": [1010, 1020], "last_read": 1090,
    }  # fmt: skip
    assert p[1]["ts"] is None and p[1]["seq"] == 0
    assert p[2]["ts"] == 2**40 + 1000


def test_negative_shift_and_roundtrip():
    shifted = wire.rewrite_trace(make(), offset_ns=-4)
    assert wh.parse(shifted)[0]["ts"] == 96
    assert wire.rewrite_trace(shifted, offset_ns=4) == make()


def test_shift_below_zero_is_an_error():
    with pytest.raises(ValueError):
        wire.rewrite_trace(wh.trace(wh.packet(ts=5)), offset_ns=-6)


def test_seq_renumber_skips_zero_and_keeps_timestamps():
    p = wh.parse(wire.rewrite_trace(make(), seq_add=1000))
    assert [x["seq"] for x in p] == [1001, 0, 1002]
    assert p[0]["ts"] == 100 and p[0]["events"] == [10, 20]


def test_unknown_fields_are_copied_verbatim():
    src = wh.trace(
        wh.packet(ts=1, seq=1, other=wh.fvarint(99, 7) + wh.flen(98, b"xyz"))
    )
    out = wire.rewrite_trace(src, offset_ns=1, seq_add=1)
    assert out.endswith(wh.fvarint(99, 7) + wh.flen(98, b"xyz"))


def test_no_edit_is_a_byte_copy():
    assert wire.rewrite_trace(make()) == make()


def test_fixed_width_fields_are_skipped_not_corrupted():
    other = wh._tag(97, 1) + b"\x01" * 8 + wh._tag(96, 5) + b"\x02" * 4  # noqa: SLF001
    src = wh.trace(wh.packet(ts=10, other=other))
    out = wire.rewrite_trace(src, offset_ns=5)
    assert wh.parse(out)[0]["ts"] == 15 and out.endswith(other)


def test_unsupported_wire_type_raises():
    bad = wh.flen(1, wh._tag(50, 3))  # noqa: SLF001  (start-group)
    with pytest.raises(ValueError):
        wire.rewrite_trace(bad, seq_add=1)
