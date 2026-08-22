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
#include "cortrace/perfetto_writer.hpp"
#include "cortrace/symbols.hpp"
#include "cortrace/timebase.hpp"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

using namespace cortrace;

namespace {

int usage(const char* argv0)
{
    std::fprintf(stderr,
        "usage: %s <etm.bin> <mem.bin> <mem_base_hex> <syms.nm>\n"
        "          [--time time.bin] [--perf out.perftrace] [--edges out.tsv]\n",
        argv0);
    return 2;
}

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
        default:
            break;
        }
    }
};

} // namespace

int main(int argc, char** argv)
{
    if (argc < 5)
        return usage(argv[0]);

    const char* etm_path = argv[1];
    const char* mem_path = argv[2];
    const uint32_t mem_base = static_cast<uint32_t>(std::strtoul(argv[3], nullptr, 16));
    const char* syms_path = argv[4];
    const char* time_path = nullptr;
    const char* perf_path = nullptr;
    const char* edges_path = nullptr;
    for (int i = 5; i < argc; ++i) {
        if (std::strcmp(argv[i], "--time") == 0 && i + 1 < argc)
            time_path = argv[++i];
        else if (std::strcmp(argv[i], "--perf") == 0 && i + 1 < argc)
            perf_path = argv[++i];
        else if (std::strcmp(argv[i], "--edges") == 0 && i + 1 < argc)
            edges_path = argv[++i];
        else
            return usage(argv[0]);
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
        std::fprintf(stderr, "error: failed to add memory image %s @ 0x%08x\n", mem_path, mem_base);
        return 1;
    }
    decoder->set_sink([&](const Element& e) {
        cov.observe(e);
        machine.process(e);
    });

    FILE* f = std::fopen(etm_path, "rb");
    if (!f) {
        std::fprintf(stderr, "error: cannot open %s\n", etm_path);
        return 1;
    }
    std::vector<uint8_t> buf(1 << 16);
    std::size_t total = 0, n;
    bool fatal = false;
    while ((n = std::fread(buf.data(), 1, buf.size(), f)) > 0) {
        total += n;
        if (!decoder->process(buf.data(), n)) {
            std::fprintf(stderr, "warning: fatal decode response after %zu bytes\n", total);
            fatal = true;
            break;
        }
    }
    std::fclose(f);
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
        const auto timed = apply_timebase(machine.slices(), tb);
        if (!write_perfetto_trace(perf_path, timed)) {
            std::fprintf(stderr, "error: cannot write perf to %s\n", perf_path);
            return 1;
        }
        std::fprintf(stderr, "\nwrote %zu slice events -> %s%s\n", timed.size(), perf_path,
            tb.empty() ? " (tick order; pass --time for ns)" : " (ns time base)");
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

    return m.balanced() ? 0 : 1;
}
