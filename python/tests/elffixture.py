"""Tiny ELFs with DWARF for the plugin tests, built with the host C compiler.

The layouts mimic what the plugins read: a NuttX-like `struct tcb_s` with `pid`
and `entry`, and a FreeRTOS-like `struct tskTaskControlBlock` with `pcTaskName`
and `uxTCBNumber`. Addresses are below 4 GiB (static, non-PIE) so they fit the
32-bit values the plugins handle.
"""

import shutil
import subprocess

import pytest

NUTTX_C = """
#include <stdint.h>
struct tcb_s {
  uint32_t pad[12];
  int32_t pid;
  uint32_t pad2[3];
  uint32_t entry;
  char name[8];
};
struct tcb_s g_tcb = { .pid = 5 };
struct tcb_s *g_running_tasks = &g_tcb;
void task_main(void) {}
void other_fn(void) {}
"""

FREERTOS_C = """
#include <stdint.h>
struct tskTaskControlBlock {
  uint32_t top;
  uint32_t prio;
  char pcTaskName[16];
  uint32_t uxTCBNumber;
};
struct tskTaskControlBlock g_tcb = { .pcTaskName = "worker", .uxTCBNumber = 3 };
struct tskTaskControlBlock *pxCurrentTCB = &g_tcb;
void main(void) {}
"""

SMP_C = """
struct tskTaskControlBlock { char pcTaskName[8]; };
unsigned int pxCurrentTCBs[2]; /* 32-bit pointers on the real target */
void main(void) {}
"""


def build(tmp_path, name, source, *flags):
    """Compile `source` into tmp_path/name, or skip if there is no compiler."""
    cc = shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        pytest.skip("no C compiler")
    src = tmp_path / f"{name}.c"
    src.write_text(source)
    out = tmp_path / name
    cmd = [cc, "-m64", "-static", "-no-pie", "-nostdlib", "-Wl,-e,main", "-o", str(out)]
    done = subprocess.run(
        cmd + list(flags) + [str(src)], capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        pytest.skip(f"cannot build test ELF: {done.stderr.strip()[:200]}")
    return str(out)


class FakeMemory:
    """Stand-in for osplugins.Memory: words and strings at given addresses."""

    def __init__(self, words=None, strings=None):
        self.words = words or {}
        self.strings = strings or {}

    def u32(self, addr):
        return self.words.get(addr)

    def cstring(self, addr, size):
        text = self.strings.get(addr)
        return None if text is None else text[:size]
