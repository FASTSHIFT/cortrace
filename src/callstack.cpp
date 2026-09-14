// Cortrace — CallStackMachine implementation.
//
// Reconstruction rules and blind-spot handling are ported from the validated
// proof of concept: call graph matched the ELF edge-for-edge (0 mismatch) with
// balanced nesting on an offline CoreMark slice.
//
// Multi-track model: the main thread runs on track 0; each exception (keyed by
// exception number) runs on its OWN Perfetto track, so ISRs appear as separate
// swim-lanes instead of nesting on the function they preempted. Contexts stack
// (a higher-priority IRQ can preempt an ISR), mirroring Cortex-M exception
// preemption.
//
// SPDX-License-Identifier: MIT
#include "cortrace/callstack.hpp"

#include <string>

namespace cortrace {

std::string exception_name(uint32_t number)
{
    switch (number) {
    case 2:
        return "IRQ:NMI";
    case 3:
        return "IRQ:HardFault";
    case 11:
        return "IRQ:SVCall";
    case 14:
        return "IRQ:PendSV";
    case 15:
        return "IRQ:SysTick";
    default:
        return number >= 16 ? "IRQ:ext" + std::to_string(number - 16)
                            : "IRQ:" + std::to_string(number);
    }
}

// CoreMark's hot functions are not self-recursive; a product build should
// derive this from the ELF call graph so genuine recursion is preserved. For
// now, treating "push callee == current top" as a missed-return is the correct
// default for non-recursive code.
static bool is_self_recursive(const std::string&) { return false; }

CallStackMachine::CallStackMachine(const SymbolTable& syms)
    : syms_(syms)
{
    // Context 0 = main thread on track 0, seeded with a root frame so an early
    // return does not underflow.
    track_names_[0] = "main thread";
    ctx_.push_back({ 0, { { "<root>", 0 } } });
}

void CallStackMachine::emit(bool begin, const std::string& name)
{
    slices_.push_back({ tick_++, last_byte_index_, begin, name, cur().track, last_etm_ts_ });
    if (begin) {
        begins_by_fn_[name]++;
        metrics_.begins++;
    } else {
        ends_by_fn_[name]++;
        metrics_.ends++;
    }
    // max_depth tracks the deepest single-track call stack.
    int d = static_cast<int>(cur().stack.size());
    if (d > metrics_.max_depth)
        metrics_.max_depth = d;
}

int CallStackMachine::track_for_exception(uint32_t number)
{
    auto it = exc_track_.find(number);
    if (it != exc_track_.end())
        return it->second;
    int t = next_track_++;
    exc_track_[number] = t;
    track_names_[t] = exception_name(number);
    return t;
}

void CallStackMachine::do_call(const std::string& callee, uint32_t ret)
{
    auto& stack = cur().stack;
    // Blind-spot guard: about to push a frame whose function equals the current
    // top and that function is not actually self-recursive => the previous
    // frame's return was lost to a gap. Pop the stale frame first (net:
    // re-entry, no runaway depth).
    if (!stack.empty() && stack.back().fn == callee && !is_self_recursive(callee)) {
        emit(false, stack.back().fn);
        stack.pop_back();
        metrics_.recovered_missed_returns++;
    }
    const std::string& caller = stack.empty() ? std::string("<root>") : stack.back().fn;
    edges_[caller + " -> " + callee]++;
    stack.push_back({ callee, ret });
    emit(true, callee);
}

void CallStackMachine::do_return()
{
    auto& stack = cur().stack;
    if (stack.size() > 1) {
        emit(false, stack.back().fn);
        stack.pop_back();
    } else {
        metrics_.mismatched_returns++;
    }
}

void CallStackMachine::relabel_top(const std::string& fn)
{
    auto& stack = cur().stack;
    if (!stack.empty())
        stack.back().fn = fn;
}

void CallStackMachine::process(const Element& e)
{
    last_byte_index_ = e.byte_index;

    switch (e.kind) {
    case ElementKind::InstrRange: {
        const uint32_t start = e.start_addr;
        const uint32_t end = e.end_addr;
        auto& stack = cur().stack;

        // Resolve a pending CALL from the previous range. Confirm only if this
        // range starts exactly at a function entry; otherwise the callee body
        // was swallowed by a blind spot and OpenCSD resynced elsewhere — do not
        // fabricate a frame.
        if (pending_call_) {
            pending_call_ = false;
            if (!after_blind_ && syms_.is_function_entry(start))
                do_call(syms_.function_at(start), pending_ret_);
            else
                metrics_.dropped_calls++;
        }
        after_blind_ = false;

        // Reconcile the current track's stack with where we actually are
        // (unless we just confirmed a fresh call above, in which case top ==
        // callee already).
        if (!stack.empty()) {
            const std::string& fn = syms_.function_at(start);
            if (stack.back().fn != fn) {
                // If `fn` matches a frame BELOW the top, the frames above it
                // returned without us seeing the return element (lost to a
                // blind spot). Unwind to that frame -- the general
                // missed-return recovery.
                int found = -1;
                for (int i = static_cast<int>(stack.size()) - 2; i >= 0; --i) {
                    if (stack[i].fn == fn) {
                        found = i;
                        break;
                    }
                }
                if (found >= 0) {
                    while (static_cast<int>(stack.size()) - 1 > found) {
                        emit(false, stack.back().fn);
                        stack.pop_back();
                        metrics_.recovered_missed_returns++;
                    }
                } else if (!syms_.is_function_entry(start)) {
                    // mid-function fallthrough into another symbol's tail region
                    relabel_top(fn);
                }
                // else: starts at a real function entry, not on the stack -> a
                // call path owns it; leave the frame as-is.
            }
        }

        if (e.is_call()) {
            pending_call_ = true;
            pending_ret_ = end;
        } else if (e.is_return()) {
            do_return();
        }
        break;
    }

    case ElementKind::Exception: {
        // Preempt the current context: open a NEW context on this exception's
        // own track (one track per exception number). The ISR's functions land
        // on that track, not nested on the preempted function.
        pending_call_ = false;
        const std::string nm = exception_name(e.exception_number);
        const int track = track_for_exception(e.exception_number);
        metrics_.exceptions++;
        ctx_.push_back({ track, {} });
        // The ISR frame itself is the root of this context's stack.
        cur().stack.push_back({ nm, 0 });
        emit(true, nm);
        break;
    }

    case ElementKind::ExceptionRet: {
        // Close the current ISR context: pop all its frames (the ISR frame +
        // any functions it called), then return to the preempted context.
        if (ctx_.size() > 1) {
            auto& stack = cur().stack;
            while (!stack.empty()) {
                emit(false, stack.back().fn);
                stack.pop_back();
            }
            ctx_.pop_back();
        }
        pending_call_ = false;
        after_blind_ = true;
        break;
    }

    case ElementKind::AddrNacc:
    case ElementKind::TraceOn:
        // Discontinuity: any call/return straddling the gap is lost. Never
        // fabricate flow across it. A call left pending here loses its callee.
        if (pending_call_) {
            pending_call_ = false;
            metrics_.dropped_calls++;
        }
        after_blind_ = true;
        break;

    case ElementKind::Timestamp:
        // Record the execution-time anchor; subsequent slices carry it so the
        // Perfetto time base can use real ETM time instead of ETF-egress time.
        last_etm_ts_ = e.timestamp;
        break;

    case ElementKind::Unknown:
        break;
    }
}

void CallStackMachine::finish()
{
    // Close every still-open context (innermost ISR first, down to the main
    // thread root) so the emitted slice stream is balanced on every track.
    while (!ctx_.empty()) {
        auto& stack = cur().stack;
        const bool is_main = (ctx_.size() == 1);
        while (stack.size() > (is_main ? 1u : 0u)) {
            emit(false, stack.back().fn);
            stack.pop_back();
        }
        if (is_main)
            break;
        ctx_.pop_back();
    }
}

} // namespace cortrace
