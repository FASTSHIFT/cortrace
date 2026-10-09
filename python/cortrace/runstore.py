"""Disk-space guards for captures and decodes.

A capture streams ~75 MB/s and the decoded Perfetto trace is ~5x the raw size,
so one second of trace costs ~450 MB and a minute would fill a small disk.
These checks run before anything is written and fail loudly instead of letting
a run stall half way. cortrace never deletes files on its own.
"""

import os
import shutil

GRAB_BYTES_PER_SEC = 75_000_000  # FPGA streamer payload rate at 150 MHz TRACECLK
EXPANSION = 5  # decoded Perfetto + side files per raw byte (measured 4.7x)
MAX_SECS = 5.0  # longer captures need --allow-long
HEADROOM = 1.1


def human(nbytes):
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1000
    return f"{value:.1f} TB"


def estimate_capture_bytes(secs):
    """Disk needed to capture `secs` seconds and decode them."""
    raw = secs * GRAB_BYTES_PER_SEC
    return int(raw * (1 + EXPANSION))


def estimate_decode_bytes(raw_size):
    """Disk needed to decode an existing raw capture of `raw_size` bytes."""
    return int(raw_size * EXPANSION)


def check_duration(secs, allow_long):
    if secs > MAX_SECS and not allow_long:
        raise SystemExit(
            f"--secs {secs:g} is longer than {MAX_SECS:g} s: that is about "
            f"{human(estimate_capture_bytes(secs))} of disk and a trace the "
            "Perfetto UI can hardly load. Pass --allow-long if you mean it."
        )


def free_bytes(path):
    """Free space on the filesystem that holds (or would hold) `path`."""
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def require_space(path, need, what):
    """Exit with a clear message if `path` cannot hold `need` more bytes."""
    free = free_bytes(path)
    if free < need * HEADROOM:
        raise SystemExit(
            f"not enough free space in {path} for {what}: need about "
            f"{human(need)}, only {human(free)} free. Free some space or pick "
            "another --out-dir."
        )
