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

// Build a frame that carries two streams: switch to `s0` for the first half,
// then to `s1` mid-frame. Even byte with LSB=1 is a stream id (id in bits[7:1]);
// aux LSB byte kept 0 so no data-LSB or delayed-switch quirk. This mirrors how
// the CoreSight formatter interleaves ETM(2) and ITM/DWT(1).
TEST(deframe_assemble_width2_roundtrip)
{
    // Width-2: a TPIU byte spans 4 half-symbols (2 bits each), LSB-first, and
    // two consecutive half-symbols pack into one capture byte as
    // {trace_b(hi nibble) , trace_a(lo nibble)}. Build a capture that encodes
    // known bytes and check assemble reconstructs them at phase 0, order lsb.
    auto sym = [](uint8_t b, int j) { return (b >> (2 * j)) & 0x3; }; // half-symbol j of byte b
    std::vector<uint8_t> want = { 0xE4, 0x1B, 0xFF, 0x00 };
    std::vector<uint8_t> cap;
    // half-symbol stream: for each byte, j=0..3 (trace_a then trace_b per cap byte)
    std::vector<uint8_t> syms;
    for (uint8_t b : want)
        for (int j = 0; j < 4; ++j)
            syms.push_back(sym(b, j));
    // pack pairs: cap byte = trace_b<<4 | trace_a (upper 2 bits of each nibble = 0)
    for (std::size_t k = 0; k + 1 < syms.size(); k += 2)
        cap.push_back(static_cast<uint8_t>((syms[k + 1] << 4) | syms[k]));

    DeframePhase p;
    p.width = 2;
    p.parity = 0;
    p.order = 0;
    auto out = assemble_nibbles(cap.data(), cap.size(), p);
    // first want.size() bytes must match
    CHECK(out.size() >= want.size());
    for (std::size_t i = 0; i < want.size(); ++i)
        CHECK_EQ((int)out[i], (int)want[i]);
}

TEST(deframe_multi_demuxes_all_streams)
{
    // Frame: [id s0][d a][d b][d c] ... [id s1][d x][d y] ...
    std::vector<uint8_t> f(16, 0);
    f[0] = static_cast<uint8_t>((2 << 1) | 1); // select stream 2
    f[1] = 0x10; // data -> s2
    f[2] = 0x12; // data -> s2 (even, LSB 0)
    f[3] = 0x14; // data -> s2
    f[4] = static_cast<uint8_t>((1 << 1) | 1); // select stream 1
    f[5] = 0xA0; // data -> s1
    f[6] = 0xA2; // data -> s1
    f[7] = 0xA4; // data -> s1
    // remaining even slots (8,10,12) are data 0x00 on stream 1; keep as 0.
    auto wire = with_sync(f);

    DeframePhase ph { 0, 0 };
    MultiDeframeResult r = tpiu_deframe_multi(wire, ph);
    CHECK_EQ((long)r.syncs, 1L);
    CHECK_EQ((long)r.frames, 1L);

    // stream 2 must contain the 0x10/0x12/0x14 bytes; stream 1 the 0xA0.. ones.
    CHECK(r.streams.count(2) == 1);
    CHECK(r.streams.count(1) == 1);
    const auto& s2 = r.streams[2];
    const auto& s1 = r.streams[1];
    CHECK(s2.size() >= 3);
    CHECK_EQ((int)s2[0], 0x10);
    CHECK_EQ((int)s2[1], 0x12);
    CHECK_EQ((int)s2[2], 0x14);
    CHECK(s1.size() >= 3);
    CHECK_EQ((int)s1[0], 0xA0);
    CHECK_EQ((int)s1[1], 0xA2);
    CHECK_EQ((int)s1[2], 0xA4);

    // src_index is parallel to each stream and strictly increasing (bytes come
    // out in wire order), and points past the 4-byte sync.
    CHECK_EQ((long)r.src_index[2].size(), (long)s2.size());
    CHECK_EQ((long)r.src_index[1].size(), (long)s1.size());
    for (std::size_t i = 1; i < r.src_index[2].size(); ++i)
        CHECK(r.src_index[2][i] > r.src_index[2][i - 1]);
    CHECK(r.src_index[2][0] >= 4); // after the sync word
}
