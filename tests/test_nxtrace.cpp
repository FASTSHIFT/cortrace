// Cortrace — nxtrace DWT data-value parser unit tests.
//
// SPDX-License-Identifier: MIT
#include "cortrace/nxtrace.hpp"
#include "test_framework.hpp"

#include <cstdint>
#include <vector>

using namespace cortrace;

namespace {

// DWT data-value WRITE packet header: bits[7:6]=10, bit3=1 (write), bit2=1
// (hardware), bits[5:4]=comparator, bits[1:0]=SS. For comparator 0, 4-byte
// write: 0x80 | 0x08 | 0x04 | 0x03 = 0x8F.
uint8_t dwt_write_hdr(int comp, int ss)
{
    return static_cast<uint8_t>(0x80 | 0x08 | 0x04 | ((comp & 3) << 4) | (ss & 3));
}

void push_le(std::vector<uint8_t>& v, uint32_t val, int n)
{
    for (int i = 0; i < n; ++i)
        v.push_back(static_cast<uint8_t>((val >> (8 * i)) & 0xFF));
}

} // namespace

TEST(nxtrace_parses_single_write_value)
{
    std::vector<uint8_t> s;
    s.push_back(dwt_write_hdr(0, 3)); // comp 0, 4 bytes
    push_le(s, 0x24000400u, 4);

    auto ev = parse_dwt_data_values(s, {});
    CHECK_EQ((long)ev.size(), 1L);
    CHECK_EQ((int)ev[0].comparator, 0);
    CHECK_EQ((int)ev[0].size, 4);
    CHECK_EQ((long)(unsigned long)ev[0].value, (long)0x24000400L);
}

TEST(nxtrace_recovers_incrementing_sequence)
{
    // Mirror the on-board §11.8 result: a run of comparator-0 4-byte writes with
    // a monotonically +1 payload, interleaved with local-timestamp packets.
    std::vector<uint8_t> s;
    for (uint32_t v = 0x496e8b; v < 0x496e8b + 8; ++v) {
        s.push_back(dwt_write_hdr(0, 3));
        push_le(s, v, 4);
        // valid LTS1: header (bits[7:6]=11, low nibble 0) + continuation bytes;
        // 0x83 has bit7=1 (more follow), 0x02 has bit7=0 (final).
        s.push_back(0xC0); // LTS1 header
        s.push_back(0x83); // continuation (bit7=1 -> more)
        s.push_back(0x02); // final payload byte (bit7=0)
    }
    auto ev = parse_dwt_data_values(s, {});
    CHECK_EQ((long)ev.size(), 8L);
    for (std::size_t i = 0; i + 1 < ev.size(); ++i)
        CHECK_EQ((long)(ev[i + 1].value - ev[i].value), 1L);
}

TEST(nxtrace_ignores_read_and_sw_packets)
{
    std::vector<uint8_t> s;
    // A software-source packet (bit2=0): should be skipped, not parsed as DWT.
    s.push_back(static_cast<uint8_t>(0x80 | 0x01)); // data-value-ish but SW source, 1B
    s.push_back(0xAA);
    // A DWT READ (bit3=0): not a write, skip.
    s.push_back(static_cast<uint8_t>(0x80 | 0x04 | 0x03)); // hardware, read, 4B
    push_le(s, 0xDEADBEEF, 4);
    // A real DWT write.
    s.push_back(dwt_write_hdr(1, 3));
    push_le(s, 0x20001000u, 4);

    auto ev = parse_dwt_data_values(s, {});
    CHECK_EQ((long)ev.size(), 1L);
    CHECK_EQ((int)ev[0].comparator, 1);
    CHECK_EQ((long)(unsigned long)ev[0].value, (long)0x20001000L);
}

TEST(nxtrace_maps_source_index)
{
    std::vector<uint8_t> s;
    s.push_back(dwt_write_hdr(0, 3));
    push_le(s, 0x11223344u, 4);
    // src_index parallel to s: pretend the header byte came from assembled
    // offset 100.
    std::vector<std::size_t> src = { 100, 101, 102, 103, 104 };

    auto ev = parse_dwt_data_values(s, src);
    CHECK_EQ((long)ev.size(), 1L);
    CHECK_EQ((long)ev[0].src_index, 100L);
}
