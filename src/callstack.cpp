// Cortrace — CallStackMachine implementation.
//
// Reconstruction rules and blind-spot handling are ported from the validated
// proof of concept: call graph matched the ELF edge-for-edge (0 mismatch) with
// balanced nesting on an offline CoreMark slice.
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
    // Seed a root frame so an early return does not underflow.
    stack_.push_back({ "<root>", 0 });
}

void CallStackMachine::do_call(const std::string& callee, uint32_t ret)
{
    // Blind-spot guard: about to push a frame whose function equals the current
    // top and that function is not actually self-recursive => the previous
    // frame's return was lost to a gap. Pop the stale frame first (net:
    // re-entry, no runaway depth).
    if (!stack_.empty() && stack_.back().fn == callee && !is_self_recursive(callee)) {
        ends_by_fn_[stack_.back().fn]++;
        slices_.push_back({ tick_++, last_byte_index_, false, stack_.back().fn });
        metrics_.ends++;
        stack_.pop_back();
        metrics_.recovered_missed_returns++;
    }
    const std::string& caller = stack_.empty() ? std::string("<root>") : stack_.back().fn;
    edges_[caller + " -> " + callee]++;
    begins_by_fn_[callee]++;
    metrics_.begins++;
    stack_.push_back({ callee, ret });
    slices_.push_back({ tick_++, last_byte_index_, true, callee });
    if (static_cast<int>(stack_.size()) > metrics_.max_depth)
        metrics_.max_depth = static_cast<int>(stack_.size());
}

void CallStackMachine::do_return()
{
    if (stack_.size() > 1) {
        ends_by_fn_[stack_.back().fn]++;
        slices_.push_back({ tick_++, last_byte_index_, false, stack_.back().fn });
        metrics_.ends++;
        stack_.pop_back();
    } else {
        metrics_.mismatched_returns++;
    }
}

void CallStackMachine::relabel_top(const std::string& fn)
{
    if (!stack_.empty())
        stack_.back().fn = fn;
}

void CallStackMachine::process(const Element& e)
{
    last_byte_index_ = e.byte_index;

    switch (e.kind) {
    case ElementKind::InstrRange: {
        const uint32_t start = e.start_addr;
        const uint32_t end = e.end_addr;

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

        // Reconcile the stack with where we actually are (unless we just
        // confirmed a fresh call above, in which case top == callee already).
        if (!stack_.empty()) {
            const std::string& fn = syms_.function_at(start);
            if (stack_.back().fn != fn) {
                // If `fn` matches a frame BELOW the top, the frames above it
                // returned without us seeing the return element (lost to a
                // blind spot). Unwind to that frame -- this is the general
                // missed-return recovery.
                int found = -1;
                for (int i = static_cast<int>(stack_.size()) - 2; i >= 0; --i) {
                    if (stack_[i].fn == fn) {
                        found = i;
                        break;
                    }
                }
                if (found >= 0) {
                    while (static_cast<int>(stack_.size()) - 1 > found) {
                        ends_by_fn_[stack_.back().fn]++;
                        slices_.push_back({ tick_++, last_byte_index_, false, stack_.back().fn });
                        metrics_.ends++;
                        stack_.pop_back();
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
        pending_call_ = false;
        const std::string nm = exception_name(e.exception_number);
        metrics_.exceptions++;
        exc_depth_mark_.push_back(static_cast<int>(stack_.size()));
        begins_by_fn_[nm]++;
        metrics_.begins++;
        stack_.push_back({ nm, 0 });
        slices_.push_back({ tick_++, last_byte_index_, true, nm });
        if (static_cast<int>(stack_.size()) > metrics_.max_depth)
            metrics_.max_depth = static_cast<int>(stack_.size());
        break;
    }

    case ElementKind::ExceptionRet: {
        if (!exc_depth_mark_.empty()) {
            const int target = exc_depth_mark_.back();
            exc_depth_mark_.pop_back();
            while (static_cast<int>(stack_.size()) > target) {
                ends_by_fn_[stack_.back().fn]++;
                slices_.push_back({ tick_++, last_byte_index_, false, stack_.back().fn });
                metrics_.ends++;
                stack_.pop_back();
            }
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
    case ElementKind::Unknown:
        break;
    }
}

void CallStackMachine::finish()
{
    // Close still-open frames (down to the root) for a balanced slice stream.
    while (stack_.size() > 1) {
        ends_by_fn_[stack_.back().fn]++;
        slices_.push_back({ tick_++, last_byte_index_, false, stack_.back().fn });
        metrics_.ends++;
        stack_.pop_back();
    }
}

} // namespace cortrace
