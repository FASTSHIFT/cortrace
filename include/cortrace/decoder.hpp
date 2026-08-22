// Cortrace — decoder interface + OpenCSD-backed implementation.
//
// The decoder turns raw (already deframed) ETMv4 bytes into a stream of
// normalised cortrace::Element objects, delivered one-by-one to a sink
// callback. The upper layers (call-stack machine, sinks) only ever see
// `Element`, never OpenCSD types, so they stay unit-testable with synthetic
// streams and free of a live-decoder dependency.
//
// The OpenCSD implementation is compiled only when libopencsd is present
// (guarded by CORTRACE_HAVE_OPENCSD); the core library links fine without it.
//
// SPDX-License-Identifier: MIT
#ifndef CORTRACE_DECODER_HPP
#define CORTRACE_DECODER_HPP

#include "cortrace/element.hpp"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace cortrace {

// Called for every decoded element, in trace order.
using ElementSink = std::function<void(const Element&)>;

// Minimal ETMv4 target configuration. Defaults match the STM32H743 Cortex-M7
// used to validate the pipeline; override the IDR/config words for other cores.
struct EtmV4Config {
    uint32_t idr0 = 0x080006e1;
    uint32_t idr1 = 0x4100f401;
    uint32_t idr2 = 0x00000004;
    uint32_t idr8 = 0x00000001;
    uint32_t idr9 = 0;
    uint32_t idr10 = 0;
    uint32_t idr11 = 0;
    uint32_t idr12 = 0x00000001;
    uint32_t idr13 = 0;
    uint32_t configr = 0x00000000;
    uint32_t traceidr = 0x00000002; // ETM trace ID on the ATB
};

// Abstract decoder so the CLI / tests can swap in a fake without OpenCSD.
class IDecoder {
public:
    virtual ~IDecoder() = default;

    // Register the memory image OpenCSD reads to follow the instruction flow.
    // `base` is the load address of `path`'s first byte (= lowest LMA of the
    // ELF; see AGENT.md gotcha #2 on the .isr_vector offset).
    virtual bool add_memory_image(uint32_t base, const std::string& path) = 0;

    // Set the sink that receives normalised elements.
    virtual void set_sink(ElementSink sink) = 0;

    // Feed `len` bytes of deframed ETMv4 stream. May be called repeatedly.
    // Returns false on a fatal decode response.
    virtual bool process(const uint8_t* data, std::size_t len) = 0;

    // Signal end-of-trace so the decoder flushes any pending element.
    virtual void flush() = 0;
};

#if defined(CORTRACE_HAVE_OPENCSD)
// Construct an OpenCSD-backed ETMv4 decoder. Returns nullptr on setup failure.
std::unique_ptr<IDecoder> make_opencsd_decoder(const EtmV4Config& cfg);
#endif

} // namespace cortrace

#endif // CORTRACE_DECODER_HPP
