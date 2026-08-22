// Cortrace — Perfetto trace writer implementation.
//
// SPDX-License-Identifier: MIT
#include "cortrace/perfetto_writer.hpp"

#include <cstdio>

namespace cortrace {
namespace {

    // ---- minimal protobuf wire encoding ---------------------------------------
    void put_varint(std::string& o, uint64_t v)
    {
        while (v >= 0x80) {
            o.push_back(static_cast<char>(v | 0x80));
            v >>= 7;
        }
        o.push_back(static_cast<char>(v));
    }

    void put_key(std::string& o, int field, int wire) { put_varint(o, (field << 3) | wire); }

    void put_len(std::string& o, int field, const std::string& s)
    {
        put_key(o, field, 2); // length-delimited
        put_varint(o, s.size());
        o += s;
    }

    void put_uint(std::string& o, int field, uint64_t v)
    {
        put_key(o, field, 0); // varint
        put_varint(o, v);
    }

    // Perfetto proto field numbers (trace_packet.proto / track_event.proto):
    //   TracePacket: timestamp=8, trusted_packet_sequence_id=10, track_event=11,
    //                sequence_flags=13, track_descriptor=60
    //   TrackEvent:  type=9, track_uuid=11, name=23
    //   TrackDescriptor: uuid=1, name=2
    enum { TE_TYPE_SLICE_BEGIN = 1, TE_TYPE_SLICE_END = 2 };

} // namespace

std::string encode_perfetto_trace(
    const std::vector<SliceEvent>& slices, const std::string& track_name, uint64_t track_uuid)
{
    std::string trace;

    // Packet 1: track descriptor.
    {
        std::string td;
        put_uint(td, 1, track_uuid);
        put_len(td, 2, track_name);
        std::string pkt;
        put_len(pkt, 60, td);
        put_len(trace, 1, pkt);
    }

    // Slice packets. The first sequence packet must clear incremental state
    // (sequence_flags=1 = SEQ_INCREMENTAL_STATE_CLEARED) or events may be
    // dropped by the UI.
    bool first = true;
    for (const auto& s : slices) {
        std::string te;
        put_uint(te, 9, s.begin ? TE_TYPE_SLICE_BEGIN : TE_TYPE_SLICE_END);
        put_uint(te, 11, track_uuid);
        if (s.begin)
            put_len(te, 23, s.name);
        std::string pkt;
        put_uint(pkt, 8, s.tick); // timestamp (ns once time base applied)
        put_len(pkt, 11, te);
        put_uint(pkt, 10, 1); // trusted_packet_sequence_id
        if (first) {
            put_uint(pkt, 13, 1); // sequence_flags: incremental state cleared
            first = false;
        }
        put_len(trace, 1, pkt);
    }
    return trace;
}

bool write_perfetto_trace(const std::string& path, const std::vector<SliceEvent>& slices,
    const std::string& track_name, uint64_t track_uuid)
{
    const std::string bytes = encode_perfetto_trace(slices, track_name, track_uuid);
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f)
        return false;
    const std::size_t wrote = std::fwrite(bytes.data(), 1, bytes.size(), f);
    std::fclose(f);
    return wrote == bytes.size();
}

} // namespace cortrace
