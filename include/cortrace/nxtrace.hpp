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

#include "cortrace/callstack.hpp"
#include "cortrace/symbols.hpp"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <string>
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

// Resolve a captured value (the written "current task" pointer) to a thread
// identity. On NuttX this reads the TCB (pid, entry->symbol) from the ELF image
// (doc §4); the default implementation just formats the pointer, so the
// scheduling view works even without target memory.
struct ThreadId {
    int tid = 0; // stable small id (Perfetto track key)
    std::string name; // display name
};

// A thread-run interval: the running thread from one switch to the next.
struct ThreadRun {
    std::size_t begin_src = 0; // src_index of the switch that started this run
    std::size_t end_src = 0; // src_index of the next switch (or last event)
    uint32_t tcb = 0; // captured "current task" pointer
    ThreadId id;
};

// Maps a captured current-task pointer to a thread identity.
using ThreadResolver = std::function<ThreadId(uint32_t value)>;

// Turn a sequence of DWT switch events (each carrying the new current-task
// pointer) into thread-run intervals. `resolve` maps a captured pointer to a
// ThreadId; if empty, a default pointer-formatting resolver is used. Only events
// on comparator `watch_comp` are treated as switches (default 0). The last run
// is left open (end_src == begin_src) unless `stream_end_src` is given.
std::vector<ThreadRun> build_thread_runs(const std::vector<DwtEvent>& events,
    const ThreadResolver& resolve = {}, uint8_t watch_comp = 0, std::size_t stream_end_src = 0);

// NuttX thread-identity resolver (doc §4): given a captured TCB pointer, read
// pid (offset 0x30) and entry (offset 0x3C) from the ELF program image (static
// TCBs live in an ELF PT_LOAD segment), and resolve the entry pointer to a
// function name via the symbol table. Purely static -- no live-memory read-back,
// no UAF. `read_u32(addr, out)` supplies a little-endian 32-bit word from the
// ELF image at a target address, returning false if the address is not backed
// by the image (e.g. a dynamically-malloc'd TCB) -- in which case the pointer is
// formatted as-is. TCB field offsets are overridable (DWARF-derived per build).
class NuttxResolver {
public:
    using ReadU32 = std::function<bool(uint32_t addr, uint32_t& out)>;

    NuttxResolver(ReadU32 read_u32, const SymbolTable& syms)
        : read_u32_(std::move(read_u32))
        , syms_(&syms)
    {
    }

    // TCB field offsets (bytes). Defaults are the on-board ELF values (doc §4).
    void set_offsets(uint32_t pid_off, uint32_t entry_off)
    {
        pid_off_ = pid_off;
        entry_off_ = entry_off;
    }

    // Optionally supply a tcb -> (pid, name) map dumped from the live target
    // (doc §4.3 path 2, nx_tcbmap.py), used for heap-allocated TCBs whose
    // pid/entry are not in the ELF image. Consulted BEFORE the ELF read.
    void set_tcb_map(std::map<uint32_t, ThreadId> m) { tcb_map_ = std::move(m); }

    ThreadId operator()(uint32_t tcb) const;

private:
    ReadU32 read_u32_;
    const SymbolTable* syms_;
    uint32_t pid_off_ = 0x30;
    uint32_t entry_off_ = 0x3C;
    std::map<uint32_t, ThreadId> tcb_map_;
};

// Load a tcb->name map file (lines: "0xTCB<TAB>pid<TAB>name", '#' comments) as
// produced by nx_tcbmap.py. Returns tcb -> ThreadId{tid=pid, name}.
std::map<uint32_t, ThreadId> load_tcb_map(const std::string& path);

// Emit thread-run intervals as Perfetto slice events on a single "Threads"
// track (track id `track`), one slice per run named by the resolved thread.
// byte_index is set to the run's begin_src so the shared FPGA time base applies.
// Also fills `track_names` for the multi-track writer.
std::vector<SliceEvent> thread_runs_to_slices(
    const std::vector<ThreadRun>& runs, int track, std::map<int, std::string>& track_names);

// Re-attribute ETM call-stack slices to per-thread tracks: each input slice is
// moved to the track of whichever thread run (by byte_index) was executing when
// it occurred, so every RTOS thread gets its OWN swim-lane showing its own call
// stack (instead of all call stacks piling onto one "main thread" track).
// `runs` must be sorted by begin_src (build_thread_runs output is). `base_track`
// is the first per-thread track id; distinct threads (by ThreadId.tid) get
// consecutive ids from there. ISR slices (track != 0 in the input, i.e. the
// call-stack machine's exception tracks) are left on their own tracks. Fills
// `track_names` with "<thread name>" per allocated track. Slices whose
// byte_index falls in no run (before the first switch) stay on their input
// track. Returns the re-tracked slices (input order preserved).
std::vector<SliceEvent> reattribute_slices_to_threads(const std::vector<SliceEvent>& etm_slices,
    const std::vector<ThreadRun>& runs, int base_track, std::map<int, std::string>& track_names);

} // namespace cortrace

#endif // CORTRACE_NXTRACE_HPP
