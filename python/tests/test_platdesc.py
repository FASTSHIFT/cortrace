"""Platform files: lookup, validation."""

import pytest

from cortrace import platdesc

MINIMAL = """
[coresight]
etm = 0x1000
dwt = 0x2000
itm = 0x3000
cstf = 0x4000
etf = 0x5000
tpiu = 0x6000
tsgen = 0x7000
"""


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(platdesc, "USER_DIR", str(tmp_path / "no-user-dir"))
    monkeypatch.delenv(platdesc.SEARCH_ENV, raising=False)


def write(tmp_path, text, name="chip.toml"):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_the_bundled_platform_loads_by_name():
    plat = platdesc.load("stm32h743")
    assert plat["coresight"]["tpiu"] == 0x5C015000
    assert plat["trace"] == {"etm_trace_id": 2, "itm_trace_id": 1, "max_width": 4}
    assert plat["clocks"]["tsgen_hz"] == 75000000
    assert plat["on_trace_start"][0]["op"] == "rmw32"


def test_defaults_fill_in_the_optional_sections(tmp_path):
    plat = platdesc.load(write(tmp_path, MINIMAL))
    assert plat["trace"]["etm_trace_id"] == 2 and plat["trace"]["max_width"] == 4
    assert plat["clocks"] == {} and plat["on_trace_start"] == []


def test_a_search_path_directory_adds_and_overrides_platforms(monkeypatch, tmp_path):
    write(tmp_path, MINIMAL, "mychip.toml")
    write(tmp_path, MINIMAL.replace("0x6000", "0x9999"), "stm32h743.toml")
    monkeypatch.setenv(platdesc.SEARCH_ENV, str(tmp_path))
    names = platdesc.available()
    assert "mychip" in names and names["stm32h743"].startswith(str(tmp_path))
    assert platdesc.load("stm32h743")["coresight"]["tpiu"] == 0x9999


def test_unknown_platform_lists_the_choices():
    with pytest.raises(platdesc.PlatformError, match="unknown platform 'x'.*stm32h743"):
        platdesc.load("x")


def test_unreadable_and_malformed_files(tmp_path):
    with pytest.raises(platdesc.PlatformError, match="cannot read"):
        platdesc.load(str(tmp_path / "missing.toml"))
    with pytest.raises(platdesc.PlatformError, match="chip.toml"):
        platdesc.load(write(tmp_path, "this is = = not toml"))


def test_every_block_must_have_an_address(tmp_path):
    with pytest.raises(platdesc.PlatformError, match=r"lacks etf, tpiu"):
        platdesc.load(
            write(tmp_path, MINIMAL.replace("etf = 0x5000\ntpiu = 0x6000\n", ""))
        )


def test_steps_are_checked(tmp_path):
    bad_op = MINIMAL + '[[on_trace_start]]\nop = "frob"\n'
    with pytest.raises(platdesc.PlatformError, match="#1: unknown op 'frob'"):
        platdesc.load(write(tmp_path, bad_op))
    no_addr = MINIMAL + '[[on_trace_start]]\nop = "write32"\naddr = 4\n'
    with pytest.raises(platdesc.PlatformError, match=r"\(write32\) needs value"):
        platdesc.load(write(tmp_path, no_addr))
