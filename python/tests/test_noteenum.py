"""Unit tests for noteenum.py (enumerator values from readelf -wi output)."""

import stat

from cortrace import align
from cortrace import noteenum as ne

# Two enum types: another one that also has an enumerator called NOTE_RESUME,
# then the one we want, with the names written in the three readelf styles.
READELF = """\
 <1><1c6b>: Abbrev Number: 37 (DW_TAG_enumeration_type)
    <1c6c>   DW_AT_name        : (indirect string, offset: 0x1): other_e
    <1c70>   DW_AT_encoding    : 7\t(unsigned)
 <2><1c7e>: Abbrev Number: 36 (DW_TAG_enumerator)
    <1c7f>   DW_AT_name        : (indirect string, offset: 0x2): NOTE_RESUME
    <1c83>   DW_AT_const_value : 99
 <1><1d00>: Abbrev Number: 12 (DW_TAG_structure_type)
    <1d01>   DW_AT_name        : (indirect string, offset: 0x3): note_type_e
 <1><1d10>: Abbrev Number: 37 (DW_TAG_enumeration_type)
    <1d11>   DW_AT_name        : note_type_e
    <1d12>   DW_AT_encoding    : 7\t(unsigned)
 <2><1d20>: Abbrev Number: 36 (DW_TAG_enumerator)
    <1d21>   DW_AT_name        : (indirect line string, offset: 0x4): NOTE_ALL
    <1d22>   DW_AT_const_value : 0
 <2><1d30>: Abbrev Number: 36 (DW_TAG_enumerator)
    <1d31>   DW_AT_name        : NOTE_SUSPEND
    <1d32>   DW_AT_const_value : 3
 <2><1d40>: Abbrev Number: 36 (DW_TAG_enumerator)
    <1d41>   DW_AT_name        : (indirect string, offset: 0x5): NOTE_RESUME
    <1d42>   DW_AT_const_value : 4\t(unsigned)
 <2><1d50>: Abbrev Number: 36 (DW_TAG_enumerator)
    <1d51>   DW_AT_name        : (indirect string, offset: 0x6): NOTE_HEX
    <1d52>   DW_AT_const_value : 0x10
"""


def test_scan_finds_the_value_in_the_named_enum_only():
    assert ne.scan(READELF.splitlines(), "note_type_e", "NOTE_RESUME") == 4
    assert ne.scan(READELF.splitlines(), "note_type_e", "NOTE_SUSPEND") == 3
    assert ne.scan(READELF.splitlines(), "note_type_e", "NOTE_HEX") == 16
    assert ne.scan(READELF.splitlines(), "other_e", "NOTE_RESUME") == 99


def test_scan_returns_none_for_unknown_names():
    assert ne.scan(READELF.splitlines(), "note_type_e", "NOTE_NONE") is None
    assert ne.scan(READELF.splitlines(), "no_such_e", "NOTE_RESUME") is None


def test_scan_ignores_a_value_it_cannot_read():
    lines = [
        " <1><1>: Abbrev Number: 1 (DW_TAG_enumeration_type)",
        "    <2>   DW_AT_name        : e",
        " <2><3>: Abbrev Number: 2 (DW_TAG_enumerator)",
        "    <4>   DW_AT_name        : X",
        "    <5>   DW_AT_const_value : junk",
    ]
    assert ne.scan(lines, "e", "X") is None


def test_readelf_for_follows_the_nm_prefix():
    assert ne.readelf_for("arm-none-eabi-nm") == "arm-none-eabi-readelf"
    assert ne.readelf_for("/opt/x/bin/arm-none-eabi-nm") == (
        "/opt/x/bin/arm-none-eabi-readelf"
    )
    assert ne.readelf_for("odd-tool") == "readelf"


def fake_readelf(tmp_path, text):
    script = tmp_path / "readelf"
    script.write_text("#!/bin/sh\ncat <<'EOF'\n" + text + "EOF\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_enum_value_runs_readelf(tmp_path):
    tool = fake_readelf(tmp_path, READELF)
    assert ne.enum_value("fw.elf", "note_type_e", "NOTE_RESUME", tool) == 4


def test_enum_value_is_none_without_readelf(tmp_path):
    missing = str(tmp_path / "nope")
    assert ne.enum_value("fw.elf", "note_type_e", "NOTE_RESUME", missing) is None


def test_resolve_resume_type_order(tmp_path, capsys):
    tool = fake_readelf(tmp_path, READELF)
    # an explicit value wins, then the ELF, then the upstream number
    assert align.resolve_resume_type(7, "fw.elf", tool) == 7
    assert align.resolve_resume_type(None, "fw.elf", tool) == 4
    assert align.resolve_resume_type(None, None, tool) == 3
    empty = fake_readelf(tmp_path, "")
    assert align.resolve_resume_type(None, "fw.elf", empty) == 3
    assert "NOTE_RESUME not found" in capsys.readouterr().err
