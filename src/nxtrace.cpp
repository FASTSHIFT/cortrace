// Cortrace — nxtrace DWT data-value packet parser (doc 01 §5.2).
//
// SPDX-License-Identifier: MIT
#include "cortrace/nxtrace.hpp"

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

} // namespace cortrace
