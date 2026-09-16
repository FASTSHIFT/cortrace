// Cortrace — nxtrace DWT data-value packet parser (doc 01 §5.2).
//
// SPDX-License-Identifier: MIT
#include "cortrace/nxtrace.hpp"

#include <cstdio>

namespace cortrace {

namespace {
    // SS field (bits[1:0]) -> payload byte count. SS=00 is a protocol packet,
    // not a source packet.
    inline int ss_to_size(uint8_t hdr)
    {
        switch (hdr & 0x03) {
        case 1:
            return 1;
        case 2:
            return 2;
        case 3:
            return 4;
        default:
            return 0;
        }
    }
} // namespace

std::vector<DwtEvent> parse_dwt_data_values(
    const std::vector<uint8_t>& bytes, const std::vector<std::size_t>& src_index)
{
    std::vector<DwtEvent> out;
    const bool have_src = src_index.size() == bytes.size();
    const std::size_t n = bytes.size();
    std::size_t i = 0;

    while (i < n) {
        const uint8_t hdr = bytes[i];

        // Source packet: SS != 00 (bits[1:0] give the payload size).
        const int size = ss_to_size(hdr);
        if (size != 0) {
            // DWT data-value packet: bits[7:6]=10 (0x80) and bit2=1 (hardware
            // source). bit3 = WnR (1 = write). Comparator id in bits[5:4].
            const bool is_data_value = (hdr & 0xC0) == 0x80;
            const bool is_hardware = (hdr & 0x04) != 0;
            const bool is_write = (hdr & 0x08) != 0;

            if (i + 1 + static_cast<std::size_t>(size) <= n) {
                if (is_data_value && is_hardware && is_write) {
                    uint32_t v = 0;
                    for (int b = 0; b < size; ++b)
                        v |= static_cast<uint32_t>(bytes[i + 1 + b]) << (8 * b);
                    DwtEvent e;
                    e.src_index = have_src ? src_index[i] : i;
                    e.comparator = static_cast<uint8_t>((hdr >> 4) & 0x03);
                    e.size = static_cast<uint8_t>(size);
                    e.value = v;
                    out.push_back(e);
                }
                i += 1 + static_cast<std::size_t>(size);
                continue;
            }
            break; // truncated payload at end of stream
        }

        // Protocol packet (SS == 00). Walk it so we stay byte-aligned.
        if (hdr == 0x00) {
            // Synchronization / idle: a run of zero bytes. Skip one at a time.
            ++i;
            continue;
        }
        // Local timestamp LTS2 (single byte): bit7=0, bits[3:0]=0000, TS in
        // bits[6:4] (1..6). Matches 0x0TTT_0000 with the low nibble zero.
        if ((hdr & 0x8F) == 0x00) {
            ++i;
            continue;
        }
        // Local timestamp LTS1 / other continuation packets: bits[7:6]=11,
        // bits[3:0]=0000; a header byte followed by continuation payload bytes
        // (bit7=1 continues). Skip header then continuation bytes.
        if ((hdr & 0xCF) == 0xC0) {
            std::size_t j = i + 1;
            while (j < n && (bytes[j] & 0x80))
                ++j;
            if (j < n)
                ++j; // include the final (bit7=0) payload byte
            i = j;
            continue;
        }
        // Extension / overflow / other single-byte protocol packet: skip one.
        ++i;
    }
    return out;
}

namespace {
    // Default resolver: no target memory / ELF, just format the pointer. The
    // tid is derived from the pointer so the same TCB maps to the same track.
    ThreadId default_resolve(uint32_t value)
    {
        ThreadId t;
        // Fold the pointer to a small stable id (low bits are enough to
        // separate the handful of concurrent TCBs in practice).
        t.tid = static_cast<int>((value >> 3) & 0x7FFF);
        char buf[24];
        std::snprintf(buf, sizeof(buf), "tcb@0x%08x", value);
        t.name = buf;
        return t;
    }
} // namespace

std::vector<ThreadRun> build_thread_runs(const std::vector<DwtEvent>& events,
    ThreadId (*resolve)(uint32_t), uint8_t watch_comp, std::size_t stream_end_src)
{
    if (!resolve)
        resolve = default_resolve;

    std::vector<ThreadRun> runs;
    const ThreadRun* prev = nullptr;
    for (const auto& e : events) {
        if (e.comparator != watch_comp)
            continue;
        if (!runs.empty())
            runs.back().end_src = e.src_index; // close the previous run here
        ThreadRun r;
        r.begin_src = e.src_index;
        r.end_src = e.src_index; // left open until the next switch
        r.tcb = e.value;
        r.id = resolve(e.value);
        runs.push_back(r);
        (void)prev;
    }
    if (!runs.empty() && stream_end_src > runs.back().begin_src)
        runs.back().end_src = stream_end_src;
    return runs;
}

std::vector<SliceEvent> thread_runs_to_slices(
    const std::vector<ThreadRun>& runs, int track, std::map<int, std::string>& track_names)
{
    std::vector<SliceEvent> slices;
    track_names[track] = "Threads";
    uint64_t tick = 0;
    for (const auto& r : runs) {
        // skip zero-length (still-open final) runs so Perfetto gets a closed slice
        if (r.end_src <= r.begin_src)
            continue;
        SliceEvent b;
        b.tick = tick++;
        b.byte_index = r.begin_src;
        b.begin = true;
        b.name = r.id.name;
        b.track = track;
        slices.push_back(b);

        SliceEvent e;
        e.tick = tick++;
        e.byte_index = r.end_src;
        e.begin = false;
        e.name = r.id.name;
        e.track = track;
        slices.push_back(e);
    }
    return slices;
}

} // namespace cortrace
