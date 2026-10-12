import os
from cortrace import _paths


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("X_BIN", "/opt/custom/tool")
    assert _paths.find_binary("X_BIN", "tool") == "/opt/custom/tool"


def test_path_lookup(monkeypatch, tmp_path):
    exe = tmp_path / "mytool"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.delenv("X_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _paths.find_binary("X_BIN", "mytool") == str(exe)


def test_missing_binary_returns_bare_name(monkeypatch):
    monkeypatch.delenv("X_BIN", raising=False)
    monkeypatch.setenv("PATH", os.devnull)
    assert _paths.find_binary("X_BIN", "no-such-tool-xyz", dev_dirs=()) == (
        "no-such-tool-xyz"
    )


def test_development_build_directory_is_searched(monkeypatch, tmp_path):
    build = tmp_path / "build-rel"
    build.mkdir()
    tool = build / "devtool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.delenv("X_BIN", raising=False)
    monkeypatch.setenv("PATH", os.devnull)
    monkeypatch.setattr(_paths, "_REPO", str(tmp_path))
    assert _paths.find_binary("X_BIN", "devtool", dev_dirs=("build-rel",)) == str(tool)
