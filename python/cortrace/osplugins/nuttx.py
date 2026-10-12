"""NuttX: the running task is the TCB pointer in g_running_tasks.

The offsets of `pid` and `entry` in struct tcb_s differ between trees (0x30 and
0x3c upstream, 0x34 and 0x44 in Vela), so they are read from the DWARF of the
firmware being traced.
"""

from . import OsPlugin, ThreadInfo, Watch

RUNNING = "g_running_tasks"


class NuttxPlugin(OsPlugin):
    """OS plugin for NuttX."""

    name = "nuttx"

    def detect(self, elf):
        return elf.symbol(RUNNING) is not None

    def watch(self, elf):
        return [Watch(RUNNING, elf.symbol(RUNNING))]

    def resolve(self, values, mem, elf):
        layout = elf.struct_layout("tcb_s")
        pid_off = layout["pid"][0]
        entry_off = layout["entry"][0]
        threads = {}
        for tcb in sorted(values):
            pid = mem.u32(tcb + pid_off)
            if pid is None:
                continue
            entry = mem.u32(tcb + entry_off)
            func = elf.function_at(entry) if entry else "?"
            threads[tcb] = ThreadInfo(pid, "" if func == "?" else func)
        return threads


PLUGIN = NuttxPlugin()
