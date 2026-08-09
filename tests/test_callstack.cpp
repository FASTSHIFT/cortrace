// Cortrace — CallStackMachine unit tests.
//
// Feed synthetic Element streams (no live decoder) and assert the core
// invariants: balanced nesting, correct edges, blind-spot handling, SysTick.
//
// SPDX-License-Identifier: MIT
#include "cortrace/callstack.hpp"
#include "cortrace/element.hpp"
#include "cortrace/symbols.hpp"
#include "test_framework.hpp"

using namespace cortrace;

// A small program:
//   main (0x1000) calls leaf (0x2000); leaf returns; main ends.
static SymbolTable make_syms()
{
    SymbolTable t;
    t.add(0x1000, "main");
    t.add(0x2000, "leaf");
    t.add(0x3000, "other");
    t.finalize();
    return t;
}

TEST(callstack_simple_call_return_balanced)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    // enter main
    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false));
    // main: BL leaf  (call range ends at 0x1008 = return addr)
    m.process(Element::instr_range(0x1004, 0x1008, BranchKind::DirectCall, true));
    // leaf entry range (confirms the call: starts exactly at leaf entry)
    m.process(Element::instr_range(0x2000, 0x2010, BranchKind::None, false));
    // leaf returns
    m.process(Element::instr_range(0x2010, 0x2014, BranchKind::Return, true));
    m.finish();

    const auto& mt = m.metrics();
    CHECK(mt.balanced());
    CHECK_EQ(mt.begins, mt.ends);
    CHECK(mt.max_depth >= 2); // root(main) + leaf
    CHECK_EQ(mt.mismatched_returns, 0L);

    // exactly one main->leaf edge
    bool found = false;
    for (auto& kv : m.edges())
        if (kv.first == "main -> leaf") {
            found = kv.second == 1;
        }
    CHECK(found);
}

TEST(callstack_call_into_nonentry_is_dropped)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false));
    // a call whose next range does NOT start at a function entry (blind spot
    // swallowed the callee body): must be dropped, not fabricated.
    m.process(Element::instr_range(0x1004, 0x1008, BranchKind::DirectCall, true));
    m.process(Element::instr_range(0x2004, 0x2010, BranchKind::None, false)); // mid-leaf, not entry
    m.finish();

    CHECK_EQ(m.metrics().dropped_calls, 1L);
    // no fabricated main->leaf edge
    for (auto& kv : m.edges())
        CHECK(kv.first != "main -> leaf");
}

TEST(callstack_blind_spot_no_fabrication)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::DirectCall, true));
    // discontinuity right after the call: callee lost
    m.process(Element::simple(ElementKind::AddrNacc));
    // resync landing happens to be at a function entry, but after_blind must
    // suppress treating it as the callee of the pre-gap call.
    m.process(Element::instr_range(0x2000, 0x2008, BranchKind::None, false));
    m.finish();

    CHECK_EQ(m.metrics().dropped_calls, 1L);
    CHECK(m.metrics().balanced());
}

TEST(callstack_return_underflow_counts_mismatch)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    // return with only the root frame present
    m.process(Element::instr_range(0x2010, 0x2014, BranchKind::Return, true));
    m.finish();
    CHECK_EQ(m.metrics().mismatched_returns, 1L);
}

TEST(callstack_exception_systick_nested_and_balanced)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false)); // in main
    m.process(Element::exception(15)); // SysTick entry
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false)); // ISR body
    m.process(Element::simple(ElementKind::ExceptionRet)); // ISR return
    m.finish();

    CHECK_EQ(m.metrics().exceptions, 1L);
    CHECK(m.metrics().balanced());
    // a SysTick slice must have been emitted
    bool systick = false;
    for (auto& s : m.slices())
        if (s.begin && s.name == "IRQ:SysTick")
            systick = true;
    CHECK(systick);
}

TEST(callstack_missed_return_recovered_on_reentry)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    // enter main, call leaf
    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false));
    m.process(Element::instr_range(0x1004, 0x1008, BranchKind::DirectCall, true));
    m.process(Element::instr_range(0x2000, 0x2010, BranchKind::None, false)); // leaf entered
    // leaf's return is LOST (no return element). Now main calls leaf again:
    m.process(Element::instr_range(0x1004, 0x1008, BranchKind::DirectCall, true));
    m.process(Element::instr_range(0x2000, 0x2010, BranchKind::None, false)); // leaf entered again
    m.finish();

    // the stale leaf frame must have been popped before re-entry (no runaway)
    CHECK(m.metrics().recovered_missed_returns >= 1L);
    CHECK(m.metrics().balanced());
}

TEST(exception_name_mapping)
{
    CHECK_EQ(exception_name(15), std::string("IRQ:SysTick"));
    CHECK_EQ(exception_name(14), std::string("IRQ:PendSV"));
    CHECK_EQ(exception_name(3), std::string("IRQ:HardFault"));
    CHECK_EQ(exception_name(16), std::string("IRQ:ext0"));
    CHECK_EQ(exception_name(20), std::string("IRQ:ext4"));
}
