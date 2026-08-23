// Cortrace — call-stack machine.
//
// Consumes normalised trace Elements in order and reconstructs the function
// call stack, emitting begin/end slice events. This is the deterministic core
// of cortrace and is fully unit-tested with synthetic element streams.
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_CALLSTACK_HPP
#define CORTRACE_CALLSTACK_HPP

#include "cortrace/element.hpp"
#include "cortrace/symbols.hpp"

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace cortrace {

// One emitted slice boundary (begin or end of a function on the timeline).
struct SliceEvent {
    uint64_t tick; // monotonic order (real ns applied by the sink via byte_index)
    uint64_t byte_index; // source ETM byte offset, for time-base lookup
    bool begin; // true = slice begin, false = slice end
    std::string name; // function / ISR name
    int track = 0; // track id: 0 = main thread, >=1 = a per-exception ISR track
};

// Aggregate quality/΄shape metrics produced during reconstruction.
struct StackMetrics {
    long begins = 0;
    long ends = 0;
    int max_depth = 0;
    long mismatched_returns = 0; // return with an (almost) empty stack
    long dropped_calls = 0; // callee lost to a blind spot (not fabricated)
    long recovered_missed_returns = 0; // stale frame popped before re-entry
    long exceptions = 0; // ISR frames rendered
    bool balanced() const { return begins == ends; }
};

class CallStackMachine {
public:
    explicit CallStackMachine(const SymbolTable& syms);

    // Feed one element. Safe to call in any order; ignores kinds it does not
    // model.
    void process(const Element& e);

    // Call once at end-of-trace: closes any still-open frames so the emitted
    // slice stream is balanced.
    void finish();

    const std::vector<SliceEvent>& slices() const { return slices_; }
    const StackMetrics& metrics() const { return metrics_; }

    // caller->callee edge -> count. Used to cross-check against the ELF `bl`
    // graph (golden criterion: every edge is backed by a real bl).
    const std::map<std::string, long>& edges() const { return edges_; }

    // track id -> track name (0 = "main thread", one track per distinct ISR
    // keyed by exception number, e.g. "IRQ:SysTick"). The sink emits one
    // Perfetto TrackDescriptor per entry.
    const std::map<int, std::string>& tracks() const { return track_names_; }

private:
    struct Frame {
        std::string fn;
        uint32_t ret_addr; // expected return address (end of the call range)
    };

    // An execution context = one call stack living on one Perfetto track. The
    // main thread is context 0 (track 0). Each exception preempts the current
    // context and runs on its OWN track (one track per exception number), so
    // ISRs appear as separate swim-lanes instead of nesting on the preempted
    // function. Exceptions can nest (higher-priority IRQ preempts an ISR), so
    // contexts form a stack.
    struct Context {
        int track; // Perfetto track id for this context
        std::vector<Frame> stack; // call frames on this track
    };

    void do_call(const std::string& callee, uint32_t ret);
    void do_return();
    void relabel_top(const std::string& fn);
    Context& cur() { return ctx_.back(); } // active (innermost) context
    void emit(bool begin, const std::string& name); // to the active track
    int track_for_exception(uint32_t number); // stable track id per exc number

    const SymbolTable& syms_;
    std::vector<Context> ctx_; // context stack (0 = main thread)
    std::vector<SliceEvent> slices_;
    std::map<std::string, long> begins_by_fn_;
    std::map<std::string, long> ends_by_fn_;
    std::map<std::string, long> edges_;
    std::map<int, std::string> track_names_; // track id -> name
    std::map<uint32_t, int> exc_track_; // exception number -> track id
    int next_track_ = 1; // next ISR track id to allocate
    StackMetrics metrics_;

    uint64_t tick_ = 0;
    uint64_t last_byte_index_ = 0;
    bool pending_call_ = false;
    uint32_t pending_ret_ = 0;
    bool after_blind_ = false;
};

// Map an ARM Cortex-M exception number to a readable ISR name.
std::string exception_name(uint32_t number);

} // namespace cortrace

#endif // CORTRACE_CALLSTACK_HPP
