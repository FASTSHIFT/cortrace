"""noteenum -- read an enumerator value from the DWARF of an ELF.

The note type numbers differ between NuttX trees (a tree that starts the
enum with NOTE_ALL numbers NOTE_RESUME 4, upstream numbers it 3), so cortrace
asks the firmware's own debug info instead of assuming one.
"""

import re
import subprocess

TAG_RE = re.compile(r"\((DW_TAG_\w+)\)\s*$")
AT_RE = re.compile(r"DW_AT_(name|const_value)\s*:\s*(.*)$")


def readelf_for(nm):
    """readelf next to `nm` (arm-none-eabi-nm -> arm-none-eabi-readelf)."""
    return nm[:-2] + "readelf" if nm.endswith("nm") else "readelf"


def _value(text):
    """The value of a DW_AT_const_value line: '4' or '4\t(unsigned)'."""
    try:
        return int(text.split()[0], 0)
    except (ValueError, IndexError):
        return None


def scan(lines, enum, name):
    """Value of enumerator `name` of enum type `enum` in `readelf -wi` output."""
    in_enum = False  # inside the enumeration type called `enum`
    named = False  # that type's own name has been seen
    wanted = False  # the current enumerator is `name`
    for line in lines:
        tag = TAG_RE.search(line)
        if tag:
            wanted = False
            if tag.group(1) == "DW_TAG_enumeration_type":
                in_enum, named = True, False
            elif tag.group(1) != "DW_TAG_enumerator":
                in_enum = False
            continue
        attr = AT_RE.search(line)
        if not attr or not in_enum:
            continue
        kind, text = attr.groups()
        if kind == "name":
            text = text.rsplit(":", 1)[-1].strip() if "(" in text else text.strip()
            if not named:
                named = True
                in_enum = text == enum
            else:
                wanted = text == name
        elif wanted:
            return _value(text)
    return None


def enum_value(elf, enum, name, readelf="arm-none-eabi-readelf"):
    """Value of `enum`'s enumerator `name` in `elf`, or None if it cannot be read."""
    try:
        proc = subprocess.Popen(
            [readelf, "-wi", elf],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
    except OSError:
        return None
    try:
        return scan(proc.stdout, enum, name)
    finally:
        proc.kill()
        proc.wait()
