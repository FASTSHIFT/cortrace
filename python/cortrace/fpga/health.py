"""health — one-shot FPGA observability readout (proposal 30 P1).

Reads the dbg_regfile in trace_mmcm_stream_top over the :5001 readout path
(which is independent of the self-TX data stream, so it works even when the
trace stream is deadlocked) and prints a human-readable health diagnosis.

The readout protocol (fpga_core_net ext_addr/ext_data, port 5001):
  request  = <u16 base LE> + padding
  reply    = [2 echo bytes][ ext_data[base], ext_data[base+1], ... ]
Note the RTL has a documented 1-cycle stale-read on the FIRST data byte, so we
read each register at its own base with a couple of extra bytes and take the
byte at the position that is known-good (index 1 of the data region).

Usage: cortrace fpga health [ip] [--check {health,ddr3,blackbox}] [--reset]
"""

import argparse
import socket
import struct
import time

IP = "192.168.10.42"  # module-level target; main() sets it from argv
PORT = 5001
CTRL_PORT = 5002
REG_SOFTRST = 0x10  # CSR: soft-reset the trace capture path (MMCM/FIFO/debug)


def soft_reset(ip):
    """Trigger the FPGA trace-path soft reset via :5002 (proposal 30). Recovers
    a stuck capture MMCM / clears sticky debug counters without a power cycle."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    s.sendto(bytes([REG_SOFTRST, 1, 0, 0]), (ip, CTRL_PORT))
    try:
        s.recvfrom(2048)
    except socket.timeout:
        pass
    s.close()
    print("soft reset pulsed (trace MMCM/FIFO + debug counters cleared)")


# dbg_regfile address map (low byte of ext_addr, page 0xFF1x..0xFF3x)
A_MAGIC = 0xFF10
A_LIVE = 0xFF11
A_CYC = 0xFF12  # 4 bytes LE
A_FIRST_CODE = 0xFF16  # 2 bytes LE
A_FIRST_TIME = 0xFF18  # 4 bytes LE
A_FIRST_CTX = 0xFF1C
A_CNT_BASE = 0xFF20  # 7 counters
A_GPIO_LEVEL = 0xFF30  # {0,0,0,clk,d3,d2,d1,d0}
A_GPIO_EDGES = 0xFF31  # clk(2) d0(2) d1(2) d2(2) d3(2) LE, 10 bytes
A_FREQ = 0xFF3B  # TRACECLK edges per 16.777ms window, 3 bytes LE
A_GAP = 0xFF3E  # gap_count(2) gap_max(2) LE
# DDR3 self-test status page (proposal 32 P2a), in trace_ddr_selftest_top
A_DDR3_MAGIC = 0xFF50  # 0xD3
A_DDR3_FLAGS = 0xFF51  # {..., err_sticky, calib_done}
A_DDR3_ERRC = 0xFF52  # 4 bytes LE  (compare error count)
A_DDR3_PASSB = 0xFF56  # 4 bytes LE  (bursts verified error-free)
A_DDR3_STATE = 0xFF5A  # 2-bit FSM state (0=ARBIT 1=WRITE 2=READ)
A_DDR3_BADW = 0xFF5B  # first-mismatch word index (10-bit: 5B lo, 5C hi2)
A_DDR3_EXPLO = 0xFF5D  # expected byte[0] at first mismatch
A_DDR3_GOTLO = 0xFF5E  # actual   byte[0] at first mismatch
A_DDR3_EXPHI = 0xFF5F  # expected byte[15:14] (2 bytes LE)
A_DDR3_GOTHI = 0xFF61  # actual   byte[15:14] (2 bytes LE)
A_BUILD_ID = 0xFF70  # 4 bytes LE — Unix epoch stamped at synth (build identity)
FREQ_WINDOW_S = (1 << 21) / 125e6  # 16.777 ms
CLK_NS = 8.0  # clk125 period (ns)

ERR_NAMES = {
    0x0000: "none",
    0x0101: "no TRACECLK edges (GPIO/trace clock absent)",
    0x0102: "trace MMCM lost lock (wrong TRACECLK freq / dropout)",
    0x0301: "capture FIFO overflow (ETM trace rate > drain)",
    0x0401: "self-TX HDR stuck (ARP not resolved — network self-TX deadlock)",
    0x0501: "MAC RX bad frame",
    0x0502: "MAC TX FIFO overflow",
    0x0503: "MAC RX FIFO overflow",
}
CNT_NAMES = [
    "no_traceclk",
    "mmcm_unlock",
    "cap_overflow",
    "selftx_stuck",
    "rx_bad_frame",
    "tx_fifo_ovf",
    "rx_fifo_ovf",
]
FSM_NAMES = {0: "IDLE", 1: "HDR", 2: "SEND", 3: "BACKOFF"}


def rd(s, base, n):
    """Read n bytes starting at ext_addr=base. The RTL reply repeats source[base]
    on the first beats (1-cycle stale read + paging), observed as the reply
    starting with the base value several times then advancing: for base FF10 the
    reply is 'db db db 08 07 ...' = ext[base]x(stale) then ext[base+1].. So the
    value for `base` is at index 2 and base+k at index 2+k."""
    s.sendto(struct.pack("<H", base & 0xFFFF) + bytes(n + 4), (IP, PORT))
    d, _ = s.recvfrom(2048)
    return d[2 : 2 + n]


def rd8(s, addr):
    return rd(s, addr, 1)[0]


def ddr3_check(ip):
    """Read the DDR3 self-test status page (0xFF5x) from trace_ddr_selftest_top
    (proposal 32 P2a) and report calib / compare-error / pass-burst state."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)
    global IP  # pylint: disable=global-statement
    IP = ip
    try:
        magic = rd8(s, A_DDR3_MAGIC)
    except socket.timeout:
        print("[FAIL] no reply on :5001 — FPGA network down (cold-boot the FPGA)")
        return 1
    if magic != 0xD3:
        print(
            f"[WARN] DDR3 status magic=0x{magic:02x} (expected 0xD3). This "
            f"bitstream may not have the DDR3 self-test (need trace_ddr_selftest)."
        )
        return 1

    def rd_le(base, n):
        return int.from_bytes(rd(s, base, n), "little")

    flags = rd8(s, A_DDR3_FLAGS)
    calib = flags & 1
    err_sticky = (flags >> 1) & 1
    mig_calib = (flags >> 2) & 1
    errc = rd_le(A_DDR3_ERRC, 4)
    passb = rd_le(A_DDR3_PASSB, 4)
    state = rd8(s, A_DDR3_STATE) & 0x3
    st_name = {0: "ARBIT", 1: "WRITE", 2: "READ"}.get(state, state)
    build_id = rd_le(A_BUILD_ID, 4)
    import datetime

    bstr = (
        datetime.datetime.fromtimestamp(build_id).strftime("%Y-%m-%d %H:%M:%S")
        if 0 < build_id < 0xFFFFFFF0
        else "??"
    )
    print("[OK]   DDR3 status page online (magic=0xD3)")
    print(
        f"       BUILD_ID = {build_id} ({bstr})  <- verify this matches the "
        f"latest build log"
    )
    print(
        f"       calib_done={calib}  mig_calib_raw={mig_calib}  "
        f"err_sticky={err_sticky}  err_count={errc}  pass_bursts={passb}  fsm={st_name}"
    )
    if not calib:
        print(
            "[FAIL] MIG DDR3 NOT calibrated — cold-boot the FPGA (SRAM load "
            "leaves MIG mis-calibrated; needs a clean power cycle)"
        )
        return 2
    if err_sticky or errc:
        badw = rd8(s, A_DDR3_BADW) | ((rd8(s, A_DDR3_BADW + 1) & 0x3) << 8)
        exp_lo = rd8(s, A_DDR3_EXPLO)
        got_lo = rd8(s, A_DDR3_GOTLO)
        exp_hi = rd_le(A_DDR3_EXPHI, 2)
        got_hi = rd_le(A_DDR3_GOTHI, 2)
        print(
            f"[FAIL] DDR3 data compare MISMATCH (err_count={errc}) — datapath "
            f"or timing problem"
        )
        print(
            f"       first bad @ word[{badw}]:  "
            f"byte0 exp=0x{exp_lo:02x} got=0x{got_lo:02x}   "
            f"byte15:14 exp=0x{exp_hi:04x} got=0x{got_hi:04x}"
        )
        if got_lo == 0 and got_hi == 0:
            print(
                "       -> readback is all-zero: DDR3 not returning data "
                "(read path / addr / calibration marginal)"
            )
        elif badw == 0:
            print(
                "       -> first word wrong: likely address/burst-align or a "
                "systematic pattern/CDC issue, not random bit errors"
            )
        return 2
    if passb == 0:
        print("[WARN] calibrated but no verified bursts yet — read again")
        return 0
    print(f"[OK]   DDR3 PASS — calibrated + {passb} bursts byte-exact, 0 errors")
    return 0


def blackbox_check(ip):
    """Read the trace black-box writer status page (0xFF5x, magic 0xB0) from
    trace_ddr_blackbox_top (proposal 32 P2b-1): calib, TRACECLK activity,
    words written to the DDR3 ring, write pointer, and capture-side loss."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)
    global IP  # pylint: disable=global-statement
    IP = ip
    try:
        magic = rd8(s, 0xFF50)
    except socket.timeout:
        print("[FAIL] no reply on :5001 — FPGA network down (cold-boot the FPGA)")
        return 1
    if magic != 0xB0:
        print(
            f"[WARN] black-box magic=0x{magic:02x} (expected 0xB0). Wrong "
            f"bitstream? (need trace_ddr_blackbox)"
        )
        return 1

    def rd_le(base, n):
        return int.from_bytes(rd(s, base, n), "little")

    build_id = rd_le(0xFF70, 4)
    import datetime

    bstr = (
        datetime.datetime.fromtimestamp(build_id).strftime("%Y-%m-%d %H:%M:%S")
        if 0 < build_id < 0xFFFFFFF0
        else "??"
    )
    flags = rd8(s, 0xFF51)
    calib = flags & 1
    mig_calib = (flags >> 1) & 1
    tck_active = (flags >> 2) & 1
    words = rd_le(0xFF52, 4)
    lost = rd_le(0xFF56, 4)
    wr_ptr = rd_le(0xFF5A, 4) & 0x1FFFFFFF
    print("[OK]   black-box status online (magic=0xB0)")
    print(f"       BUILD_ID = {build_id} ({bstr})")
    print(
        f"       calib={calib}  mig_calib_raw={mig_calib}  TRACECLK_active={tck_active}"
    )
    print(
        f"       words_written={words} (128-bit) = {words*16} bytes  "
        f"wr_ptr={wr_ptr} words  cap_lost={lost} bytes"
    )
    if not calib:
        print("[FAIL] MIG not calibrated — cold-boot the FPGA")
        return 2
    if not tck_active:
        print(
            "[WARN] no TRACECLK activity — configure the STM32 ETM so trace "
            "bytes flow into the black box"
        )
        return 0
    if lost:
        print(f"[WARN] {lost} capture-side bytes dropped (AsyncFIFO backpressure)")
    # reader debug (P2b-3): FSM state + words left/done for the last readback
    rdst = rd8(s, 0xFF60)
    rd_busy = (rdst >> 2) & 1
    rd_state = rdst & 0x3
    wleft = rd_le(0xFF61, 4)
    wdone = rd_le(0xFF65, 4)
    st_name = {0: "IDLE", 1: "START", 2: "RUN", 3: "NEXT"}.get(rd_state, rd_state)
    print(
        f"       reader: state={st_name} busy={rd_busy} words_left={wleft} "
        f"words_done={wdone}"
    )
    if words == 0:
        print("[WARN] TRACECLK active but 0 words written yet — read again")
        return 0
    print(
        f"[OK]   BLACK BOX RECORDING — {words*16} bytes captured to DDR3, "
        f"cap_lost={lost}"
    )
    return 0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="cortrace fpga health",
        description="One-shot FPGA observability readout over the :5001 readout path.",
    )
    ap.add_argument(
        "ip", nargs="?", default=IP, help="FPGA address (default: %(default)s)"
    )
    ap.add_argument(
        "--check",
        choices=("health", "ddr3", "blackbox"),
        default="health",
        help="health: debug register file (default); ddr3: DDR3 self-test status "
        "page; blackbox: black-box writer status",
    )
    ap.add_argument(
        "--reset",
        action="store_true",
        help="pulse a soft reset first, so the readout reflects a cleared state "
        "(--check health only)",
    )
    return ap.parse_args(argv)


def main(argv=None):
    global IP  # pylint: disable=global-statement
    a = parse_args(argv)
    IP = a.ip
    if a.check == "blackbox":
        return blackbox_check(IP)
    if a.check == "ddr3":
        return ddr3_check(IP)
    return health_check(a.reset)


def health_check(
    reset=False,
):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Debug-register readout and diagnosis; returns the exit code."""
    if reset:
        soft_reset(IP)
        time.sleep(0.2)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)

    try:
        magic = rd8(s, A_MAGIC)
    except socket.timeout:
        print(
            "[FAIL] no reply on :5001 — FPGA network/readout path down "
            "(power-cycle or reflash the FPGA)"
        )
        return 1

    if magic != 0xDB:
        print(
            f"[WARN] debug regfile magic = 0x{magic:02x} (expected 0xDB). "
            f"Old bitstream without proposal-30 observability? Falling back."
        )
        # still try the legacy lost_cnt/lock readout
        return 0

    print(f"[OK]   debug regfile online (magic=0x{magic:02x} v1)")

    live = rd8(s, A_LIVE)
    have_first = (live >> 7) & 1
    pkt_active = (live >> 6) & 1
    traceclk_active = (live >> 5) & 1
    trace_lock = (live >> 4) & 1
    sys_lock = (live >> 3) & 1
    fsm = live & 0x3

    def rd_le(base, n):
        b = rd(s, base, n)
        return int.from_bytes(b, "little")

    rd_le(A_CYC, 4)  # kept for the side effect of latching the counter group
    first_code = rd_le(A_FIRST_CODE, 2)
    first_time = rd_le(A_FIRST_TIME, 4)
    first_ctx = rd8(s, A_FIRST_CTX)

    # ---- live state ----
    print(
        f"       sys MMCM lock={sys_lock}  trace MMCM lock={trace_lock}  "
        f"TRACECLK active={traceclk_active}"
    )
    print(f"       self-TX FSM = {FSM_NAMES.get(fsm, fsm)}  pkt_active={pkt_active}")
    if not sys_lock:
        print("[FAIL] system MMCM not locked — FPGA clocking broken")
    if not traceclk_active:
        print("[WARN] no TRACECLK activity — ETM not configured / no trace clock")
    elif not trace_lock:
        print("[WARN] trace MMCM not locked despite TRACECLK — wrong sampling freq")
    else:
        print("[OK]   TRACECLK active + trace MMCM locked")

    # ---- raw GPIO monitor (cross-check pin activity vs decode) ----
    glevel = rd8(s, A_GPIO_LEVEL)
    ge = rd(s, A_GPIO_EDGES, 10)
    gclk_ed = ge[0] | (ge[1] << 8)
    gd_ed = [ge[2 + 2 * i] | (ge[3 + 2 * i] << 8) for i in range(4)]
    clk_lvl = (glevel >> 4) & 1
    d_lvl = glevel & 0xF

    # edge counts are 16-bit saturating; 0xFFFF shown as ">=65535"
    def ec(v):
        return ">=65535" if v == 0xFFFF else str(v)

    print(
        f"       raw GPIO: TRACECLK level={clk_lvl} edges={ec(gclk_ed)}  "
        f"TRACED[3:0] level={d_lvl:04b} edges=[{','.join(ec(x) for x in gd_ed)}]"
    )
    # TRACECLK frequency meter: edges over a fixed window -> MHz
    fb = rd(s, A_FREQ, 3)
    freq_edges = fb[0] | (fb[1] << 8) | (fb[2] << 16)
    # edges = 2 * f * window  ->  f = edges / (2*window)
    f_mhz = freq_edges / (2 * FREQ_WINDOW_S) / 1e6
    print(
        f"       TRACECLK freq meter: {freq_edges} edges/16.78ms "
        f"=> ~{f_mhz:.2f} MHz at the pin"
    )
    # TRACECLK gap detector: gaps => the H7 TPIU stopping the clock between
    # bursts, which makes the capture MMCM lose lock (flapping).
    gb = rd(s, A_GAP, 4)
    gap_count = gb[0] | (gb[1] << 8)
    gap_max = gb[2] | (gb[3] << 8)
    gc = ">=65535" if gap_count == 0xFFFF else str(gap_count)
    gm_ns = gap_max * CLK_NS
    print(f"       TRACECLK gaps: count={gc}  longest={gap_max} clk ({gm_ns:.0f} ns)")
    if gap_count > 0:
        print(
            f"[FAIL] TRACECLK is DISCONTINUOUS ({gc} gaps, up to {gm_ns:.0f} ns): "
            f"the H7 TPIU stops the clock between trace bursts. The capture "
            f"MMCM loses lock on every gap (flapping) -> sample errors. This is "
            f"the root cause of the ~7-9% unknown, NOT a frequency mismatch."
        )
        print(
            "       => fix options: (a) keep TRACECLK continuous (TPIU "
            "continuous formatting / periodic sync), or (b) use a "
            "gap-tolerant capture front-end that re-locks fast / free-runs."
        )
    if gclk_ed == 0 and all(x == 0 for x in gd_ed):
        print(
            "[FAIL] raw trace pins are STATIC — no signal reaching the FPGA "
            "GPIO (check STM32 ETM pins / wiring / DAP config)"
        )
    elif gclk_ed == 0:
        print(
            "[WARN] TRACECLK pin not toggling but data lanes are — trace clock "
            "output / PE2 wiring problem"
        )
    else:
        active_lanes = [i for i, x in enumerate(gd_ed) if x > 0]
        print(
            f"[OK]   raw GPIO toggling: TRACECLK + TRACED lanes {active_lanes} "
            f"active at the pins (signal IS reaching the FPGA)"
        )
        if not trace_lock:
            print(
                "       => pins wiggle but trace MMCM not locked: this is a "
                "SAMPLING/FREQ issue (TRACECLK freq vs bitstream), not a "
                "'no signal' issue. Cross-check confirms the GPIO side is fine."
            )

    # ---- counters ----
    cnts = rd(s, A_CNT_BASE, len(CNT_NAMES))
    nonzero = [(CNT_NAMES[i], cnts[i]) for i in range(len(CNT_NAMES)) if cnts[i]]
    if nonzero:
        print(
            "       error counters: "
            + "  ".join(f"{n}={v}{'+ ' if v == 255 else ''}" for n, v in nonzero)
        )

    # ---- first (root-cause) error ----
    if have_first:
        # cyc runs at 125MHz
        t_ms = first_time / 125_000.0
        ctx_fsm = first_ctx & 0x3
        name = ERR_NAMES.get(first_code, f"unknown 0x{first_code:04x}")
        print(f"[FAIL] FIRST_ERR = 0x{first_code:04x} ({name})")
        print(
            f"       @ t={t_ms:.2f} ms (cyc={first_time}), "
            f"self-TX was in {FSM_NAMES.get(ctx_fsm, ctx_fsm)}"
        )
        # targeted advice
        if first_code == 0x0401:
            print(
                "       => self-TX/ARP deadlock (HANDOFF §7.1). The self-TX FSM "
                "waited >8ms for ARP resolve. Host must answer the FPGA's ARP, "
                "or the FSM needs the deadlock fix."
            )
        elif first_code == 0x0301:
            print(
                "       => ETM trace rate exceeds the 4-bit@TRACECLK drain "
                "(proposal 29). Lower CPU clock or raise TRACECLK."
            )
        elif first_code in (0x0101, 0x0102):
            print(
                "       => check STM32 ETM config / TRACECLK freq vs FPGA "
                "MMCM sampling frequency."
            )
        return 2
    print("[OK]   no sticky errors latched — link healthy")
    return 0
