#!/usr/bin/env python3
"""align_check.py -- do the NuttX note trace and the hardware (ETM+DWT) trace
agree on WHEN each thread switch happened?

Inputs (both recorded over the same window, see nxtrace `--companion-cmd`):
  --hw    Perfetto file written by cortrace-decode (has a "Threads" track whose
          slices are thread runs; a run's start is a context switch into it).
  --note  text from `python -m nxtrace capture --format dump ...`, lines like
          "[22079159426] cpu=0 pid=2 type=3" (type 3 = NOTE_RESUME = switch in).

Both clocks count CPU cycles (note: DWT_CYCCNT; hw: ETM cycle count), so they
should differ only by a constant origin. For every contiguous burst of note
switches this finds the origin offset that makes the most switches coincide
with a hardware switch of the same thread, then reports how well they agree.
The note stream is allowed to be bursty (RTT over a slow probe drops notes
when its ring is full), hence the per-burst fit.

  offset  = note_ns - hw_ns for the same switch (constant if clocks are locked)
  resid   = per-switch scatter around that burst's offset (jitter)
  drift   = slope of offset over time across bursts (clock-rate mismatch, ppm)
"""

import argparse
import importlib.util
import os
import re
import statistics
import sys


def default_pb2():
    """perfetto_trace_pb2.py inside the pynuttx checkout named by $PYNUTTX."""
    root = os.environ.get("PYNUTTX")
    return os.path.join(root, "nxtrace", "perfetto_trace_pb2.py") if root else None


def load_pb2(path):
    spec = importlib.util.spec_from_file_location("perfetto_trace_pb2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_tcbmap(path):
    """name -> pid, from nx_tcbmap.py output ('0xTCB<TAB>pid<TAB>name')."""
    m = {}
    if not path:
        return m
    with open(path, encoding="utf-8") as f:
        for ln in f:
            if ln.startswith("#") or not ln.strip():
                continue
            parts = ln.rstrip("\n").split("\t")
            if len(parts) >= 3:
                m[parts[2]] = int(parts[1])
    return m


def hw_switches_tsv(path, name2pid):
    """[(ns, pid)] from cortrace-decode --nx-runs-out ('<tick>\\t<name>' per run)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            tick, _, name = ln.rstrip("\n").partition("\t")
            m = re.search(r"\(pid (\d+)\)", name)
            out.append((int(tick), int(m.group(1)) if m else name2pid.get(name, name)))
    out.sort(key=lambda x: x[0])
    return out


def hw_switches(perfetto_path, pb2, name2pid, track_name="Threads"):
    """[(ns, pid_or_name)] thread-run starts from the cortrace 'Threads' track."""
    tr = pb2.Trace()
    tr.ParseFromString(open(perfetto_path, "rb").read())
    uuids = {}
    for p in tr.packet:
        if p.HasField("track_descriptor"):
            td = p.track_descriptor
            uuids[td.uuid] = td.name
    want = {u for u, n in uuids.items() if n == track_name}
    if not want:
        sys.exit(
            f"no '{track_name}' track in {perfetto_path}; tracks: "
            f"{sorted(set(uuids.values()))[:12]}"
        )
    out = []
    for p in tr.packet:
        if not p.HasField("track_event"):
            continue
        te = p.track_event
        if te.track_uuid in want and te.type == te.TYPE_SLICE_BEGIN:
            # cortrace names runs "<thread> (pid N)"; fall back to the tcbmap.
            m = re.search(r"\(pid (\d+)\)", te.name)
            pid = int(m.group(1)) if m else name2pid.get(te.name, te.name)
            out.append((p.timestamp, pid))
    out.sort(key=lambda x: x[0])
    return out


NOTE_RE = re.compile(r"\[(\d+)\]\s+cpu=(\d+)\s+pid=(\d+)\s+type=(\d+)")


def note_switches(path, resume_type):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = NOTE_RE.match(ln)
            if m and int(m.group(4)) == resume_type:
                out.append((int(m.group(1)), int(m.group(3))))
    return out


def sequence_pair(hw, notes):
    """(offset, [(hw_t, resid)]) pairing the k-th hw switch with the k-th note switch,
    or (None, []) if the two sequences differ (lost switches / extra threads)."""
    if len(hw) != len(notes) or any(h[1] != n[1] for h, n in zip(hw, notes)):
        return None, []
    diffs = [n[0] - h[0] for h, n in zip(hw, notes)]
    off = int(statistics.median(diffs))
    return off, [(h[0], d - off) for h, d in zip(hw, diffs)]


def global_fit(hw, notes, tol_ns):
    """One constant offset (note - hw) that makes the most hw switches coincide
    with a note switch of the same thread. Returns (offset, [(hw_t, resid)])."""
    import bisect

    by_pid = {}
    for t, p in notes:
        by_pid.setdefault(p, []).append(t)

    def match(off, sw=hw):
        res = []
        for t, p in sw:
            ts = by_pid.get(p)
            if not ts:
                continue
            target = t + off
            i = bisect.bisect_left(ts, target)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(ts):
                    d = ts[j] - target
                    if best is None or abs(d) < abs(best):
                        best = d
            if best is not None and abs(best) <= tol_ns:
                res.append((t, best))
        return res

    # Candidate origins: align the first hw switch with every same-thread note,
    # scored on a ~64-switch sample so long captures stay fast; the top few are
    # then scored on everything.
    t0, p0 = hw[0]
    cands = [tn - t0 for tn in by_pid.get(p0, [])]
    sample = hw[:: max(1, len(hw) // 64)]
    scored = sorted(((len(match(o, sample)), o) for o in cands), reverse=True)[:5]
    best_off, best_res = None, []
    for _, off in scored:
        r = match(off)
        if len(r) > len(best_res):
            best_off, best_res = off, r
    # Refine: the matched residuals are relative to the candidate; fold them in.
    if best_res:
        best_off += int(statistics.median(d for _, d in best_res))
        best_res = match(best_off)
    return best_off, best_res


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--hw", help="cortrace Perfetto file (slow for big captures)")
    ap.add_argument(
        "--hw-runs", help="cortrace-decode --nx-runs-out TSV (fast; preferred)"
    )
    ap.add_argument("--note", required=True, help="nxtrace --format dump text")
    ap.add_argument(
        "--tcbmap", default=None, help="nx_tcbmap.py output (thread name -> pid)"
    )
    ap.add_argument(
        "--pb2",
        default=default_pb2(),
        help="path to pynuttx nxtrace/perfetto_trace_pb2.py (only for --hw; "
        "default: $PYNUTTX/nxtrace/perfetto_trace_pb2.py)",
    )
    ap.add_argument(
        "--resume-type", type=int, default=3, help="note type of NOTE_RESUME"
    )
    ap.add_argument(
        "--offset-out", help="write the fitted offset (note_ns - hw_ns, integer) here"
    )
    ap.add_argument(
        "--tol-us",
        type=float,
        default=30.0,
        help="match tolerance around the fitted offset",
    )
    a = ap.parse_args()

    name2pid = load_tcbmap(a.tcbmap)
    if a.hw_runs:
        hw = hw_switches_tsv(a.hw_runs, name2pid)
    elif a.hw:
        if not a.pb2:
            sys.exit("--hw needs --pb2 (or $PYNUTTX pointing at the pynuttx checkout)")
        hw = hw_switches(a.hw, load_pb2(a.pb2), name2pid)
    else:
        sys.exit("give --hw-runs or --hw")
    notes = note_switches(a.note, a.resume_type)
    print(f"hardware switches: {len(hw)}   note switches: {len(notes)}")
    if not hw or not notes:
        sys.exit("nothing to compare")
    print(
        f"hw span   : {(hw[-1][0]-hw[0][0])/1e6:.3f} ms ; pids {sorted({str(p) for _,p in hw})}"
    )
    print(f"note span : {(notes[-1][0]-notes[0][0])/1e6:.3f} ms")

    # Both traces come from ONE capture, so when they hold the same switches in the
    # same order (same count, same thread sequence) pair them one-to-one: exact and
    # immune to the aliasing a periodic workload causes in the nearest-neighbour fit.
    off, res = sequence_pair(hw, notes)
    if off is None:
        off, res = global_fit(hw, notes, a.tol_us * 1000)
    else:
        print("pairing        : one-to-one by order (identical thread sequence)")
    if off is None or not res:
        print(
            "no common offset found -- the two traces do not overlap in a way that matches"
        )
        return 1
    if a.offset_out:
        with open(a.offset_out, "w", encoding="utf-8") as f:
            f.write(str(int(off)))
    ds = [d for _, d in res]
    ts = [t for t, _ in res]
    n = len(res)
    mt, md = statistics.mean(ts), statistics.mean(ds)
    den = sum((t - mt) ** 2 for t in ts)
    slope = sum((t - mt) * (d - md) for t, d in zip(ts, ds)) / den if den else 0.0
    print()
    print(
        f"matched        : {n}/{len(hw)} hw switches within "
        f"+/-{a.tol_us:.0f} us of one common offset"
    )
    print(f"offset         : note = hw + {off/1e6:.6f} ms")
    print(
        f"residual       : mean {md/1e3:+.3f} us  sd {statistics.pstdev(ds)/1e3:.3f} us  "
        f"min {min(ds)/1e3:+.3f}  max {max(ds)/1e3:+.3f}"
    )
    print(f"drift          : {slope*1e6:+.2f} ppm over {(ts[-1]-ts[0])/1e6:.1f} ms")
    miss = len(hw) - n
    if miss:
        print(
            f"unmatched hw   : {miss}  (switch absent from the note stream, or outside tolerance)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
