"""Read what an OS plugin needs from the firmware ELF: symbols, struct layout
from the DWARF, and the initial memory image.

pyelftools is imported lazily so that the commands which never look at an ELF
(version, fpga, open...) work without it.
"""

import bisect


class ElfError(Exception):
    """The ELF cannot answer what was asked (no symbol, no debug info...)."""


DW_OP_PLUS_UCONST = 0x23


def member_offset(value):
    """Byte offset from DW_AT_data_member_location.

    DWARF 4 and later give a plain constant. DWARF 2 and 3 (older compilers, or
    -gdwarf-2) give a location expression, `DW_OP_plus_uconst <ULEB128>`.
    """
    if isinstance(value, int):
        return value
    ops = list(value)
    if not ops or ops[0] != DW_OP_PLUS_UCONST:
        raise ElfError(f"unsupported member location expression {ops}")
    offset = shift = 0
    for byte in ops[1:]:
        offset |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    return offset


class ElfInfo:
    """Symbols, struct member offsets and memory image of one firmware ELF."""

    def __init__(self, path):
        from elftools.elf.elffile import (
            ELFFile,
        )  # pylint: disable=import-outside-toplevel

        self.path = path
        self._file = open(path, "rb")  # pylint: disable=consider-using-with
        self._elf = ELFFile(self._file)
        self._symbols = None
        self._sizes = {}
        self._funcs = None
        self._segments = None
        self._dwarf = None
        self._layouts = {}

    def close(self):
        """Release the file."""
        self._file.close()

    # ---- symbols ----------------------------------------------------------
    def _load_symbols(self):
        symtab = self._elf.get_section_by_name(".symtab")
        if symtab is None:
            raise ElfError(f"{self.path}: no symbol table (stripped?)")
        self._symbols = {}
        self._sizes = {}
        funcs = []
        for sym in symtab.iter_symbols():
            if not sym.name or sym["st_shndx"] == "SHN_UNDEF":
                continue
            is_func = sym["st_info"]["type"] == "STT_FUNC"
            value = sym["st_value"] & ~1 if is_func else sym["st_value"]
            if sym.name not in self._symbols:
                self._symbols[sym.name] = value
                self._sizes[sym.name] = sym["st_size"]
            if is_func:
                funcs.append((value, sym.name))
        funcs.sort()
        self._funcs = ([a for a, _ in funcs], [n for _, n in funcs])

    def symbol(self, name):
        """Address of a symbol, or None."""
        if self._symbols is None:
            self._load_symbols()
        return self._symbols.get(name)

    def symbol_size(self, name):
        """st_size of a symbol (0 when it is unknown)."""
        if self._symbols is None:
            self._load_symbols()
        return self._sizes.get(name, 0)

    def function_at(self, addr):
        """Name of the function containing `addr` ('?' when there is none)."""
        if self._funcs is None:
            self._load_symbols()
        addrs, names = self._funcs
        i = bisect.bisect_right(addrs, addr & ~1) - 1
        return names[i] if i >= 0 else "?"

    # ---- memory image -----------------------------------------------------
    def read(self, addr, size):
        """Bytes of the initial image at `addr`, or None if no PT_LOAD segment
        with file contents covers all of them (.bss and heap are not there)."""
        if self._segments is None:
            self._segments = [
                (seg["p_vaddr"], seg["p_filesz"], seg["p_offset"])
                for seg in self._elf.iter_segments()
                if seg["p_type"] == "PT_LOAD" and seg["p_filesz"]
            ]
        for vaddr, filesz, offset in self._segments:
            if vaddr <= addr and addr + size <= vaddr + filesz:
                self._file.seek(offset + addr - vaddr)
                return self._file.read(size)
        return None

    # ---- DWARF ------------------------------------------------------------
    def struct_layout(self, struct_name):
        """{member: (offset, size)} of the complete `struct struct_name` (or a
        typedef'd struct of that name) from the DWARF.

        Raises ElfError when the ELF has no debug info or no such struct.
        """
        if struct_name in self._layouts:
            return self._layouts[struct_name]
        if self._dwarf is None:
            if not self._elf.has_dwarf_info():
                raise ElfError(
                    f"{self.path}: no DWARF debug info (build with -g) -- cannot "
                    f"read the layout of struct {struct_name}"
                )
            self._dwarf = self._elf.get_dwarf_info()
        for cu in self._dwarf.iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_structure_type":
                    continue
                name = die.attributes.get("DW_AT_name")
                if (
                    name is None
                    or name.value.decode() != struct_name
                    or "DW_AT_declaration" in die.attributes
                ):
                    continue
                layout = self._members(die)
                self._layouts[struct_name] = layout
                return layout
        raise ElfError(
            f"{self.path}: struct {struct_name} not found in the DWARF "
            "(was the firmware built with -g?)"
        )

    @staticmethod
    def _members(struct_die):
        layout = {}
        for child in struct_die.iter_children():
            if child.tag != "DW_TAG_member":
                continue
            name = child.attributes.get("DW_AT_name")
            loc = child.attributes.get("DW_AT_data_member_location")
            if name is None or loc is None:
                continue
            layout[name.value.decode()] = (member_offset(loc.value), _array_size(child))
        return layout


def _array_size(member_die):
    """Bytes of an array-of-char member (0 when it is not one)."""
    try:
        type_die = member_die.get_DIE_from_attribute("DW_AT_type")
    except KeyError:
        return 0
    if type_die.tag != "DW_TAG_array_type":
        return 0
    for sub in type_die.iter_children():
        if sub.tag == "DW_TAG_subrange_type":
            upper = sub.attributes.get("DW_AT_upper_bound")
            if upper is not None and isinstance(upper.value, int):
                return upper.value + 1
    return 0
