"""Tests for .githooks/commit-msg: scope is required for every type but docs."""

import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".githooks" / "commit-msg"


def run_hook(tmp_path, message):
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(message, encoding="utf-8")
    return subprocess.run(
        ["bash", str(HOOK), str(msg)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "subject",
    [
        "feat(perfetto): one-click open",
        "fix(decode): cap lister address space",
        "refactor(a/b.c_d-e)!: reshape api",
        "test(scripts): add cases",
        "chore(gitignore): ignore pycache",
        "style(fmt): clang-format",
        "perf(deframe): avoid copy",
        "docs: localise notes",
        "docs(zh): localise notes",
        "docs(zh)!: rewrite",
        'Revert "fix(decode): x"',
    ],
)
def test_accepts(tmp_path, subject):
    assert run_hook(tmp_path, subject + "\n").returncode == 0


@pytest.mark.parametrize(
    "subject",
    [
        "feat: one-click open",
        "fix: something",
        "refactor!: reshape",
        "test: add cases",
        "chore: tidy",
        "style: fmt",
        "perf: faster",
        "feat(): empty scope",
        "feat(Bad Scope): upper",
        "feat(x):",
        "feat(x) missing colon",
        "docs:",
        "wip: x",
        "random text",
    ],
)
def test_rejects(tmp_path, subject):
    r = run_hook(tmp_path, subject + "\n")
    assert r.returncode == 1
    assert "commit blocked" in r.stderr


def test_skips_comments_and_blank_lines(tmp_path):
    msg = "# comment\n\nfix(decode): real subject\n\nbody\n"
    assert run_hook(tmp_path, msg).returncode == 0


def test_empty_message_rejected(tmp_path):
    assert run_hook(tmp_path, "# only comment\n").returncode == 1
