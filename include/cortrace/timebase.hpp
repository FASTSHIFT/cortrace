// Cortrace — FPGA time base: ETM byte offset -> wall-clock ns.
//
// The capture side timestamps each RAW ETM byte (a `time.bin` of uint64 ns,
// one per byte). OpenCSD reports the source byte offset (idx_sop) of every
// element, so cortrace maps element -> ns by table lookup. This is
// frequency-independent: it does not rely on ETM cycle-count packets (which are
// suppressed under STALL).
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_TIMEBASE_HPP
#define CORTRACE_TIMEBASE_HPP

#include "cortrace/callstack.hpp"

#include <cstdint>
#include <string>
#include <vector>

namespace cortrace {

class TimeBase {
public:
    // Load a `time.bin` of little-endian uint64 ns values, one per ETM byte.
    // Throws std::runtime_error if the file cannot be opened.
    std::size_t load(const std::string& path);

    bool empty() const { return ns_.empty(); }
    std::size_t size() const { return ns_.size(); }

    // ns at ETM byte offset `idx`; clamps to the last entry past the end.
    uint64_t ns_for(uint64_t idx) const
    {
        if (ns_.empty())
            return 0;
        return idx < ns_.size() ? ns_[idx] : ns_.back();
    }

private:
    std::vector<uint64_t> ns_;
};

// Return a copy of `slices` with each event's `tick` replaced by the wall-clock
// ns for its byte_index. If `tb` is empty, returns `slices` unchanged (falls
// back to monotonic tick ordering).
std::vector<SliceEvent> apply_timebase(const std::vector<SliceEvent>& slices, const TimeBase& tb);

// Return a copy of `slices` with each event's `tick` set from the ETM global
// timestamp (SliceEvent.etm_ts, the SoC TSGEN count sampled at execution time).
// If `tsgen_hz > 0` the count is converted to ns (count * 1e9 / hz); otherwise
// the raw count is used as the tick. Timestamps are clamped non-decreasing so
// the sequence is valid for Perfetto. This is the execution-time base, distinct
// from apply_timebase's FPGA ETF-egress base.
std::vector<SliceEvent> apply_etm_timestamp(
    const std::vector<SliceEvent>& slices, double tsgen_hz = 0.0);

} // namespace cortrace

#endif // CORTRACE_TIMEBASE_HPP
