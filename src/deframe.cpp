// Cortrace — FPGA raw-capture front end implementation.
//
// Direct C++ port of the host Python deframe path (dsl_parse.assemble +
// tpiu_official.deframe), byte-for-byte faithful so results match the
// reference. See deframe.hpp for the pipeline overview.
//
// SPDX-License-Identifier: MIT
#include "cortrace/deframe.hpp"

namespace cortrace {

namespace {
    constexpr uint32_t SYNCPATTERN = 0xFFFFFF7Fu;
    constexpr uint8_t HALFSYNC_HIGH = 0x7Fu;
    constexpr uint8_t HALFSYNC_LOW = 0xFFu;
    constexpr int TPIU_PACKET_LEN = 16;
    constexpr uint8_t NO_CHANGE = 0xFFu;
} // namespace

int count_etmv4_async(const uint8_t* data, std::size_t len)
{
    int n = 0;
    int zc = 0;
    for (std::size_t i = 0; i < len; ++i) {
        const uint8_t c = data[i];
        if (c == 0) {
            ++zc;
        } else if (c == 0x80 && zc >= 11) {
            ++n;
            zc = 0;
        } else {
            zc = 0;
        }
    }
    return n;
}

std::vector<uint8_t> assemble_nibbles(
    const uint8_t* raw, std::size_t len, const DeframePhase& phase)
{
    // nibble stream: for each raw byte pair, high nibble of raw[k] then low
    // nibble of raw[k+1] (the {trace_a hi, trace_b lo} capture cadence).
    std::vector<uint8_t> nibs;
    if (len >= 1)
        nibs.reserve(2 * (len - 1));
    for (std::size_t k = 0; k + 1 < len; ++k) {
        nibs.push_back((raw[k] >> 4) & 0xF);
        nibs.push_back(raw[k + 1] & 0xF);
    }

    // pair nibbles into bytes under the phase.
    std::vector<uint8_t> out;
    const std::size_t start = static_cast<std::size_t>(phase.parity);
    if (nibs.size() > start)
        out.reserve((nibs.size() - start) / 2);
    for (std::size_t k = start; k + 1 < nibs.size(); k += 2) {
        const uint8_t n0 = nibs[k];
        const uint8_t n1 = nibs[k + 1];
        out.push_back(phase.order == 0 ? static_cast<uint8_t>((n1 << 4) | n0)
                                       : static_cast<uint8_t>((n0 << 4) | n1));
    }
    return out;
}

namespace {
    // Port of _getPacket: interpret one 16-byte TPIU frame, appending the bytes
    // that belong to `want_stream` to `out`. Returns the updated current stream.
    int get_packet(const uint8_t* rxed, int want_stream, int cur_stream, std::vector<uint8_t>& out)
    {
        int delayed = NO_CHANGE;
        uint8_t lowbits = rxed[TPIU_PACKET_LEN - 1];
        int cur = cur_stream;
        for (int i = 0; i < TPIU_PACKET_LEN; i += 2) {
            if (rxed[i] & 1) {
                // stream change, before or after this data byte
                if (lowbits & 1)
                    delayed = rxed[i] >> 1;
                else
                    cur = rxed[i] >> 1;
            } else {
                if (cur) { // stream 0 = padding, dropped
                    const uint8_t b = static_cast<uint8_t>(rxed[i] | (lowbits & 1));
                    if (want_stream < 0 || cur == want_stream)
                        out.push_back(b);
                }
            }
            // second byte of the pair is always data (for i < 14)
            if (i < 14 && cur) {
                if (want_stream < 0 || cur == want_stream)
                    out.push_back(rxed[i + 1]);
            }
            if (delayed != NO_CHANGE) {
                cur = delayed;
                delayed = NO_CHANGE;
            }
            lowbits >>= 1;
        }
        return cur;
    }
} // namespace

DeframeResult tpiu_deframe(
    const std::vector<uint8_t>& data, int want_stream, const DeframePhase& phase)
{
    DeframeResult r;
    r.phase = phase;

    bool state_synced = false;
    uint32_t sync_monitor = 0;
    uint8_t rxed[TPIU_PACKET_LEN] = { 0 };
    int byte_count = 0;
    bool got_lowbits = false;
    int cur_stream = 0;

    for (std::size_t pos = 0; pos < data.size(); ++pos) {
        const uint8_t d = data[pos];
        sync_monitor = (sync_monitor << 8) | d;
        if (sync_monitor == SYNCPATTERN) {
            state_synced = true;
            byte_count = 0;
            got_lowbits = false;
            ++r.syncs;
            continue;
        }
        if (!state_synced)
            continue;
        if (!got_lowbits) {
            got_lowbits = true;
            rxed[byte_count] = d;
            continue;
        }
        got_lowbits = false;
        if (d == HALFSYNC_HIGH && rxed[byte_count] == HALFSYNC_LOW)
            continue; // halfsync filler, ignore
        ++byte_count;
        rxed[byte_count] = d;
        ++byte_count;
        if (byte_count == TPIU_PACKET_LEN) {
            ++r.frames;
            byte_count = 0;
            cur_stream = get_packet(rxed, want_stream, cur_stream, r.etm);
        }
    }
    r.async_count = count_etmv4_async(r.etm.data(), r.etm.size());
    return r;
}

DeframeResult deframe_raw_capture(
    const uint8_t* raw, std::size_t len, int want_stream, bool search, const DeframePhase& phase)
{
    if (!search) {
        auto data = assemble_nibbles(raw, len, phase);
        return tpiu_deframe(data, want_stream, phase);
    }

    // Try all four (parity, order) phases; keep the best by post-deframe
    // A-sync count, tie-broken by deframed payload size.
    DeframeResult best;
    bool have_best = false;
    for (int parity = 0; parity < 2; ++parity) {
        for (int order = 0; order < 2; ++order) {
            DeframePhase p { parity, order };
            auto data = assemble_nibbles(raw, len, p);
            DeframeResult r = tpiu_deframe(data, want_stream, p);
            const bool better = !have_best || r.async_count > best.async_count
                || (r.async_count == best.async_count && r.etm.size() > best.etm.size());
            if (better) {
                best = std::move(r);
                have_best = true;
            }
        }
    }
    return best;
}

} // namespace cortrace
