import os
import subprocess
import sys

import pytest
from cortrace import __version__, cli


def test_version_command_and_flags(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out == f"cortrace {__version__}\n"
    for flag in ("--version", "-V"):
        with pytest.raises(SystemExit) as e:
            cli.main([flag])
        assert e.value.code == 0
        assert capsys.readouterr().out == f"cortrace {__version__}\n"


def test_help_lists_every_command(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: cortrace" in out
    for name in cli.SUMMARIES:
        assert name in out


def test_help_command(capsys):
    assert cli.main(["help"]) == 0
    assert "usage: cortrace" in capsys.readouterr().out


def test_no_arguments_prints_help_and_fails(capsys):
    assert cli.main([]) == 2
    assert "usage: cortrace" in capsys.readouterr().out


def test_unknown_command_is_an_argparse_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["frobnicate"])
    assert e.value.code == 2
    assert "invalid choice: 'frobnicate'" in capsys.readouterr().err


def test_unknown_option_before_the_command_is_rejected(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--bogus"])
    assert e.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


def test_dispatch_passes_the_rest_through_untouched_and_in_order(monkeypatch):
    seen = {}
    monkeypatch.setitem(cli.COMMANDS, "fuse", lambda args: seen.update(args=args) or 7)
    rest = [
        "--raw",
        "x",
        "--elf",
        "e",
        "-x",
        "--out-dir",
        "d",
        "positional",
        "--tcbmap",
        "m",
    ]
    assert cli.main(["fuse"] + rest) == 7
    assert seen["args"] == rest


def test_command_options_that_look_like_abbreviations_are_not_swallowed(monkeypatch):
    """--no-b... must not be treated as --no-banner on the way through."""
    seen = []
    monkeypatch.setitem(cli.COMMANDS, "capture", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(cli.banner, "show", lambda: None)
    assert cli.main(["capture", "--no", "x", "--no-b"]) == 0
    assert seen == [["--no", "x", "--no-b"]]


def test_none_return_means_success(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "open", lambda args: None)
    assert cli.main(["open"]) == 0


def test_commands_own_help_is_reached(capsys):
    """-h after the command belongs to the command, not to cortrace."""
    with pytest.raises(SystemExit) as e:
        cli.main(["align", "--help"])
    assert e.value.code == 0
    assert "--hw-runs" in capsys.readouterr().out


def test_decode_execs_the_binary_with_the_arguments(monkeypatch):
    monkeypatch.setenv("CORTRACE_DECODE", "/opt/cortrace-decode")
    seen = {}
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: seen.update(p=path, a=argv))
    cli.main(["decode", "raw.bin", "syms.nm", "--raw", "--version"])
    assert seen == {
        "p": "/opt/cortrace-decode",
        "a": ["/opt/cortrace-decode", "raw.bin", "syms.nm", "--raw", "--version"],
    }


def test_decode_reports_a_missing_binary(monkeypatch, capsys):
    monkeypatch.setenv("CORTRACE_DECODE", "/nonexistent/cortrace-decode")
    assert cli.main(["decode"]) == 127
    assert "cannot run" in capsys.readouterr().err


def test_fpga_subtools(monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli, "_lazy", lambda mod: lambda args: seen.append((mod, args)) or 0
    )
    assert cli.main(["fpga", "ctrl", "--ip", "1.2.3.4", "set-width", "4"]) == 0
    assert cli.main(["fpga", "net"]) == 0
    assert cli.main(["fpga", "health", "1.2.3.4", "bb"]) == 0
    assert seen == [
        ("fpga.ctrl", ["--ip", "1.2.3.4", "set-width", "4"]),
        ("fpga.net", []),
        ("fpga.health", ["1.2.3.4", "bb"]),
    ]


@pytest.mark.parametrize("argv", [["fpga"], ["fpga", "bogus"]])
def test_fpga_needs_a_known_tool(argv, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "<tool>" in err or "invalid choice" in err


def test_python_dash_m_entry_point():
    pkg_root = os.path.dirname(os.path.dirname(cli.__file__))
    env = dict(os.environ, PYTHONPATH=pkg_root)
    out = subprocess.run(
        [sys.executable, "-m", "cortrace", "version"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == f"cortrace {__version__}\n"
