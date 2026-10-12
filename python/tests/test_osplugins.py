"""OS plugins: selection, NuttX and FreeRTOS resolution, user plugins."""

import pytest
from elffixture import FREERTOS_C, NUTTX_C, SMP_C, FakeMemory, build

from cortrace import osplugins
from cortrace.elfinfo import ElfInfo
from cortrace.osplugins import (
    Memory,
    PluginError,
    ThreadInfo,
    freertos,
    nuttx,
    threadmap,
)


@pytest.fixture(name="nuttx_elf")
def nuttx_elf_fixture(tmp_path):
    elf = ElfInfo(build(tmp_path, "nuttx", NUTTX_C, "-g"))
    yield elf
    elf.close()


@pytest.fixture(name="rtos_elf")
def rtos_elf_fixture(tmp_path):
    elf = ElfInfo(build(tmp_path, "rtos", FREERTOS_C, "-g"))
    yield elf
    elf.close()


@pytest.fixture(autouse=True)
def isolate_user_plugins(monkeypatch, tmp_path):
    monkeypatch.setattr(osplugins, "USER_DIR", str(tmp_path / "no-plugins"))
    monkeypatch.delenv("CORTRACE_PLUGIN_PATH", raising=False)


# ---- selection --------------------------------------------------------------
def test_builtin_plugins_are_available():
    assert {"nuttx", "freertos"} <= set(osplugins.available())


def test_auto_picks_the_plugin_by_symbol(nuttx_elf, rtos_elf):
    assert osplugins.select("auto", nuttx_elf).name == "nuttx"
    assert osplugins.select("auto", rtos_elf).name == "freertos"


def test_auto_without_a_match_selects_nothing(tmp_path):
    elf = ElfInfo(build(tmp_path, "bare", "void main(void) {}", "-g"))
    assert osplugins.select("auto", elf) is None
    elf.close()


def test_none_selects_no_plugin(nuttx_elf):
    assert osplugins.select("none", nuttx_elf) is None


def test_unknown_name_lists_the_choices(nuttx_elf):
    with pytest.raises(PluginError, match="unknown OS plugin 'rtems'.*freertos"):
        osplugins.select("rtems", nuttx_elf)


def test_naming_a_plugin_skips_detection(rtos_elf):
    assert osplugins.select("nuttx", rtos_elf).name == "nuttx"


PLUGIN_SRC = """
from cortrace.osplugins import OsPlugin
class Greedy(OsPlugin):
    name = "{name}"
    def detect(self, elf):
        return True
PLUGIN = Greedy()
"""


def test_user_plugins_come_from_the_plugin_path(monkeypatch, tmp_path, nuttx_elf):
    plugdir = tmp_path / "plug"
    plugdir.mkdir()
    (plugdir / "greedy.py").write_text(PLUGIN_SRC.format(name="greedy"))
    (plugdir / "empty.py").write_text("X = 1\n")
    (plugdir / "notes.txt").write_text("not python")
    monkeypatch.setenv("CORTRACE_PLUGIN_PATH", str(plugdir))
    assert "greedy" in osplugins.available()
    with pytest.raises(PluginError, match="several OS plugins match.*greedy.*nuttx"):
        osplugins.select("auto", nuttx_elf)


def test_a_user_plugin_overrides_a_builtin_one(monkeypatch, tmp_path):
    plugdir = tmp_path / "plug"
    plugdir.mkdir()
    (plugdir / "mine.py").write_text(PLUGIN_SRC.format(name="nuttx"))
    monkeypatch.setenv("CORTRACE_PLUGIN_PATH", str(plugdir))
    assert type(osplugins.available()["nuttx"]).__name__ == "Greedy"


def test_the_base_class_leaves_the_work_to_plugins():
    base = osplugins.OsPlugin()
    for call in (
        lambda: base.detect(None),
        lambda: base.watch(None),
        lambda: base.resolve(set(), None, None),
    ):
        with pytest.raises(NotImplementedError):
            call()


# ---- Memory -----------------------------------------------------------------
class Probe:
    def __init__(self, data):
        self.data = data

    def read_bytes(self, addr, size):
        chunk = self.data.get(addr)
        return None if chunk is None else chunk[:size]


def test_memory_prefers_the_live_target_and_falls_back_to_the_image(nuttx_elf):
    tcb = nuttx_elf.symbol("g_tcb")
    live = Probe({tcb + 48: (9).to_bytes(4, "little")})
    assert Memory(nuttx_elf, live).u32(tcb + 48) == 9
    assert Memory(nuttx_elf, Probe({})).u32(tcb + 48) == 5  # image
    assert Memory(nuttx_elf).u32(0x10) is None


def test_memory_cstring_stops_at_the_nul(nuttx_elf):
    live = Probe({0x100: b"idle\0junk"})
    mem = Memory(nuttx_elf, live)
    assert mem.cstring(0x100, 9) == "idle"
    assert mem.cstring(0x200, 4) is None


# ---- NuttX ------------------------------------------------------------------
def test_nuttx_watches_g_running_tasks(nuttx_elf):
    plugin = nuttx.PLUGIN
    assert plugin.detect(nuttx_elf)
    (watch,) = plugin.watch(nuttx_elf)
    assert watch.symbol == "g_running_tasks"
    assert watch.addr == nuttx_elf.symbol("g_running_tasks")


def test_nuttx_names_threads_with_dwarf_offsets(nuttx_elf):
    entry = nuttx_elf.symbol("task_main") + 6
    mem = FakeMemory(
        {
            0x24000000 + 48: 7,
            0x24000000 + 64: entry,
            0x24000100 + 48: 0,  # idle: pid 0, no entry point
            0x24000100 + 64: 0,
            # 0x24000200 is not readable
        }
    )
    threads = nuttx.PLUGIN.resolve({0x24000000, 0x24000100, 0x24000200}, mem, nuttx_elf)
    assert threads == {
        0x24000000: ThreadInfo(7, "task_main"),
        0x24000100: ThreadInfo(0, ""),
    }


# ---- FreeRTOS ---------------------------------------------------------------
def test_freertos_watches_the_current_tcb(rtos_elf):
    plugin = freertos.PLUGIN
    assert plugin.detect(rtos_elf)
    (watch,) = plugin.watch(rtos_elf)
    assert (watch.symbol, watch.addr) == (
        "pxCurrentTCB",
        rtos_elf.symbol("pxCurrentTCB"),
    )


def test_freertos_smp_watches_one_variable_per_core(tmp_path):
    elf = ElfInfo(build(tmp_path, "smp", SMP_C, "-g"))
    assert freertos.PLUGIN.detect(elf)
    watches = freertos.PLUGIN.watch(elf)
    assert [w.symbol for w in watches] == ["pxCurrentTCBs[0]", "pxCurrentTCBs[1]"]
    assert watches[1].addr == watches[0].addr + 4
    elf.close()


def test_freertos_reads_names_from_the_tcb(rtos_elf):
    mem = FakeMemory(
        words={0x2000_0000 + 24: 3},
        strings={
            0x2000_0000 + 8: "worker",
            0x2000_1000 + 8: "IDLE",
            0x2000_2000 + 8: "",
        },
    )
    threads = freertos.PLUGIN.resolve(
        {0x2000_0000, 0x2000_1000, 0x2000_2000}, mem, rtos_elf
    )
    assert threads[0x2000_0000] == ThreadInfo(3, "worker")  # uxTCBNumber
    assert threads[0x2000_1000] == ThreadInfo(2, "IDLE")  # numbered by position
    assert 0x2000_2000 not in threads  # unreadable name: left to the decoder


def test_freertos_without_the_tcb_layout_explains_what_to_do(tmp_path):
    elf = ElfInfo(build(tmp_path, "nodbg", FREERTOS_C))
    with pytest.raises(PluginError, match="-g"):
        freertos.PLUGIN.resolve({1}, FakeMemory(), elf)
    elf.close()


def test_freertos_needs_the_name_member(tmp_path):
    source = "struct tskTaskControlBlock { int x; } t; void main(void) {}"
    elf = ElfInfo(build(tmp_path, "noname", source, "-g"))
    with pytest.raises(PluginError, match="pcTaskName"):
        freertos.PLUGIN.resolve({1}, FakeMemory(), elf)
    elf.close()


# ---- files ------------------------------------------------------------------
def test_values_file_roundtrip(tmp_path):
    path = tmp_path / "v.txt"
    path.write_text("# switches\n0x20000100\n\n0x20000200\n")
    assert threadmap.read_values(str(path)) == {0x20000100, 0x20000200}


def test_thread_map_roundtrip(tmp_path):
    path = str(tmp_path / "map.txt")
    threads = {0x20000200: ThreadInfo(2, "b"), 0x20000100: ThreadInfo(1, "")}
    threadmap.write_map(path, threads)
    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines[1].startswith("0x20000100\t1")  # sorted by pointer
    assert threadmap.read_map(path) == threads


def test_read_map_skips_comments_blanks_and_short_lines(tmp_path):
    path = tmp_path / "m.txt"
    path.write_text("# c\n\n0x10\n0x20\t4\tidle\n")
    assert threadmap.read_map(str(path)) == {0x20: ThreadInfo(4, "idle")}
