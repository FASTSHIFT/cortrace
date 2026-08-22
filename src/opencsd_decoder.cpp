// Cortrace — OpenCSD-backed ETMv4 decoder adapter.
//
// Wraps the OpenCSD C API and normalises its generic trace elements into
// cortrace::Element. The branch classification (call / return / indirect) is
// derived from OpenCSD's last_i_type / last_i_subtype so the call-stack machine
// never has to reason about ETM packet semantics.
//
// Compiled only when libopencsd is available (CORTRACE_HAVE_OPENCSD).
//
// SPDX-License-Identifier: MIT
#include "cortrace/decoder.hpp"

#include <cstring>

#include "opencsd/c_api/opencsd_c_api.h"
#include "opencsd/ocsd_if_types.h"
#include "opencsd/trc_gen_elem_types.h"

namespace cortrace {
namespace {

    // Translate an OpenCSD instruction type/subtype pair into a cortrace BranchKind.
    BranchKind classify(ocsd_instr_type itype, ocsd_instr_subtype sub)
    {
        switch (itype) {
        case OCSD_INSTR_BR:
            return sub == OCSD_S_INSTR_BR_LINK ? BranchKind::DirectCall : BranchKind::Direct;
        case OCSD_INSTR_BR_INDIRECT:
            if (sub == OCSD_S_INSTR_BR_LINK)
                return BranchKind::IndirectCall;
            if (sub == OCSD_S_INSTR_V7_IMPLIED_RET || sub == OCSD_S_INSTR_V8_RET
                || sub == OCSD_S_INSTR_V8_ERET)
                return BranchKind::Return;
            return BranchKind::IndirectBranch;
        default:
            return BranchKind::None;
        }
    }

    class OpenCsdDecoder final : public IDecoder {
    public:
        explicit OpenCsdDecoder(const EtmV4Config& cfg)
        {
            tree_ = ocsd_create_dcd_tree(OCSD_TRC_SRC_SINGLE, OCSD_DFRMTR_FRAME_MEM_ALIGN);
            if (tree_ == C_API_INVALID_TREE_HANDLE)
                return;

            ocsd_etmv4_cfg ocfg;
            std::memset(&ocfg, 0, sizeof ocfg);
            ocfg.reg_idr0 = cfg.idr0;
            ocfg.reg_idr1 = cfg.idr1;
            ocfg.reg_idr2 = cfg.idr2;
            ocfg.reg_idr8 = cfg.idr8;
            ocfg.reg_idr9 = cfg.idr9;
            ocfg.reg_idr10 = cfg.idr10;
            ocfg.reg_idr11 = cfg.idr11;
            ocfg.reg_idr12 = cfg.idr12;
            ocfg.reg_idr13 = cfg.idr13;
            ocfg.reg_configr = cfg.configr;
            ocfg.reg_traceidr = cfg.traceidr;
            ocfg.arch_ver = ARCH_V7;
            ocfg.core_prof = profile_CortexM;

            unsigned char csid = 0;
            if (ocsd_dt_create_decoder(
                    tree_, OCSD_BUILTIN_DCD_ETMV4I, OCSD_CREATE_FLG_FULL_DECODER, &ocfg, &csid)
                != OCSD_OK) {
                teardown();
                return;
            }
            if (ocsd_dt_set_gen_elem_outfn(tree_, &OpenCsdDecoder::elem_trampoline, this)
                != OCSD_OK) {
                teardown();
                return;
            }
            ok_ = true;
        }

        ~OpenCsdDecoder() override { teardown(); }

        bool valid() const { return ok_; }

        bool add_memory_image(uint32_t base, const std::string& path) override
        {
            if (!ok_)
                return false;
            return ocsd_dt_add_binfile_mem_acc(tree_, base, OCSD_MEM_SPACE_ANY, path.c_str())
                == OCSD_OK;
        }

        void set_sink(ElementSink sink) override { sink_ = std::move(sink); }

        bool process(const uint8_t* data, std::size_t len) override
        {
            if (!ok_)
                return false;
            std::size_t consumed = 0;
            while (consumed < len) {
                uint32_t used = 0;
                ocsd_datapath_resp_t r
                    = ocsd_dt_process_data(tree_, OCSD_OP_DATA, index_ + consumed,
                        static_cast<uint32_t>(len - consumed), data + consumed, &used);
                consumed += used;
                if (OCSD_DATA_RESP_IS_FATAL(r))
                    return false;
                if (used == 0) // decoder wants no more data from this buffer
                    break;
            }
            index_ += len;
            return true;
        }

        void flush() override
        {
            if (ok_)
                ocsd_dt_process_data(tree_, OCSD_OP_EOT, index_, 0, nullptr, nullptr);
        }

    private:
        void teardown()
        {
            if (tree_ != C_API_INVALID_TREE_HANDLE) {
                ocsd_destroy_dcd_tree(tree_);
                tree_ = C_API_INVALID_TREE_HANDLE;
            }
            ok_ = false;
        }

        void emit(const Element& e)
        {
            if (sink_)
                sink_(e);
        }

        void on_element(ocsd_trc_index_t idx, const ocsd_generic_trace_elem* elem)
        {
            const uint64_t bidx = static_cast<uint64_t>(idx);
            switch (elem->elem_type) {
            case OCSD_GEN_TRC_ELEM_INSTR_RANGE:
                emit(Element::instr_range(static_cast<uint32_t>(elem->st_addr),
                    static_cast<uint32_t>(elem->en_addr),
                    classify(elem->last_i_type, elem->last_i_subtype), elem->last_instr_exec != 0,
                    bidx));
                break;
            case OCSD_GEN_TRC_ELEM_EXCEPTION:
                emit(Element::exception(elem->exception_number, bidx));
                break;
            case OCSD_GEN_TRC_ELEM_EXCEPTION_RET:
                emit(Element::simple(ElementKind::ExceptionRet, bidx));
                break;
            case OCSD_GEN_TRC_ELEM_TRACE_ON:
                emit(Element::simple(ElementKind::TraceOn, bidx));
                break;
            case OCSD_GEN_TRC_ELEM_ADDR_NACC:
                emit(Element::simple(ElementKind::AddrNacc, bidx));
                break;
            case OCSD_GEN_TRC_ELEM_TIMESTAMP:
                emit(Element::simple(ElementKind::Timestamp, bidx));
                break;
            default:
                break;
            }
        }

        static ocsd_datapath_resp_t elem_trampoline(const void* ctx, const ocsd_trc_index_t idx_sop,
            const uint8_t /*cs_id*/, const ocsd_generic_trace_elem* elem)
        {
            auto* self = static_cast<OpenCsdDecoder*>(const_cast<void*>(ctx));
            self->on_element(idx_sop, elem);
            return OCSD_RESP_CONT;
        }

        dcd_tree_handle_t tree_ = C_API_INVALID_TREE_HANDLE;
        ElementSink sink_;
        std::size_t index_ = 0;
        bool ok_ = false;
    };

} // namespace

std::unique_ptr<IDecoder> make_opencsd_decoder(const EtmV4Config& cfg)
{
    auto dec = std::make_unique<OpenCsdDecoder>(cfg);
    if (!dec->valid())
        return nullptr;
    return dec;
}

} // namespace cortrace
