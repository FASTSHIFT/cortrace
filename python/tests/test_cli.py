import pytest
from cortrace import __version__, cli


def test_version(capsys):
    assert cli.main(["version"]) == 0
    assert capsys.readouterr().out == f"cortrace {__version__}\n"
    assert cli.main(["--version"]) == 0


def test_help_lists_every_command(capsys):
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in cli.COMMANDS:
        assert f"  {name} " in out


def test_no_arguments_prints_help_and_fails(capsys):
    assert cli.main([]) == 2
    assert "usage: cortrace" in capsys.readouterr().out


def test_unknown_command(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "unknown command 'frobnicate'" in capsys.readouterr().err


def test_dispatch_passes_remaining_arguments(monkeypatch):
    seen = {}
    monkeypatch.setitem(cli.COMMANDS, "fuse", lambda args: seen.update(args=args) or 7)
    assert cli.main(["fuse", "--raw", "x"]) == 7
    assert seen["args"] == ["--raw", "x"]


def test_none_return_means_success(monkeypatch):
    monkeypatch.setitem(cli.COMMANDS, "open", lambda args: None)
    assert cli.main(["open"]) == 0


def test_lazy_commands_import_the_module():
    with pytest.raises(SystemExit) as e:
        cli.main(["align", "--help"])
    assert e.value.code == 0


def test_decode_execs_the_binary_with_the_arguments(monkeypatch):
    monkeypatch.setenv("CORTRACE_DECODE", "/opt/cortrace-decode")
    seen = {}
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: seen.update(p=path, a=argv))
    cli.main(["decode", "raw.bin", "syms.nm", "--raw"])
    assert seen == {
        "p": "/opt/cortrace-decode",
        "a": ["/opt/cortrace-decode", "raw.bin", "syms.nm", "--raw"],
    }


def test_decode_reports_a_missing_binary(monkeypatch, capsys):
    monkeypatch.setenv("CORTRACE_DECODE", "/nonexistent/cortrace-decode")
    assert cli.main(["decode"]) == 127
    assert "cannot run" in capsys.readouterr().err


def test_fpga_subtools(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        cli, "_lazy", lambda mod: lambda args: seen.append((mod, args)) or 0
    )
    assert cli._fpga(["ctrl", "rearm"]) == 0
    assert cli._fpga(["net"]) == 0
    assert cli._fpga(["health", "1.2.3.4"]) == 0
    assert seen == [
        ("fpga.ctrl", ["rearm"]),
        ("fpga.net", []),
        ("fpga.health", ["1.2.3.4"]),
    ]
    assert cli._fpga([]) == 2
    assert cli._fpga(["bogus"]) == 2
    assert "usage: cortrace fpga" in capsys.readouterr().err


def test_python_dash_m_entry_point():
    import os
    import subprocess
    import sys

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
