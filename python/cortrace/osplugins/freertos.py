"""FreeRTOS: the running task is the TCB pointer in pxCurrentTCB.

The task name is an array inside the TCB (pcTaskName). TCBs live in .bss or on
the heap, so the names are only available from the live target: pass the probe
(`--probe-telnet`) when resolving.
"""

from ..elfinfo import ElfError
from . import OsPlugin, PluginError, ThreadInfo, Watch

CURRENT = "pxCurrentTCB"  # single core
CURRENT_SMP = "pxCurrentTCBs"  # one entry per core (SMP builds)
STRUCT_TAGS = ("tskTaskControlBlock", "tskTCB")


class FreertosPlugin(OsPlugin):
    """OS plugin for FreeRTOS."""

    name = "freertos"

    def detect(self, elf):
        return elf.symbol(CURRENT) is not None or elf.symbol(CURRENT_SMP) is not None

    def watch(self, elf):
        addr = elf.symbol(CURRENT)
        if addr is not None:
            return [Watch(CURRENT, addr)]
        base = elf.symbol(CURRENT_SMP)
        if base is None:
            raise PluginError(f"this ELF defines neither {CURRENT} nor {CURRENT_SMP}")
        cores = max(1, elf.symbol_size(CURRENT_SMP) // 4)
        return [Watch(f"{CURRENT_SMP}[{i}]", base + 4 * i) for i in range(cores)]

    def _layout(self, elf):
        for tag in STRUCT_TAGS:
            try:
                return elf.struct_layout(tag)
            except ElfError:
                continue
        raise PluginError(
            "FreeRTOS TCB layout not found in the DWARF (struct "
            f"{' / '.join(STRUCT_TAGS)}); build tasks.c with -g"
        )

    def resolve(self, values, mem, elf):
        layout = self._layout(elf)
        if "pcTaskName" not in layout:
            raise PluginError("FreeRTOS TCB has no pcTaskName member")
        name_off, name_len = layout["pcTaskName"]
        number_off = layout["uxTCBNumber"][0] if "uxTCBNumber" in layout else None
        threads = {}
        for seq, tcb in enumerate(sorted(values), start=1):
            name = mem.cstring(tcb + name_off, name_len or 16)
            if not name:
                continue
            number = mem.u32(tcb + number_off) if number_off is not None else None
            threads[tcb] = ThreadInfo(number or seq, name)
        return threads


PLUGIN = FreertosPlugin()
