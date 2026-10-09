"""Contract between the Python tools and the cortrace-decode binary.

fuse and capture build cortrace-decode command lines. If a flag is renamed or
dropped on the C++ side, nothing in the Python unit tests would notice; this
test runs the real binary's --help and checks every flag we pass exists. It is
skipped when no binary is available (the Python-only CI job) and runs in the
.deb build job and on developer machines with a build tree.
"""

import os
import subprocess

import pytest
from cortrace import capture, fuse
from cortrace._paths import find_binary

BINARY = find_binary("CORTRACE_DECODE", "cortrace-decode")

pytestmark = pytest.mark.skipif(
    not (os.path.isfile(BINARY) and os.access(BINARY, os.X_OK)),
    reason="cortrace-decode not built",
)


@pytest.fixture(scope="module")
def help_text():
    out = subprocess.run([BINARY, "--help"], capture_output=True, text=True, check=True)
    return out.stdout + out.stderr


def flags(cmd):
    return {tok for tok in cmd if tok.startswith("--")}


def fuse_command(tmp_path):
    a = fuse.parse_args(
        ["--raw", "r", "--elf", "e", "--out-dir", str(tmp_path), "--tcbmap", "m"]
        + ["--pynuttx", str(tmp_path)]
    )
    paths = {"notes_bin": "n", "hw": "h", "runs": "r"}
    return fuse.decode_command(a, paths, "syms.nm")


def capture_command(tmp_path, base):
    a, _ = capture.parse_args(
        ["--out-dir", str(tmp_path), "--elf", "e", "--raw-in", "r"]
        + ["--time-base", base, "--tsgen-hz", "75e6", "--tcbmap", "m", "--phase", "1,0"]
    )
    return capture.decode_command(a, "raw", "out.pf", "syms.nm")


def test_fuse_flags_exist(help_text, tmp_path):
    missing = [f for f in sorted(flags(fuse_command(tmp_path))) if f not in help_text]
    assert not missing, f"cortrace-decode no longer has: {missing}"


@pytest.mark.parametrize("base", ["cycle", "etm", "hybrid"])
def test_capture_flags_exist(help_text, tmp_path, base):
    cmd = capture_command(tmp_path, base)
    missing = [f for f in sorted(flags(cmd)) if f not in help_text]
    assert not missing, f"cortrace-decode no longer has: {missing}"


def test_the_notes_and_runs_outputs_are_still_offered(help_text):
    """fuse depends on these two outputs; their names are part of the contract."""
    for flag in ("--itm-note-out", "--nx-runs-out", "--itm-note-unwrap"):
        assert flag in help_text
