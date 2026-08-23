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

TEST(callstack_exception_systick_on_own_track_and_balanced)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false)); // in main
    m.process(Element::exception(15)); // SysTick entry -> its own track
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false)); // ISR body
    m.process(Element::simple(ElementKind::ExceptionRet)); // ISR return
    m.finish();

    CHECK_EQ(m.metrics().exceptions, 1L);
    CHECK(m.metrics().balanced());

    // the SysTick slice must be emitted on a NON-main track (track != 0), and
    // its track must be registered with the ISR name.
    int systick_track = -1;
    for (auto& s : m.slices())
        if (s.begin && s.name == "IRQ:SysTick")
            systick_track = s.track;
    CHECK(systick_track > 0); // not the main thread track (0)
    auto it = m.tracks().find(systick_track);
    CHECK(it != m.tracks().end());
    CHECK_EQ(it->second, std::string("IRQ:SysTick"));

    // main-thread slices stay on track 0.
    for (auto& s : m.slices())
        if (s.name == "main")
            CHECK_EQ(s.track, 0);
}

TEST(callstack_isr_does_not_nest_on_preempted_function)
{
    // The ISR must NOT appear as a child of the function it preempted: main's
    // frame stays on track 0, the ISR lives entirely on its own track.
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false)); // main running
    m.process(Element::exception(15)); // preempt
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false)); // ISR body
    m.process(Element::simple(ElementKind::ExceptionRet));
    m.process(Element::instr_range(0x1008, 0x100c, BranchKind::None, false)); // main resumes
    m.finish();

    // Every IRQ slice is on a track != 0; every non-IRQ slice is on track 0.
    for (auto& s : m.slices()) {
        if (s.name.rfind("IRQ:", 0) == 0)
            CHECK(s.track != 0);
        else
            CHECK_EQ(s.track, 0);
    }
    CHECK(m.metrics().balanced());
}

TEST(callstack_distinct_exceptions_get_distinct_tracks)
{
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false));
    // SysTick (15)
    m.process(Element::exception(15));
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false));
    m.process(Element::simple(ElementKind::ExceptionRet));
    // PendSV (14) -- different exception number -> different track
    m.process(Element::exception(14));
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false));
    m.process(Element::simple(ElementKind::ExceptionRet));
    // SysTick again -- must REUSE the first SysTick track (stable per number)
    m.process(Element::exception(15));
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false));
    m.process(Element::simple(ElementKind::ExceptionRet));
    m.finish();

    int systick_track = -1, pendsv_track = -1, systick_track2 = -1;
    int systick_seen = 0;
    for (auto& s : m.slices()) {
        if (!s.begin)
            continue;
        if (s.name == "IRQ:SysTick") {
            if (systick_seen == 0)
                systick_track = s.track;
            else
                systick_track2 = s.track;
            systick_seen++;
        } else if (s.name == "IRQ:PendSV") {
            pendsv_track = s.track;
        }
    }
    CHECK(systick_track > 0);
    CHECK(pendsv_track > 0);
    CHECK(systick_track != pendsv_track); // distinct exceptions -> distinct tracks
    CHECK_EQ(systick_track, systick_track2); // same exception number -> same track
    CHECK(m.metrics().balanced());
}

TEST(callstack_nested_exception_preempts_isr_on_its_own_track)
{
    // A higher-priority exception preempts an ISR: it runs on ITS own track,
    // then control returns to the first ISR's track, then to main.
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false)); // main
    m.process(Element::exception(15)); // SysTick
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false));
    m.process(Element::exception(14)); // PendSV preempts SysTick (nested)
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false));
    m.process(Element::simple(ElementKind::ExceptionRet)); // back to SysTick
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false));
    m.process(Element::simple(ElementKind::ExceptionRet)); // back to main
    m.finish();

    CHECK_EQ(m.metrics().exceptions, 2L);
    CHECK(m.metrics().balanced());
    // three distinct tracks registered: main + SysTick + PendSV
    CHECK(m.tracks().size() >= 3);
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

TEST(callstack_finish_closes_open_isr_and_main_frames)
{
    // End of trace while an ISR (with a called function) and a main-thread call
    // are still open: finish() must close every frame on every track so the
    // slice stream is balanced.
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);

    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false)); // main
    m.process(Element::instr_range(0x1004, 0x1008, BranchKind::DirectCall, true));
    m.process(Element::instr_range(0x2000, 0x2010, BranchKind::None, false)); // in leaf
    m.process(Element::exception(15)); // SysTick preempts, leaves ISR open
    m.process(Element::instr_range(0x3000, 0x3008, BranchKind::None, false)); // ISR body
    // no ExceptionRet, no returns -> everything open at EOT
    m.finish();

    CHECK(m.metrics().balanced()); // finish() closed all open frames
    // per-track balance: count begins/ends per track
    std::map<int, long> bal;
    for (auto& s : m.slices())
        bal[s.track] += s.begin ? 1 : -1;
    for (auto& kv : bal)
        CHECK_EQ(kv.second, 0L); // every track nets to zero
}

TEST(callstack_indirect_call_is_a_call)
{
    // IndirectCall (BLX reg) confirmed at a function entry is a real call edge.
    SymbolTable syms = make_syms();
    CallStackMachine m(syms);
    m.process(Element::instr_range(0x1000, 0x1008, BranchKind::None, false));
    m.process(Element::instr_range(0x1004, 0x1008, BranchKind::IndirectCall, true));
    m.process(Element::instr_range(0x2000, 0x2010, BranchKind::None, false)); // leaf entry
    m.finish();
    bool found = false;
    for (auto& kv : m.edges())
        if (kv.first == "main -> leaf")
            found = true;
    CHECK(found);
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
