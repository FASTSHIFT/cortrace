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

} // namespace cortrace
