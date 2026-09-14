// cortrace-decode — offline CLI front end.
//
// Runs the full pipeline on a captured, already-deframed ETMv4 byte stream:
//   raw ETM bytes  ->  OpenCSD decode  ->  call-stack machine  ->  Perfetto
//
// Reports the golden quality criteria (nesting balance, per-function slice
// counts, call edges for ELF cross-check), a function-coverage report (which
// functions the instruction flow visited vs which rendered as slices, so
// "missed" functions are surfaced), and the blind-region rate. Writes a
// Perfetto trace that opens directly in ui.perfetto.dev.
//
// Usage:
//   cortrace-decode <etm.bin> <mem.bin> <mem_base_hex> <syms.nm>
//                   [--time time.bin] [--perf out.perftrace] [--edges out.tsv]
//
// SPDX-License-Identifier: MIT
#include "cortrace/callstack.hpp"
#include "cortrace/decoder.hpp"
#include "cortrace/deframe.hpp"
#include "cortrace/perfetto_writer.hpp"
#include "cortrace/symbols.hpp"
#include "cortrace/timebase.hpp"

#include <argparse/argparse.hpp>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <sys/resource.h>
#endif

using namespace cortrace;

namespace {

// Tracks which functions the instruction flow actually visited, and the
// blind-region (no-coverage) time, straight off the element stream.
struct Coverage {
    const SymbolTable& syms;
    const TimeBase& tb;

    std::map<std::string, long> ranges_by_fn; // fn -> # of instr ranges landing in it
    std::set<std::string> visited; // functions with any instruction coverage

    bool blind_pending = false;
    bool have_first = false;
    uint64_t first_ns = 0, last_cov_ns = 0, final_ns = 0, blind_ns = 0;
    long blind_regions = 0;

    explicit Coverage(const SymbolTable& s, const TimeBase& t)
        : syms(s)
        , tb(t)
    {
    }

    void observe(const Element& e)
    {
        switch (e.kind) {
        case ElementKind::InstrRange: {
            const std::string& fn = syms.function_at(e.start_addr);
            ranges_by_fn[fn]++;
            visited.insert(fn);
            if (!tb.empty()) {
                const uint64_t now = tb.ns_for(e.byte_index);
                if (!have_first) {
                    first_ns = now;
                    have_first = true;
                }
                if (blind_pending && now > last_cov_ns) {
                    blind_ns += now - last_cov_ns;
                    blind_regions++;
                }
                blind_pending = false;
                last_cov_ns = now;
                final_ns = now;
            }
            break;
        }
        case ElementKind::AddrNacc:
        case ElementKind::TraceOn:
            blind_pending = true;
            break;
        case ElementKind::Timestamp:
            ts_count++;
            if (!have_first_ts) {
                first_ts = e.timestamp;
                have_first_ts = true;
            }
            last_ts = e.timestamp;
            break;
        default:
            break;
        }
    }

    long ts_count = 0;
    bool have_first_ts = false;
    uint64_t first_ts = 0, last_ts = 0;
};

} // namespace

int main(int argc, char** argv)
{
    argparse::ArgumentParser program("cortrace-decode");
    program.add_description(
        "Decode a captured ETMv4 stream: raw ETM bytes -> OpenCSD -> call-stack "
        "-> Perfetto, with quality metrics and a function-coverage report.");

    program.add_argument("etm").help("input ETM bytes, or a raw FPGA capture with --raw");
    program.add_argument("mem").help("flat memory image of the ELF");
    program.add_argument("mem_base").help("load address (hex) of mem's first byte, e.g. 08000000");
    program.add_argument("syms").help("ELF symbol table (arm-none-eabi-nm output)");

    program.add_argument("--time")
        .metavar("time.bin")
        .help("FPGA ETF-egress ns-per-ETM-byte time base");
    program.add_argument("--etm-time")
        .flag()
        .help("use the ETM global timestamp (execution time) as the Perfetto time base");
    program.add_argument("--tsgen-hz")
        .metavar("HZ")
        .scan<'g', double>()
        .default_value(0.0)
        .help("TSGEN frequency for --etm-time count->ns (0 = raw counts as ticks)");
    program.add_argument("--perf").metavar("out.perftrace").help("write a Perfetto trace");
    program.add_argument("--edges").metavar("out.tsv").help("write call edges for ELF cross-check");
    program.add_argument("--events")
        .metavar("out.log")
        .help("plain +B/-E event log for structural verify");
    program.add_argument("--memory-limit-mb")
        .metavar("N")
        .scan<'i', long>()
        .default_value<long>(4096)
        .help("cap virtual memory (0 disables); guards a libopencsd allocation blowup");
    program.add_argument("--strict").flag().help("exit 1 if ANY loss/patch heuristic fired");
    program.add_argument("--raw").flag().help(
        "<etm> is a raw FPGA capture; deframe it first (nibble reassemble + TPIU stream 2)");
    program.add_argument("--dump-etm")
        .metavar("out.bin")
        .help("with --raw, also write the deframed ETM bytes");
    program.add_argument("--phase").metavar("P,O").help(
        "lock the deframe phase (skip the search), e.g. 1,0 for the A7-Lite default");

    try {
        program.parse_args(argc, argv);
    } catch (const std::exception& err) {
        std::fprintf(stderr, "%s\n", err.what());
        std::cerr << program;
        return 2;
    }

    const std::string etm_path = program.get("etm");
    const std::string mem_path = program.get("mem");
    const uint32_t mem_base
        = static_cast<uint32_t>(std::strtoul(program.get("mem_base").c_str(), nullptr, 16));
    const std::string syms_path = program.get("syms");

    const auto time_opt = program.present("--time");
    const auto perf_opt = program.present("--perf");
    const auto edges_opt = program.present("--edges");
    const auto events_opt = program.present("--events");
    const char* time_path = time_opt ? time_opt->c_str() : nullptr;
    const char* perf_path = perf_opt ? perf_opt->c_str() : nullptr;
    const bool etm_time = program.get<bool>("--etm-time");
    const double tsgen_hz = program.get<double>("--tsgen-hz");
    const char* edges_path = edges_opt ? edges_opt->c_str() : nullptr;
    const char* events_path = events_opt ? events_opt->c_str() : nullptr;

    // Self-cap virtual memory. libopencsd 1.8.3 has a pathological allocation
    // on some (currently uncharacterised) input patterns above ~1.9 MB of ETM
    // -- observed on-board with S2 selftrace captures: RSS jumps from ~40 MB
    // to 22+ GB across a small input-size increase (2026-09-07 investigation).
    // Once malloc fails, cortrace treats it as a normal fatal decode response
    // and gracefully returns partial-but-balanced results.
    const long mem_limit_mb = program.get<long>("--memory-limit-mb");
    // --strict: any nonzero loss/patch metric => exit non-zero. This is the
    // mode you use when you must prove "no silent loss / no silent heuristic
    // patching" -- e.g. the deterministic self-trace verification loop.
    const bool strict = program.get<bool>("--strict");

    const auto dump_opt = program.present("--dump-etm");
    const char* dump_etm_path = dump_opt ? dump_opt->c_str() : nullptr;

    // --phase P,O locks the deframe phase and implies --raw. Defaults to the
    // A7-Lite board (parity=1, order=0).
    DeframePhase phase;
    bool phase_locked = false;
    if (auto phase_opt = program.present("--phase")) {
        const std::string& pv = *phase_opt;
        char* end = nullptr;
        phase.parity = static_cast<int>(std::strtol(pv.c_str(), &end, 10));
        phase.order
            = (end && *end == ',') ? static_cast<int>(std::strtol(end + 1, nullptr, 10)) : 0;
        phase_locked = true;
    }
    const bool raw_input = program.get<bool>("--raw") || phase_locked;

    // Apply the memory cap before we allocate anything decoder-related.
    if (mem_limit_mb > 0) {
#if defined(__unix__) || defined(__APPLE__)
        struct rlimit rl;
        rl.rlim_cur = static_cast<rlim_t>(mem_limit_mb) * 1024ULL * 1024ULL;
        rl.rlim_max = rl.rlim_cur;
        if (setrlimit(RLIMIT_AS, &rl) != 0) {
            std::fprintf(
                stderr, "warning: could not set memory cap %ld MB (continuing)\n", mem_limit_mb);
        } else {
            std::fprintf(stderr, "memory cap: %ld MB (RLIMIT_AS)\n", mem_limit_mb);
        }
#endif
    }

    SymbolTable syms;
    try {
        std::size_t n = syms.load_nm(syms_path);
        std::fprintf(stderr, "loaded %zu function symbols\n", n);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "error: %s\n", e.what());
        return 1;
    }

    TimeBase tb;
    if (time_path) {
        try {
            std::size_t n = tb.load(time_path);
            std::fprintf(stderr, "loaded %zu ns time-base entries\n", n);
        } catch (const std::exception& e) {
            std::fprintf(stderr, "error: %s\n", e.what());
            return 1;
        }
    }

    CallStackMachine machine(syms);
    Coverage cov(syms, tb);

    EtmV4Config cfg; // STM32H743 Cortex-M7 defaults
    auto decoder = make_opencsd_decoder(cfg);
    if (!decoder) {
        std::fprintf(stderr, "error: failed to create OpenCSD decoder\n");
        return 1;
    }
    if (!decoder->add_memory_image(mem_base, mem_path)) {
        std::fprintf(
            stderr, "error: failed to add memory image %s @ 0x%08x\n", mem_path.c_str(), mem_base);
        return 1;
    }
    decoder->set_sink([&](const Element& e) {
        cov.observe(e);
        machine.process(e);
    });

    FILE* f = std::fopen(etm_path.c_str(), "rb");
    if (!f) {
        std::fprintf(stderr, "error: cannot open %s\n", etm_path.c_str());
        return 1;
    }

    std::size_t total = 0;
    bool fatal = false;

    // --raw: read the whole capture, deframe it in-process (nibble reassemble +
    // TPIU stream 2), then feed the resulting ETM bytes to the decoder. This
    // replaces the slow host Python deframe_to_etm.py path.
    if (raw_input) {
        std::fseek(f, 0, SEEK_END);
        long sz = std::ftell(f);
        std::fseek(f, 0, SEEK_SET);
        std::vector<uint8_t> capture(sz > 0 ? static_cast<std::size_t>(sz) : 0);
        std::size_t got = capture.empty() ? 0 : std::fread(capture.data(), 1, capture.size(), f);
        std::fclose(f);
        f = nullptr;

        DeframeResult dr = deframe_raw_capture(
            capture.data(), got, /*want_stream=*/2, /*search=*/!phase_locked, phase);
        std::fprintf(stderr,
            "deframe: raw=%zu B -> etm=%zu B  phase=(parity=%d,order=%d)  "
            "A-syncs=%d  frames=%zu\n",
            got, dr.etm.size(), dr.phase.parity, dr.phase.order, dr.async_count, dr.frames);

        if (dump_etm_path) {
            FILE* df = std::fopen(dump_etm_path, "wb");
            if (df) {
                std::fwrite(dr.etm.data(), 1, dr.etm.size(), df);
                std::fclose(df);
                std::fprintf(stderr, "wrote deframed ETM -> %s\n", dump_etm_path);
            }
        }

        total = dr.etm.size();
        if (!decoder->process(dr.etm.data(), dr.etm.size())) {
            std::fprintf(stderr, "error: opencsd fatal while decoding deframed stream\n");
            fatal = true;
        }
    } else {
        std::vector<uint8_t> buf(1 << 16);
        std::size_t n;
        while ((n = std::fread(buf.data(), 1, buf.size(), f)) > 0) {
            total += n;
            if (!decoder->process(buf.data(), n)) {
                std::fprintf(stderr,
                    "error: opencsd fatal at %zu bytes (byte index into ETM stream). "
                    "This is a hard decode failure on Layer F (libopencsd). Silent recovery "
                    "would mask real data corruption or a decoder bug; use "
                    "--allow-fatal-restart for triage only.\n",
                    total);
                fatal = true;
                break;
            }
        }
        std::fclose(f);
        f = nullptr;
    }
    decoder->flush();
    machine.finish();

    const StackMetrics& m = machine.metrics();
    std::fprintf(stderr, "\n=== cortrace decode result ===\n");
    std::fprintf(
        stderr, "  etm bytes processed : %zu%s\n", total, fatal ? " (stopped: fatal)" : "");
    std::fprintf(stderr, "  begins / ends       : %ld / %ld  (%s)\n", m.begins, m.ends,
        m.balanced() ? "balanced" : "UNBALANCED");
    std::fprintf(stderr, "  max depth           : %d\n", m.max_depth);
    std::fprintf(stderr, "  mismatched returns  : %ld\n", m.mismatched_returns);
    std::fprintf(
        stderr, "  dropped calls       : %ld  (callee lost to blind spot)\n", m.dropped_calls);
    std::fprintf(stderr, "  recovered returns   : %ld\n", m.recovered_missed_returns);
    std::fprintf(stderr, "  exceptions rendered : %ld\n", m.exceptions);
    std::fprintf(stderr, "  slice events        : %zu\n", machine.slices().size());
    std::fprintf(stderr, "  ETM timestamps      : %ld", cov.ts_count);
    if (cov.ts_count > 0) {
        std::fprintf(stderr, "  (0x%llx .. 0x%llx, span %llu counts)",
            static_cast<unsigned long long>(cov.first_ts),
            static_cast<unsigned long long>(cov.last_ts),
            static_cast<unsigned long long>(cov.last_ts - cov.first_ts));
    }
    std::fprintf(stderr, "\n");

    // ---- function-coverage report: flow-visited vs slice-rendered ----------
    // A function the instruction flow visited but that never rendered as a
    // begin slice is "missed" on the timeline -- usually because it was only
    // ever entered across a blind spot (dropped call) or is a leaf reached by
    // fall-through/tail-call. Surface these explicitly.
    const auto& begins = machine.slices();
    std::set<std::string> rendered;
    for (const auto& s : begins)
        if (s.begin)
            rendered.insert(s.name);

    std::vector<std::pair<long, std::string>> missed; // (range count, fn)
    for (const auto& kv : cov.ranges_by_fn) {
        const std::string& fn = kv.first;
        if (fn == "?" || fn == "<root>")
            continue;
        if (rendered.find(fn) == rendered.end())
            missed.emplace_back(kv.second, fn);
    }
    std::sort(missed.begin(), missed.end(),
        [](const auto& a, const auto& b) { return a.first > b.first; });

    std::fprintf(stderr, "\n=== function coverage ===\n");
    std::fprintf(stderr, "  functions with instruction flow : %zu\n", cov.visited.size());
    std::fprintf(stderr, "  functions rendered as slices    : %zu\n", rendered.size());
    if (missed.empty()) {
        std::fprintf(stderr, "  MISSED (flow but no slice)      : none\n");
    } else {
        std::fprintf(stderr, "  MISSED (flow but no slice)      : %zu\n", missed.size());
        for (const auto& p : missed)
            std::fprintf(stderr, "      %-28s (%ld instr ranges)\n", p.second.c_str(), p.first);
    }

    // ---- blind-region (no-coverage) time rate ------------------------------
    if (!tb.empty() && cov.final_ns > cov.first_ns) {
        const uint64_t span = cov.final_ns - cov.first_ns;
        const double blind_pct
            = 100.0 * static_cast<double>(cov.blind_ns) / static_cast<double>(span);
        std::fprintf(stderr, "\n=== blind-region rate ===\n");
        std::fprintf(stderr, "  trace span : %.3f us\n", span / 1000.0);
        std::fprintf(stderr, "  blind time : %.3f us over %ld regions\n", cov.blind_ns / 1000.0,
            cov.blind_regions);
        std::fprintf(
            stderr, "  BLIND RATE : %.2f%%  (covered %.2f%%)\n", blind_pct, 100.0 - blind_pct);
    }

    // ---- outputs -----------------------------------------------------------
    if (perf_path) {
        std::vector<SliceEvent> timed;
        const char* base_desc;
        if (etm_time) {
            timed = apply_etm_timestamp(machine.slices(), tsgen_hz);
            base_desc = tsgen_hz > 0.0 ? " (ETM timestamp, ns)" : " (ETM timestamp, raw counts)";
        } else {
            timed = apply_timebase(machine.slices(), tb);
            base_desc = tb.empty() ? " (tick order; pass --time or --etm-time)" : " (ns time base)";
        }
        if (!write_perfetto_trace_multi(perf_path, timed, machine.tracks())) {
            std::fprintf(stderr, "error: cannot write perf to %s\n", perf_path);
            return 1;
        }
        std::fprintf(
            stderr, "\nwrote %zu slice events -> %s%s\n", timed.size(), perf_path, base_desc);
    }
    if (edges_path) {
        FILE* ef = std::fopen(edges_path, "w");
        if (!ef) {
            std::fprintf(stderr, "error: cannot write edges to %s\n", edges_path);
            return 1;
        }
        for (const auto& kv : machine.edges())
            std::fprintf(ef, "%ld\t%s\n", kv.second, kv.first.c_str());
        std::fclose(ef);
        std::fprintf(stderr, "wrote %zu call edges -> %s\n", machine.edges().size(), edges_path);
    }
    if (events_path) {
        // Plain begin/end event log for downstream structural verification
        // (selftrace_strict_verify.py etc). One line per slice event:
        //   +funcname   for begin
        //   -funcname   for end
        // Emitted in tick order, matching the perftrace exactly.
        FILE* ef = std::fopen(events_path, "w");
        if (!ef) {
            std::fprintf(stderr, "error: cannot write events to %s\n", events_path);
            return 1;
        }
        for (const auto& s : machine.slices())
            std::fprintf(ef, "%c%s\n", s.begin ? '+' : '-', s.name.c_str());
        std::fclose(ef);
        std::fprintf(stderr, "wrote %zu events -> %s\n", machine.slices().size(), events_path);
    }

    // --strict verdict. balanced() is necessary but nowhere near sufficient
    // on a deterministic loop -- mismatched_returns / dropped_calls /
    // recovered_missed_returns are all silent heuristic patches that could
    // hide a real fault. Blind time > 0 also means byte-level loss.
    if (strict) {
        bool clean = !fatal && m.balanced() && m.mismatched_returns == 0 && m.dropped_calls == 0
            && m.recovered_missed_returns == 0 && cov.blind_ns == 0 && cov.blind_regions == 0;
        std::fprintf(stderr, "\n=== --strict verdict ===\n");
        std::fprintf(stderr, "  fatal          : %s\n", fatal ? "YES (FAIL)" : "no");
        std::fprintf(stderr, "  balanced       : %s\n", m.balanced() ? "yes" : "NO (FAIL)");
        std::fprintf(stderr, "  mismatched     : %ld%s\n", m.mismatched_returns,
            m.mismatched_returns ? "  (FAIL: heuristic patched a hole)" : "");
        std::fprintf(stderr, "  dropped calls  : %ld%s\n", m.dropped_calls,
            m.dropped_calls ? "  (FAIL: blind-spot swallowed a call)" : "");
        std::fprintf(stderr, "  recovered      : %ld%s\n", m.recovered_missed_returns,
            m.recovered_missed_returns ? "  (FAIL: return patched)" : "");
        std::fprintf(stderr, "  blind regions  : %ld  (%.3f us)\n", cov.blind_regions,
            cov.blind_ns / 1000.0);
        std::fprintf(stderr, "  verdict        : %s\n",
            clean ? "CLEAN (100% observable, no silent patches)"
                  : "DIRTY (any nonzero count above is silent data loss)");
        return clean ? 0 : 1;
    }
    return (m.balanced() && !fatal) ? 0 : 1;
}
