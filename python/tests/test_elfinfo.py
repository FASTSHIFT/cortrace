"""ElfInfo: symbols, memory image and DWARF struct layout."""

import pytest
from elffixture import FREERTOS_C, NUTTX_C, SMP_C, build

from cortrace.elfinfo import ElfError, ElfInfo, member_offset


@pytest.fixture(name="nuttx_elf", scope="module")
def nuttx_elf_fixture(tmp_path_factory):
    elf = ElfInfo(build(tmp_path_factory.mktemp("nx"), "nuttx", NUTTX_C, "-g"))
    yield elf
    elf.close()


def test_symbols_and_sizes(nuttx_elf):
    assert nuttx_elf.symbol("g_tcb") is not None
    assert nuttx_elf.symbol("nope") is None
    assert nuttx_elf.symbol_size("g_running_tasks") == 8
    assert nuttx_elf.symbol_size("nope") == 0


def test_function_at_names_the_containing_function(nuttx_elf):
    addr = nuttx_elf.symbol("task_main")
    assert nuttx_elf.function_at(addr) == "task_main"
    assert nuttx_elf.function_at(addr | 1) == "task_main"  # thumb bit ignored
    assert nuttx_elf.function_at(0) == "?"


def test_read_gives_initial_data_but_not_unmapped_memory(nuttx_elf):
    tcb = nuttx_elf.symbol("g_tcb")
    assert int.from_bytes(nuttx_elf.read(tcb + 48, 4), "little") == 5
    assert nuttx_elf.read(0x10, 4) is None
    assert nuttx_elf.read(tcb + 10_000_000, 4) is None


def test_struct_layout_gives_member_offsets_and_array_sizes(nuttx_elf):
    layout = nuttx_elf.struct_layout("tcb_s")
    assert layout["pid"][0] == 48
    assert layout["entry"][0] == 64
    assert layout["name"] == (68, 8)
    assert nuttx_elf.struct_layout("tcb_s") is layout  # cached


def test_unknown_struct_is_an_error(nuttx_elf):
    with pytest.raises(ElfError, match="not found"):
        nuttx_elf.struct_layout("no_such_struct")


def test_freertos_layout_reads_the_name_array(tmp_path):
    elf = ElfInfo(build(tmp_path, "rtos", FREERTOS_C, "-g"))
    layout = elf.struct_layout("tskTaskControlBlock")
    assert layout["pcTaskName"] == (8, 16)
    assert layout["uxTCBNumber"][0] == 24
    elf.close()


def test_dwarf2_member_locations_are_expressions(tmp_path):
    """-gdwarf-2 (the firmware's setting) stores DW_OP_plus_uconst, not a number."""
    elf = ElfInfo(build(tmp_path, "dw2", NUTTX_C, "-gdwarf-2"))
    layout = elf.struct_layout("tcb_s")
    assert layout["pid"][0] == 48 and layout["entry"][0] == 64
    elf.close()


def test_member_offset_decodes_constants_and_expressions():
    assert member_offset(52) == 52
    assert member_offset([0x23, 52]) == 52  # DW_OP_plus_uconst 52
    assert member_offset([0x23, 0xE4, 0x00]) == 100  # multi-byte ULEB128
    assert member_offset([0x23, 0x80, 0x02]) == 256
    with pytest.raises(ElfError, match="unsupported member location"):
        member_offset([0x91, 0x04])  # DW_OP_fbreg
    with pytest.raises(ElfError, match="unsupported member location"):
        member_offset([])


def test_declaration_only_structs_are_skipped(tmp_path):
    source = "struct tcb_s; struct tcb_s *p; void main(void) {}"
    elf = ElfInfo(build(tmp_path, "decl", source, "-g"))
    with pytest.raises(ElfError):
        elf.struct_layout("tcb_s")
    elf.close()


def test_no_debug_info_is_an_error(tmp_path, monkeypatch):
    elf = ElfInfo(build(tmp_path, "nodbg", NUTTX_C))
    with pytest.raises(ElfError, match="not found in the DWARF.*-g"):
        elf.struct_layout("tcb_s")
    monkeypatch.setattr(
        elf._elf, "has_dwarf_info", lambda: False
    )  # pylint: disable=protected-access
    elf._dwarf = None  # pylint: disable=protected-access
    with pytest.raises(ElfError, match="no DWARF debug info"):
        elf.struct_layout("another_struct")
    elf.close()


def test_stripped_elf_has_no_symbols(tmp_path):
    elf = ElfInfo(build(tmp_path, "stripped", SMP_C, "-s"))
    with pytest.raises(ElfError, match="no symbol table"):
        elf.symbol("main")
    elf.close()
