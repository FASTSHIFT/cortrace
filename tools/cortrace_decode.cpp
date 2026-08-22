// cortrace-decode — offline CLI front end.
//
// Runs the full pipeline on a captured, already-deframed ETMv4 byte stream:
//   raw ETM bytes  ->  OpenCSD decode  ->  call-stack machine  ->  metrics
//
// Reports the golden quality criteria (nesting balance, per-function slice
// counts, call edges for ELF cross-check) so a capture can be scored offline
// without touching hardware.
//
// Usage:
//   cortrace-decode <etm.bin> <mem.bin> <mem_base_hex> <syms.nm> [--edges out.tsv]
//
// SPDX-License-Identifier: MIT
#include "cortrace/callstack.hpp"
#include "cortrace/decoder.hpp"
#include "cortrace/symbols.hpp"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using namespace cortrace;

namespace {

int usage(const char* argv0)
{
    std::fprintf(stderr,
        "usage: %s <etm.bin> <mem.bin> <mem_base_hex> <syms.nm> [--edges out.tsv]\n",
        argv0);
    return 2;
}

} // namespace

int main(int argc, char** argv)
{
    if (argc < 5)
        return usage(argv[0]);

    const char* etm_path = argv[1];
    const char* mem_path = argv[2];
    const uint32_t mem_base = static_cast<uint32_t>(std::strtoul(argv[3], nullptr, 16));
    const char* syms_path = argv[4];
    const char* edges_path = nullptr;
    for (int i = 5; i < argc; ++i) {
        if (std::strcmp(argv[i], "--edges") == 0 && i + 1 < argc)
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

    CallStackMachine machine(syms);

    EtmV4Config cfg; // STM32H743 Cortex-M7 defaults
    auto decoder = make_opencsd_decoder(cfg);
    if (!decoder) {
        std::fprintf(stderr, "error: failed to create OpenCSD decoder\n");
        return 1;
    }
    if (!decoder->add_memory_image(mem_base, mem_path)) {
        std::fprintf(stderr, "error: failed to add memory image %s @ 0x%08x\n",
            mem_path, mem_base);
        return 1;
    }
    decoder->set_sink([&machine](const Element& e) { machine.process(e); });

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
    std::fprintf(stderr, "  etm bytes processed : %zu%s\n", total,
        fatal ? " (stopped: fatal)" : "");
    std::fprintf(stderr, "  begins / ends       : %ld / %ld  (%s)\n", m.begins, m.ends,
        m.balanced() ? "balanced" : "UNBALANCED");
    std::fprintf(stderr, "  max depth           : %d\n", m.max_depth);
    std::fprintf(stderr, "  mismatched returns  : %ld\n", m.mismatched_returns);
    std::fprintf(stderr, "  dropped calls       : %ld  (callee lost to blind spot)\n",
        m.dropped_calls);
    std::fprintf(stderr, "  recovered returns   : %ld\n", m.recovered_missed_returns);
    std::fprintf(stderr, "  exceptions rendered : %ld\n", m.exceptions);
    std::fprintf(stderr, "  slice events        : %zu\n", machine.slices().size());

    if (edges_path) {
        FILE* ef = std::fopen(edges_path, "w");
        if (!ef) {
            std::fprintf(stderr, "error: cannot write edges to %s\n", edges_path);
            return 1;
        }
        for (const auto& kv : machine.edges())
            std::fprintf(ef, "%ld\t%s\n", kv.second, kv.first.c_str());
        std::fclose(ef);
        std::fprintf(stderr, "  wrote %zu call edges -> %s\n", machine.edges().size(),
            edges_path);
    }

    return m.balanced() ? 0 : 1;
}
