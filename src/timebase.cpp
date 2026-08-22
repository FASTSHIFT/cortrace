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

} // namespace cortrace
