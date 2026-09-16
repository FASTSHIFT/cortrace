// Cortrace — FPGA raw-capture front end: nibble reassemble + TPIU deframe.
//
// The A7-Lite capture appliance streams the STM32 parallel-trace port as a raw
// byte sequence (two nibbles per TRACECLK period, {trace_a hi, trace_b lo}).
// Turning that into the bare ETMv4 byte stream OpenCSD wants is three byte-wise
// passes that used to run in slow host Python (deframe_to_etm.py):
//   1. nibble extract  : raw[k] -> two 4-bit nibbles
//   2. assemble        : pair nibbles into bytes under a (parity, order) phase
//   3. TPIU deframe     : 16-byte CoreSight frames -> the requested ATB stream
// This is the C++ port so cortrace-decode can consume a raw capture directly
// (orders of magnitude faster than the Python path).
//
// The (parity, order) phase is fixed for a given board + bitstream, but a fresh
// capture's phase is recovered by trying all four and scoring each by the
// number of post-deframe ETMv4 A-syncs (the only signal that proves the whole
// chain — nibble phase, TPIU frame phase, stream demux — is aligned).
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_DEFRAME_HPP
#define CORTRACE_DEFRAME_HPP

#include <cstddef>
#include <cstdint>
#include <map>
#include <vector>

namespace cortrace {

// Nibble-pairing phase. parity drops the leading nibble (0 or 1); order chooses
// whether the first nibble of a pair is the low or high half of the byte.
//
// For non-4-bit parallel ports (width 2 or 1) the capture format is the same
// (one byte per TRACECLK, {trace_b hi-nibble, trace_a lo-nibble}) but a TPIU
// byte spans 8/(2*width) TRACECLK periods, so reassembly differs. When
// width != 4, `parity` is reinterpreted as the byte-boundary phase
// (0..8/width-1) and `order` as the within-half-symbol bit order (0=lsb,1=msb).
// width == 4 keeps the original parity/order nibble-pairing exactly.
struct DeframePhase {
    int parity = 1; // matches the A7-Lite board default (parity=1, order=0)
    int order = 0;
    int width = 4; // parallel TRACED port width: 4, 2 or 1
};

struct DeframeResult {
    std::vector<uint8_t> etm; // deframed ETMv4 bytes for the requested stream
    DeframePhase phase; // the phase actually used
    int async_count = 0; // ETMv4 A-syncs found post-deframe (alignment score)
    std::size_t frames = 0; // TPIU 16-byte frames decoded
    std::size_t syncs = 0; // full TPIU sync patterns seen
};

// Multi-stream deframe: demux ALL TPIU ATB streams in a single pass so ETM
// (stream 2) and DWT/ITM (stream 1) share one timebase. Each output byte keeps
// the source index (into the ASSEMBLED byte stream) it came from, so callers
// can map any stream's byte back to the same FPGA time base array.
struct MultiDeframeResult {
    // stream_id -> deframed bytes for that ATB stream (stream 0 = padding,
    // dropped). Typical ids here: 2 = ETM, 1 = ITM/DWT.
    std::map<int, std::vector<uint8_t>> streams;
    // stream_id -> per-byte source index into the assembled stream (parallel to
    // streams[id]); used to index a shared timebase.
    std::map<int, std::vector<std::size_t>> src_index;
    DeframePhase phase;
    int async_count = 0; // ETMv4 A-syncs in stream 2 (alignment score)
    std::size_t frames = 0;
    std::size_t syncs = 0;
};

// Deframe an assembled byte stream, demuxing every ATB stream at once.
MultiDeframeResult tpiu_deframe_multi(const std::vector<uint8_t>& data, const DeframePhase& phase);

// Full front end (multi-stream): nibble-assemble + demux all streams. When
// `search` is true, try all four phases and keep the one whose stream 2 has the
// most ETMv4 A-syncs (the ETM stream is the alignment anchor for the whole
// capture; the other streams ride the same frames/phase).
MultiDeframeResult deframe_raw_capture_multi(
    const uint8_t* raw, std::size_t len, bool search, const DeframePhase& phase);

// Assemble bytes from a raw capture under a fixed phase (no search).
std::vector<uint8_t> assemble_nibbles(
    const uint8_t* raw, std::size_t len, const DeframePhase& phase);

// TPIU-deframe an assembled byte stream, extracting `want_stream` (ETM = 2).
// Faithful port of orbuculum's tpiuDecoder.c (see tpiu_official.py).
DeframeResult tpiu_deframe(
    const std::vector<uint8_t>& data, int want_stream, const DeframePhase& phase);

// Full front end: nibble-assemble + TPIU-deframe a raw FPGA capture. If
// `search` is true, try all four phases and keep the one with the most
// post-deframe A-syncs; otherwise use `phase` as given.
DeframeResult deframe_raw_capture(
    const uint8_t* raw, std::size_t len, int want_stream, bool search, const DeframePhase& phase);

// Count ETMv4 A-syncs (>= 11 zero bytes followed by 0x80) in a byte stream.
int count_etmv4_async(const uint8_t* data, std::size_t len);

} // namespace cortrace

#endif // CORTRACE_DEFRAME_HPP
