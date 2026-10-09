// Cortrace — TimeBase implementation.
//
// SPDX-License-Identifier: MIT
#include "cortrace/timebase.hpp"

#include <cstdio>
#include <stdexcept>

namespace cortrace {

std::size_t TimeBase::load(const std::string& path)
{
    FILE* f = std::fopen(path.c_str(), "rb");
    if (!f)
        throw std::runtime_error("cannot open time base: " + path);
    std::fseek(f, 0, SEEK_END);
    const long sz = std::ftell(f);
    std::fseek(f, 0, SEEK_SET);
    if (sz > 0) {
        ns_.resize(static_cast<std::size_t>(sz) / sizeof(uint64_t));
        const std::size_t rd = std::fread(ns_.data(), sizeof(uint64_t), ns_.size(), f);
        ns_.resize(rd);
    }
    std::fclose(f);
    return ns_.size();
}

std::vector<SliceEvent> apply_timebase(const std::vector<SliceEvent>& slices, const TimeBase& tb)
{
    if (tb.empty())
        return slices;
    std::vector<SliceEvent> out = slices;
    // Perfetto requires timestamps to be non-decreasing within a sequence; the
    // byte-indexed base is monotonic, but clamp defensively so equal-byte
    // events keep their emission order.
    uint64_t last = 0;
    for (auto& s : out) {
        uint64_t ns = tb.ns_for(s.byte_index);
        if (ns < last)
            ns = last;
        s.tick = ns;
        last = ns;
    }
    return out;
}

std::vector<SliceEvent> apply_etm_timestamp(const std::vector<SliceEvent>& slices, double tsgen_hz)
{
    std::vector<SliceEvent> out = slices;
    uint64_t last = 0;
    for (auto& s : out) {
        uint64_t t = s.etm_ts;
        if (tsgen_hz > 0.0)
            t = static_cast<uint64_t>(static_cast<double>(s.etm_ts) * 1e9 / tsgen_hz);
        // Clamp non-decreasing: timestamps only refresh at TS packets, so many
        // consecutive slices share a value; that is fine (equal ticks keep
        // emission order), but a decode glitch must never move time backwards.
        if (t < last)
            t = last;
        s.tick = t;
        last = t;
    }
    return out;
}

std::vector<SliceEvent> apply_cycle_time(const std::vector<SliceEvent>& slices, double sysclk_hz)
{
    std::vector<SliceEvent> out = slices;
    uint64_t last = 0;
    for (auto& s : out) {
        uint64_t t = s.cycle_clock;
        if (sysclk_hz > 0.0)
            t = static_cast<uint64_t>(static_cast<double>(s.cycle_clock) * 1e9 / sysclk_hz);
        if (t < last)
            t = last;
        s.tick = t;
        last = t;
    }
    return out;
}

std::vector<SliceEvent> apply_hybrid_time(
    const std::vector<SliceEvent>& slices, double tsgen_hz, double sysclk_hz)
{
    if (tsgen_hz <= 0.0 || sysclk_hz <= 0.0)
        return apply_etm_timestamp(slices, tsgen_hz);

    const std::size_t n = slices.size();
    std::vector<SliceEvent> out = slices;
    if (n == 0)
        return out;

    auto ts_ns = [&](uint64_t ts) { return static_cast<double>(ts) * 1e9 / tsgen_hz; };
    const double ns_per_cycle = 1e9 / sysclk_hz;

    // Anchor groups: runs of events sharing one etm_ts value (0 = no TS seen yet).
    std::vector<std::size_t> starts;
    for (std::size_t i = 0; i < n; ++i)
        if (i == 0 || slices[i].etm_ts != slices[i - 1].etm_ts)
            starts.push_back(i);

    // First real anchor, to place events recorded before any TS packet.
    std::size_t first_real = starts.size();
    for (std::size_t g = 0; g < starts.size(); ++g)
        if (slices[starts[g]].etm_ts != 0) {
            first_real = g;
            break;
        }

    uint64_t last = 0;
    for (std::size_t g = 0; g < starts.size(); ++g) {
        const std::size_t a = starts[g];
        const std::size_t b = g + 1 < starts.size() ? starts[g + 1] : n;
        const bool have_ts = slices[a].etm_ts != 0;
        const double cc0 = static_cast<double>(slices[a].cycle_clock);

        double base;
        double ceiling = -1.0; // next anchor, if any
        if (have_ts) {
            base = ts_ns(slices[a].etm_ts);
        } else if (first_real < starts.size()) {
            // Before the first TS: count back from it by cycles.
            const std::size_t fa = starts[first_real];
            base = ts_ns(slices[fa].etm_ts)
                - (static_cast<double>(slices[fa].cycle_clock) - cc0) * ns_per_cycle;
            if (base < 0.0)
                base = 0.0;
        } else {
            base = 0.0; // no TS anywhere: pure cycle time from 0
        }
        if (g + 1 < starts.size() && slices[starts[g + 1]].etm_ts != 0)
            ceiling = ts_ns(slices[starts[g + 1]].etm_ts);

        for (std::size_t i = a; i < b; ++i) {
            double t = base + (static_cast<double>(slices[i].cycle_clock) - cc0) * ns_per_cycle;
            if (ceiling >= 0.0 && t > ceiling)
                t = ceiling;
            if (t < base)
                t = base;
            uint64_t tick = static_cast<uint64_t>(t);
            if (tick < last)
                tick = last;
            out[i].tick = tick;
            last = tick;
        }
    }
    return out;
}

} // namespace cortrace
