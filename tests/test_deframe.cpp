// Cortrace — deframe (nibble reassemble + TPIU) unit tests.
//
// SPDX-License-Identifier: MIT
#include "cortrace/deframe.hpp"
#include "test_framework.hpp"

#include <cstdint>
#include <vector>

using namespace cortrace;

namespace {

// Build one 16-byte TPIU frame that carries `payload` bytes all tagged with
// `stream`, then the raw wire bytes preceded by the 4-byte sync pattern.
// Frame layout (CoreSight formatter, see tpiuDecoder.c): even bytes are either
// a stream-id (LSB=1, id in bits[7:1]) or a data byte (LSB=0), the final byte
// holds the collected LSBs, and here we keep it simple: first even byte selects
// the stream, the rest are data with LSB clear.
std::vector<uint8_t> make_frame_stream(uint8_t stream, const std::vector<uint8_t>& payload)
{
    std::vector<uint8_t> f(16, 0);
    // byte 0: stream select (id<<1 | 1)
    f[0] = static_cast<uint8_t>((stream << 1) | 1);
    // byte 1: first data byte (pairs with byte 0's slot)
    // bytes 2..14: data; byte 15 = aux LSBs (keep 0 so no data LSB is set and
    // no late stream change is triggered).
    std::size_t pi = 0;
    for (int i = 1; i < 15 && pi < payload.size(); ++i) {
        // avoid setting LSB on even data bytes (that would be read as a stream
        // id); payload values in tests are chosen even.
        f[i] = payload[pi++];
    }
    return f;
}

std::vector<uint8_t> with_sync(const std::vector<uint8_t>& frame)
{
    std::vector<uint8_t> s = { 0xFF, 0xFF, 0xFF, 0x7F };
    s.insert(s.end(), frame.begin(), frame.end());
    return s;
}

} // namespace

TEST(deframe_count_async_basic)
{
    // 11 zeros then 0x80 = one A-sync; 10 zeros + 0x80 = none.
    std::vector<uint8_t> one(11, 0);
    one.push_back(0x80);
    CHECK_EQ(count_etmv4_async(one.data(), one.size()), 1);

    std::vector<uint8_t> none(10, 0);
    none.push_back(0x80);
    CHECK_EQ(count_etmv4_async(none.data(), none.size()), 0);

    // two back-to-back A-syncs
    std::vector<uint8_t> two;
    for (int r = 0; r < 2; ++r) {
        for (int i = 0; i < 12; ++i)
            two.push_back(0);
        two.push_back(0x80);
    }
    CHECK_EQ(count_etmv4_async(two.data(), two.size()), 2);
}

TEST(deframe_assemble_phase_order)
{
    // raw bytes -> nibbles: hi(raw[k]), lo(raw[k+1]).
    // raw = {0xAB, 0xCD, 0xEF}: nibs = [A, D, C, F] (hi 0xAB=A, lo 0xCD=D,
    //   hi 0xCD=C, lo 0xEF=F).
    std::vector<uint8_t> raw = { 0xAB, 0xCD, 0xEF };

    // parity=0, order=0: pair (A,D)->(D<<4|A)=0xDA, (C,F)->0xFC
    DeframePhase p00 { 0, 0 };
    auto a00 = assemble_nibbles(raw.data(), raw.size(), p00);
    CHECK_EQ((long)a00.size(), 2L);
    CHECK_EQ((int)a00[0], 0xDA);
    CHECK_EQ((int)a00[1], 0xFC);

    // parity=0, order=1: (A,D)->(A<<4|D)=0xAD, (C,F)->0xCF
    DeframePhase p01 { 0, 1 };
    auto a01 = assemble_nibbles(raw.data(), raw.size(), p01);
    CHECK_EQ((int)a01[0], 0xAD);
    CHECK_EQ((int)a01[1], 0xCF);

    // parity=1 drops the first nibble: nibs[1:]=[D,C,F] -> one pair (D,C)
    DeframePhase p10 { 1, 0 };
    auto a10 = assemble_nibbles(raw.data(), raw.size(), p10);
    CHECK_EQ((long)a10.size(), 1L);
    CHECK_EQ((int)a10[0], 0xCD); // order0: (C<<4|D)=0xCD
}

TEST(deframe_tpiu_extracts_requested_stream)
{
    // A frame tagged stream 2 with even data payload; deframe want_stream=2
    // should return that payload, and want_stream=1 should return nothing.
    std::vector<uint8_t> payload = { 0x10, 0x20, 0x30, 0x40 };
    auto wire = with_sync(make_frame_stream(2, payload));

    DeframePhase ph { 0, 0 };
    DeframeResult r2 = tpiu_deframe(wire, 2, ph);
    CHECK_EQ((long)r2.syncs, 1L);
    CHECK_EQ((long)r2.frames, 1L);
    // the payload bytes must appear in order in the extracted stream
    bool found = false;
    for (std::size_t i = 0; i + payload.size() <= r2.etm.size(); ++i) {
        bool eq = true;
        for (std::size_t j = 0; j < payload.size(); ++j)
            if (r2.etm[i + j] != payload[j]) {
                eq = false;
                break;
            }
        if (eq) {
            found = true;
            break;
        }
    }
    CHECK(found);

    DeframeResult r1 = tpiu_deframe(wire, 1, ph);
    CHECK_EQ((long)r1.etm.size(), 0L); // nothing tagged stream 1
}

TEST(deframe_search_picks_a_phase)
{
    // A raw capture with no real sync yields empty streams under every phase;
    // the search must still return a valid (defaulted) result without crashing.
    std::vector<uint8_t> raw(64, 0x00);
    DeframePhase def;
    DeframeResult r = deframe_raw_capture(raw.data(), raw.size(), 2, /*search=*/true, def);
    CHECK_EQ((long)r.etm.size(), 0L);
    // phase fields are within range
    CHECK(r.phase.parity == 0 || r.phase.parity == 1);
    CHECK(r.phase.order == 0 || r.phase.order == 1);
}

TEST(deframe_locked_phase_skips_search)
{
    std::vector<uint8_t> payload = { 0x22, 0x44 };
    auto wire = with_sync(make_frame_stream(2, payload));
    // feed as already-assembled bytes by using parity/order that are identity
    // on byte-aligned input is not possible; instead exercise the no-search
    // path via tpiu_deframe directly above. Here just ensure the API runs.
    DeframePhase ph { 1, 0 };
    DeframeResult r = deframe_raw_capture(wire.data(), wire.size(), 2, /*search=*/false, ph);
    CHECK_EQ(r.phase.parity, 1);
    CHECK_EQ(r.phase.order, 0);
}
