// Cortrace — minimal Perfetto trace writer.
//
// Encodes a stream of SliceEvents into a Perfetto `Trace` (a sequence of
// `TracePacket`s carrying TrackEvent slices) that opens directly in
// ui.perfetto.dev. Deliberately dependency-free (hand-rolled protobuf varint
// encoding) so it lives in the core library and is unit-testable; a product
// build may swap in the real protobuf library, but the wire format here is
// validated against Perfetto's synthetic-track-event schema.
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_PERFETTO_WRITER_HPP
#define CORTRACE_PERFETTO_WRITER_HPP

#include "cortrace/callstack.hpp"

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace cortrace {

// Serialise slice events onto a single Perfetto track. Returns the encoded
// trace bytes. `track_name` labels the track in the UI; `track_uuid` must be a
// nonzero, stable id.
std::string encode_perfetto_trace(const std::vector<SliceEvent>& slices,
    const std::string& track_name = "ETM callstack", uint64_t track_uuid = 0x1001);

// Convenience: encode and write to `path`. Returns false on write failure.
bool write_perfetto_trace(const std::string& path, const std::vector<SliceEvent>& slices,
    const std::string& track_name = "ETM callstack", uint64_t track_uuid = 0x1001);

// Multi-track variant: each SliceEvent.track is emitted on its own Perfetto
// track. `tracks` maps track id -> display name (e.g. 0="main thread",
// 1="IRQ:SysTick"). One TrackDescriptor is emitted per entry; each track's
// events go on a distinct track_uuid so ISRs appear as separate swim-lanes.
std::string encode_perfetto_trace_multi(
    const std::vector<SliceEvent>& slices, const std::map<int, std::string>& tracks);

bool write_perfetto_trace_multi(const std::string& path, const std::vector<SliceEvent>& slices,
    const std::map<int, std::string>& tracks);

} // namespace cortrace

#endif // CORTRACE_PERFETTO_WRITER_HPP
