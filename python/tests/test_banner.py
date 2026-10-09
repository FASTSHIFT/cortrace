import io

import pytest
from cortrace import __version__, banner, cli


class Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def banner_allowed(monkeypatch):
    monkeypatch.delenv("CORTRACE_NO_BANNER", raising=False)


def test_text_has_the_logo_version_and_tagline():
    text = banner.banner_text()
    for line in banner.LOGO.strip("\n").split("\n"):
        assert line in text
    assert banner.TAGLINE in text and f"cortrace v{__version__}" in text


def test_logo_is_pure_ascii_and_fits_a_terminal():
    lines = banner.LOGO.strip("\n").split("\n")
    assert len(lines) == 6
    assert all(line.isascii() and len(line) <= 64 for line in lines)


def test_shown_on_a_terminal_only():
    tty = Tty()
    banner.show(tty)
    assert banner.banner_text() in tty.getvalue()
    pipe = io.StringIO()  # StringIO.isatty() is False
    banner.show(pipe)
    assert pipe.getvalue() == ""


def test_stream_without_isatty_is_treated_as_a_pipe():
    class Bare:
        pass

    assert banner.enabled(Bare()) is False


def test_environment_variable_silences_it(monkeypatch):
    monkeypatch.setenv("CORTRACE_NO_BANNER", "1")
    tty = Tty()
    banner.show(tty)
    assert tty.getvalue() == ""


def run_cli(monkeypatch, argv):
    """Run cli.main with every command stubbed; returns (exit code, banner shown)."""
    shown = []
    monkeypatch.setattr(cli.banner, "show", lambda: shown.append(True))
    for name in list(cli.COMMANDS):
        monkeypatch.setitem(cli.COMMANDS, name, lambda args: 0)
    monkeypatch.setattr(cli, "_lazy", lambda module: lambda args: 0)
    return cli.main(argv), bool(shown)


@pytest.mark.parametrize("cmd", ["capture", "serve", "fuse"])
def test_long_running_commands_show_it(monkeypatch, cmd):
    assert run_cli(monkeypatch, [cmd, "--elf", "x"]) == (0, True)


@pytest.mark.parametrize(
    "argv",
    [["align"], ["tcbmap"], ["open"], ["decode"], ["version"], ["fpga", "net"]],
)
def test_helper_commands_stay_quiet(monkeypatch, argv):
    assert run_cli(monkeypatch, argv)[1] is False


def test_help_shows_it(monkeypatch, capsys):
    assert run_cli(monkeypatch, ["--help"]) == (0, True)
    assert run_cli(monkeypatch, ["help"]) == (0, True)
    assert run_cli(monkeypatch, []) == (2, True)
    capsys.readouterr()


@pytest.mark.parametrize(
    "argv",
    [
        ["--no-banner", "capture", "--elf", "x"],  # before the command
        ["capture", "--no-banner", "--elf", "x"],  # after it
        ["--help", "--no-banner"],
        ["--no-banner"],
    ],
)
def test_no_banner_flag_hides_it_in_either_position(monkeypatch, capsys, argv):
    assert run_cli(monkeypatch, argv)[1] is False
    capsys.readouterr()


def test_no_banner_flag_is_not_passed_to_the_command(monkeypatch):
    seen = []
    monkeypatch.setattr(cli.banner, "show", lambda: None)
    monkeypatch.setitem(cli.COMMANDS, "capture", lambda args: seen.append(args) or 0)
    assert cli.main(["capture", "--no-banner", "--elf", "x"]) == 0
    assert cli.main(["--no-banner", "capture", "--elf", "y"]) == 0
    assert seen == [["--elf", "x"], ["--elf", "y"]]
