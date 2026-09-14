// Cortrace — TimeBase unit tests.
//
// SPDX-License-Identifier: MIT
#include "cortrace/callstack.hpp"
#include "cortrace/timebase.hpp"
#include "test_framework.hpp"

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

using namespace cortrace;

namespace {
// Write a temp time.bin of little-endian uint64 ns values and return its path.
std::string write_tb(const std::vector<uint64_t>& ns)
{
    std::string path = "/tmp/cortrace_tb_test.bin";
    FILE* f = std::fopen(path.c_str(), "wb");
    std::fwrite(ns.data(), sizeof(uint64_t), ns.size(), f);
    std::fclose(f);
    return path;
}
} // namespace

TEST(timebase_lookup_and_clamp)
{
    TimeBase tb;
    CHECK(tb.empty());
    CHECK_EQ((long)tb.ns_for(5), 0L); // empty -> 0

    std::string p = write_tb({ 100, 200, 300, 400 });
    std::size_t n = tb.load(p);
    CHECK_EQ((long)n, 4L);
    CHECK(!tb.empty());
    CHECK_EQ((long)tb.ns_for(0), 100L);
    CHECK_EQ((long)tb.ns_for(2), 300L);
    // past the end clamps to the last entry
    CHECK_EQ((long)tb.ns_for(99), 400L);
}

TEST(timebase_load_missing_throws)
{
    TimeBase tb;
    bool threw = false;
    try {
        tb.load("/nonexistent/path/xyz.bin");
    } catch (const std::exception&) {
        threw = true;
    }
    CHECK(threw);
}

TEST(timebase_apply_replaces_tick_with_ns)
{
    // slice byte_index -> ns via the table; tick field is overwritten.
    std::vector<SliceEvent> slices = {
        { 0, 0, true, "f", 0 }, // byte_index 0 -> 100 ns
        { 1, 2, false, "", 0 }, // byte_index 2 -> 300 ns
    };
    TimeBase tb;
    tb.load(write_tb({ 100, 200, 300 }));
    auto out = apply_timebase(slices, tb);
    CHECK_EQ((long)out.size(), 2L);
    CHECK_EQ((long)out[0].tick, 100L);
    CHECK_EQ((long)out[1].tick, 300L);
    // track field is preserved through apply_timebase
    CHECK_EQ(out[0].track, 0);
}

TEST(timebase_apply_empty_is_identity)
{
    std::vector<SliceEvent> slices = { { 7, 0, true, "f", 1 } };
    TimeBase tb; // empty
    auto out = apply_timebase(slices, tb);
    CHECK_EQ((long)out[0].tick, 7L); // unchanged (monotonic tick order kept)
    CHECK_EQ(out[0].track, 1);
}

TEST(timebase_apply_is_non_decreasing)
{
    // Even if the table maps a later event to a smaller ns, apply_timebase
    // clamps so timestamps never go backwards (Perfetto requires monotonic).
    std::vector<SliceEvent> slices = {
        { 0, 1, true, "a", 0 }, // -> 500
        { 1, 0, false, "", 0 }, // -> 100, but must clamp up to >= 500
    };
    TimeBase tb;
    tb.load(write_tb({ 100, 500 }));
    auto out = apply_timebase(slices, tb);
    CHECK(out[1].tick >= out[0].tick);
}

TEST(etm_timestamp_raw_counts_become_ticks)
{
    // With tsgen_hz=0 the raw TSGEN count is used directly as the tick.
    // SliceEvent = { tick, byte_index, begin, name, track, etm_ts }.
    std::vector<SliceEvent> slices = {
        { 0, 0, true, "a", 0, 1000 },
        { 0, 0, false, "a", 0, 2000 },
    };
    auto out = apply_etm_timestamp(slices, 0.0);
    CHECK_EQ((long)out[0].tick, 1000L);
    CHECK_EQ((long)out[1].tick, 2000L);
}

TEST(etm_timestamp_converts_counts_to_ns)
{
    // tsgen_hz set => tick = count * 1e9 / hz. At 1 MHz, 1 count = 1000 ns.
    std::vector<SliceEvent> slices = {
        { 0, 0, true, "a", 0, 1 },
        { 0, 0, false, "a", 0, 5 },
    };
    auto out = apply_etm_timestamp(slices, 1e6);
    CHECK_EQ((long)out[0].tick, 1000L);
    CHECK_EQ((long)out[1].tick, 5000L);
}

TEST(etm_timestamp_clamps_non_decreasing)
{
    // A backwards TSGEN value (decode glitch) must never move time backwards.
    std::vector<SliceEvent> slices = {
        { 0, 0, true, "a", 0, 5000 }, { 0, 0, false, "a", 0, 4000 }, // must clamp up to >= 5000
    };
    auto out = apply_etm_timestamp(slices, 0.0);
    CHECK(out[1].tick >= out[0].tick);
    CHECK_EQ((long)out[1].tick, 5000L);
}

TEST(etm_timestamp_shared_value_keeps_order)
{
    // Many consecutive slices share one TS value (TS only refreshes at packets);
    // equal ticks are fine and preserve emission order.
    std::vector<SliceEvent> slices = {
        { 0, 0, true, "a", 0, 7 },
        { 0, 0, true, "b", 0, 7 },
        { 0, 0, false, "b", 0, 7 },
    };
    auto out = apply_etm_timestamp(slices, 0.0);
    CHECK_EQ((long)out[0].tick, 7L);
    CHECK_EQ((long)out[1].tick, 7L);
    CHECK_EQ((long)out[2].tick, 7L);
}
