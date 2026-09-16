// Cortrace — nxtrace: DWT data-value packet parsing for RTOS thread-switch
// tracking (doc 01 §5.2).
//
// On the H743 parallel path the DWT emits a "data trace data-value" packet on
// each WRITE to a watched address (a comparator programmed FUNCTION=0x0D). When
// the watched address is the RTOS "current task" pointer (NuttX g_running_tasks),
// the packet payload IS the new TCB pointer, captured at the instant of the
// write — no target-memory read-back, so even short-lived threads are seen.
//
// This module parses the demuxed DWT/ITM stream (TPIU ATB id 1) into a sequence
// of (source_index, comparator, value) events. Timing is applied separately by
// mapping each event's source_index through the shared FPGA time base (the same
// array ETM uses), so DWT events and ETM instructions land on one timeline.
//
// Packet format (ARMv7-M ARM, DDI0403E, Table C1-14 / D4-7; confirmed on-board
// 2026-09-16, doc §11.8):
//   header byte:  bits[7:6]=10 (data-value), bit3=WnR (1=write), bit2=1
//                 (hardware/DWT source), bits[5:4]=comparator id (0..3),
//                 bits[1:0]=SS payload size (01=1B, 10=2B, 11=4B)
//   then SS little-endian payload bytes = the written value.
// Local timestamp packets (LTS1/LTS2) and other ITM protocol packets are
// skipped by the parser (time comes from the shared FPGA base, not ITM LTS).
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_NXTRACE_HPP
#define CORTRACE_NXTRACE_HPP

#include <cstddef>
#include <cstdint>
#include <vector>

namespace cortrace {

// One recovered DWT data-value write event.
struct DwtEvent {
    std::size_t src_index = 0; // source index into the assembled stream (for timebase)
    uint8_t comparator = 0; // DWT comparator id (0..3)
    uint8_t size = 0; // payload byte count (1/2/4)
    uint32_t value = 0; // the written value (little-endian payload)
};

// Parse the demuxed DWT/ITM byte stream into data-value WRITE events. `src_index`
// is the parallel per-byte source-index array from MultiDeframeResult (same
// length as `bytes`); the event's src_index is taken from the header byte's
// source position so it can be mapped through the shared FPGA time base. If
// `src_index` is empty, events carry a byte offset into `bytes` instead.
std::vector<DwtEvent> parse_dwt_data_values(
    const std::vector<uint8_t>& bytes, const std::vector<std::size_t>& src_index);

} // namespace cortrace

#endif // CORTRACE_NXTRACE_HPP
