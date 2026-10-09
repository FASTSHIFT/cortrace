"""The VERSION file: format rules and how the package reports it."""

import re
from pathlib import Path

import pytest
import cortrace

REPO = Path(__file__).resolve().parents[2]
# PEP 440 subset, same rule as cmake/VersionUtil.cmake (without the +local part)
RELEASE_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?$"
)


def test_version_file_is_a_valid_release_version():
    text = (REPO / "VERSION").read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1  # exactly one line
    assert RELEASE_RE.match(text.strip()), f"bad VERSION: {text!r}"


def test_first_release_is_1_0_0():
    assert (REPO / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


@pytest.mark.skipif(
    hasattr(cortrace, "_version"), reason="an installed/built _version.py wins"
)
def test_source_tree_reports_the_version_file_as_a_dev_build():
    base = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    assert cortrace.__version__ == f"{base}+dev"


def test_missing_version_file_degrades_gracefully(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no file")

    monkeypatch.setattr(cortrace.Path, "read_text", boom)
    assert cortrace._source_tree_version() == "0+unknown"


@pytest.mark.parametrize(
    "version,ok",
    [
        ("1.0.0", True),
        ("1.0.0a1", True),
        ("1.0.0b2", True),
        ("1.0.0rc1", True),
        ("1.0.0.post1", True),
        ("1.0.0.dev3", True),
        ("1.0.0a1.dev2", True),
        ("1.0", False),
        ("v1.0.0", False),
        ("1.0.0-rc1", False),
        ("1.0.0a", False),
        (
            "1.0.0+git1.abc",
            False,
        ),  # the +local label is added by the build, never stored
    ],
)
def test_release_version_rule(version, ok):
    assert bool(RELEASE_RE.match(version)) is ok
