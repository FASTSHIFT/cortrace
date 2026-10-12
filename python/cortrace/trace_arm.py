"""Arm the trace hardware of a Cortex-M through the debug probe.

The sequence is the architecture's: unlock each CoreSight block, route the ETM
and the ITM/DWT through the funnel and the ETF into the parallel TPIU, start the
ETM, and make the DWT write a data-value packet whenever the OS writes the
"current task" pointer. Where the blocks are and what the chip needs first comes
from the platform file; which variable to watch comes from the OS plugin.

This is the register sequence of cortrace-fpga's nxtrace_dap.cfg, driven over
the probe's telnet port instead of OpenOCD's Tcl.
"""

CS_UNLOCK_KEY = 0xC5ACCE55
LAR = 0xFB0  # CoreSight Lock Access Register

DEMCR = 0xE000EDFC
DEMCR_TRCENA = 1 << 24

MAX_COMPARATORS = 4
DWT_COMP_STRIDE = 0x10
DWT_FUNC_DATA_VALUE_WRITE = 0x0D  # data-value packet on write


class ArmError(Exception):
    """The trace hardware did not behave as the sequence expects."""


class Regs:
    """Read-modify-write helpers over a probe."""

    def __init__(self, probe):
        self.probe = probe

    def write(self, addr, value):
        """Write a 32-bit register."""
        self.probe.write32(addr, value)

    def read(self, addr):
        """Read a 32-bit register."""
        return self.probe.read32(addr)

    def rmw(self, addr, set_bits=0, clear_bits=0):
        """(value & ~clear_bits) | set_bits, like OpenOCD's mmw."""
        self.write(addr, (self.read(addr) & ~clear_bits) | set_bits)

    def poll(self, addr, mask, tries):
        """Wait until any bit of `mask` is set; False if it never was."""
        return any(self.read(addr) & mask for _ in range(tries))

    def unlock(self, base):
        """Unlock a CoreSight block."""
        self.write(base + LAR, CS_UNLOCK_KEY)


def run_chip_steps(regs, steps, log):
    """Execute the platform's [[on_trace_start]] steps."""
    for step in steps:
        op = step["op"]
        if op == "write32":
            regs.write(step["addr"], step["value"])
        elif op == "rmw32":
            regs.rmw(step["addr"], step.get("set", 0), step.get("clear", 0))
        elif not regs.poll(step["addr"], step["mask"], step.get("tries", 1000)):
            what = step.get("comment", f"0x{step['addr']:08x}")
            if step.get("required", False):
                raise ArmError(f"timed out: {what}")
            log(f"  {what}: not set after {step.get('tries', 1000)} reads, going on")
            continue
        log(f"  {step.get('comment', op)}")


def arm_tpiu(regs, base, width):
    """Parallel port of `width` bits, continuous formatter."""
    regs.unlock(base)
    regs.write(base + 0x004, 1 << (width - 1))  # CSPSR: current port size
    regs.write(base + 0x0F0, 0)  # SPPR: parallel
    regs.write(base + 0x304, 0x102)  # FFCR: EnFCont | EnFTC


def arm_funnel(regs, base):
    """Enable the ETM (slave 0) and ITM/DWT (slave 1) inputs; ITM has priority."""
    regs.unlock(base)
    regs.write(base + 0x000, 0x303)
    regs.rmw(base + 0x004, set_bits=0x1, clear_bits=0x3F)


def arm_etf(regs, base):
    """Embedded trace FIFO as a hardware FIFO between the funnel and the TPIU."""
    regs.write(base + 0x020, 0)  # CTL: off while programming
    regs.unlock(base)
    regs.write(base + 0x028, 0x2)  # MODE: hardware FIFO
    regs.write(base + 0x304, 0x1)  # FFCR: EnFt
    regs.write(base + 0x020, 0x1)  # CTL: TraceCaptEn


def _etm_wait_idle(regs, base):
    regs.poll(base + 0x00C, 0x1, 100)  # TRCSTATR.IDLE


def arm_etm(regs, base, trace_id):
    """Program the ETMv4 for full instruction trace with cycle counts and
    timestamps (no bandwidth broadcast, so the times are execution times)."""
    regs.unlock(base)
    regs.write(base + 0x310, 0x8)  # TRCPDCR: power up
    regs.write(base + 0x004, 0)  # TRCPRGCTLR: off while programming
    _etm_wait_idle(regs, base)
    regs.write(base + 0x010, 0x810)  # TRCCONFIGR: TS | CCI
    regs.write(base + 0x040, trace_id)  # TRCTRACEIDR
    regs.write(base + 0x038, 0x10)  # TRCCCCTLR: cycle-count threshold
    regs.write(base + 0x02C, 0x100)  # TRCSTALLCTLR: instruction stall only
    regs.write(base + 0x080, 0x201)  # TRCVICTLR: trace everything
    regs.write(base + 0x034, 0xC)  # TRCSYNCPR: sync every 4 KiB
    regs.write(base + 0x030, 0)  # TRCTSCTLR: implicit timestamps
    regs.write(base + 0x004, 1)  # TRCPRGCTLR: on


def hold_etm_off(regs, base):
    """Leave the ETM disabled. It has to be written: a system reset does not
    reset the debug domain, so an ETM enabled by an earlier session would
    keep tracing."""
    regs.unlock(base)
    regs.write(base + 0x004, 0)
    _etm_wait_idle(regs, base)


def arm_itm(regs, base, trace_id):
    """All stimulus ports on; the ITM/DWT stream gets `trace_id` on the bus."""
    regs.unlock(base)
    regs.write(base + 0xE40, 0)  # TPR: no privilege restriction
    regs.write(base + 0xE00, 0xFFFFFFFF)  # TER: all ports
    # TCR: ITMENA | TSENA | SYNCENA | TXENA | TraceBusID
    regs.write(base + 0xE80, (trace_id << 16) | 0xF)


def arm_dwt(regs, base, watches):
    """Cycle counter on; one comparator per watched variable, each emitting a
    data-value packet on every write."""
    if len(watches) > MAX_COMPARATORS:
        raise ArmError(
            f"{len(watches)} variables to watch, the DWT has {MAX_COMPARATORS} "
            "comparators"
        )
    regs.write(base + 0x004, 0)  # CYCCNT
    regs.write(base + 0x000, 0x40000001)  # CTRL: CYCCNTENA
    for i, watch in enumerate(watches):
        comp = base + 0x020 + DWT_COMP_STRIDE * i
        regs.write(comp + 0x8, 0)  # FUNCTION off while programming
        regs.write(comp + 0x0, watch.addr)
        regs.write(comp + 0x4, 0)  # MASK: exact address
        regs.write(comp + 0x8, DWT_FUNC_DATA_VALUE_WRITE)


def arm(probe, platform, watches, *, width=4, etm=True, log=print):
    """Halt the core, arm everything, resume. Returns a few read-back values."""
    max_width = platform["trace"]["max_width"]
    if not 1 <= width <= max_width:
        raise ArmError(f"trace width {width} not supported (1..{max_width})")
    regs = Regs(probe)
    blocks = platform["coresight"]
    trace = platform["trace"]
    probe.halt()
    log("chip steps:")
    run_chip_steps(regs, platform["on_trace_start"], log)
    regs.rmw(DEMCR, set_bits=DEMCR_TRCENA)
    regs.rmw(blocks["tsgen"] + 0x000, set_bits=0x1)  # CNTCR.EN: timestamps run
    arm_tpiu(regs, blocks["tpiu"], width)
    arm_funnel(regs, blocks["cstf"])
    arm_etf(regs, blocks["etf"])
    if etm:
        arm_etm(regs, blocks["etm"], trace["etm_trace_id"])
    else:
        hold_etm_off(regs, blocks["etm"])
    arm_itm(regs, blocks["itm"], trace["itm_trace_id"])
    arm_dwt(regs, blocks["dwt"], watches)
    state = {
        "dwt_comp0": regs.read(blocks["dwt"] + 0x020),
        "dwt_func0": regs.read(blocks["dwt"] + 0x028),
        "itm_tcr": regs.read(blocks["itm"] + 0xE80),
    }
    probe.resume()
    return state
