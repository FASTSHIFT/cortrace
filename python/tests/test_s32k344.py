"""The S32K344 platform file against the manual's numbers (RM Rev. 12).

These pin what the draft says; they do not show that a board traces. See
docs/06-s32k3xx.md for where each number comes from.
"""

import pytest
from test_trace_arm import FakeProbe, order

from cortrace import platdesc, trace_arm
from cortrace.osplugins import Watch


@pytest.fixture(name="k3")
def k3_fixture():
    return platdesc.load("s32k344")


class GatingProbe(FakeProbe):
    """MC_CGM mux 11: CS follows the CG bit, as the manual describes."""

    def __init__(self):
        super().__init__({0xE004100C: 1})

    def write32(self, addr, value):
        super().write32(addr, value)
        if addr == 0x402D85C0:  # MUX_11_CSC
            self.regs[0x402D85C4] = 0 if value & 0x4 else 0x20000


def armed(k3):
    probe = GatingProbe()
    trace_arm.arm(probe, k3, [Watch("g_x", 4)], log=lambda _m: None)
    return probe


def test_the_blocks_are_where_table_9_and_829_put_them(k3):
    blocks = k3["coresight"]
    assert (blocks["itm"], blocks["dwt"], blocks["etm"]) == (
        0xE0000000,
        0xE0001000,
        0xE0041000,
    )
    assert blocks["tpiu"] == 0x4024A000 and blocks["tsgen"] == 0x4024C000
    assert k3["trace"]["max_width"] == 4
    assert k3["clocks"] == {"tsgen_hz": 160000000, "sysclk_hz": 160000000}


def test_path_is_sink_first_through_two_funnels_and_two_etfs(k3):
    probe = armed(k3)
    chain = (0x4024A000, 0x40247000, 0x40243000, 0x40244000, 0x40241000)
    touched = []
    for addr, _ in probe.writes:
        base = addr & ~0xFFF
        if base in chain and base not in touched:
            touched.append(base)
    assert touched == list(chain)
    assert order(probe, 0x40243000) == [0x301]  # funnel 2: port 0 only
    assert order(probe, 0x40241000) == [0x303]  # funnel 0: ETMI and ITM
    assert order(probe, 0x40243004) == []  # no ITM priority on a single port
    assert order(probe, 0x40241004) == [0x1]
    assert order(probe, 0x40247028) == order(probe, 0x40244028) == [2]  # hw FIFO
    assert order(probe, 0x4024A004) == [0x8]  # 4-bit port


def test_trace_clock_is_switched_to_pll_phi1_through_a_gated_switch(k3):
    probe = armed(k3)
    # gate, select PLL_PHI1 (1001b in SELCTL), ungate: the order of RM Fig. 124
    assert order(probe, 0x402D85C0) == [0x4, 0x4 | 0x09000000, 0x09000000]
    assert order(probe, 0x402D85C8) == [0x80010000]  # DE, DIV = 1


def test_pads_are_alt_functions_with_output_on_and_fast_slew(k3):
    probe = armed(k3)
    pads = {  # GPIO number -> SSS, from the IOMUX workbook
        66: 6,  # PTC2  TRACE_ETM_CLKOUT
        103: 6,  # PTD7  TRACE_ETM_D0
        108: 4,  # PTD12 TRACE_ETM_D1
        107: 4,  # PTD11 TRACE_ETM_D2
        106: 4,  # PTD10 TRACE_ETM_D3
    }
    for gpio, sss in pads.items():
        (value,) = order(probe, 0x40290000 + 0x240 + 4 * gpio)  # SIUL2 MSCRn
        assert value & 0x7 == sss
        assert value & (1 << 21)  # OBE
        assert value & (1 << 8)  # DSE
        assert not value & (1 << 14)  # SRC = 0: fastest
        assert value & ~0x0020_0107 == 0  # nothing else
    # DBGRSTFASTPAD is set; the other bits of MDMAPCTL are not touched
    assert probe.regs[0x40250604] == 0x2000


def test_a_stuck_trace_clock_gate_is_an_error(k3):
    probe = FakeProbe({0x402D85C4: 0x20000, 0xE004100C: 1})  # CS never drops
    with pytest.raises(trace_arm.ArmError, match="CS == 0"):
        trace_arm.arm(probe, k3, [Watch("g_x", 4)], log=lambda _m: None)
