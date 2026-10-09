"""cortrace: Cortex-M parallel-trace (ETM/DWT/ITM) decode, fusion and capture tools."""

from pathlib import Path


def _source_tree_version():
    """VERSION file of a source checkout (<repo>/VERSION), marked as a dev build."""
    try:
        text = (Path(__file__).resolve().parents[2] / "VERSION").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "0+unknown"
    return f"{text.strip()}+dev"


try:
    # written by the build (cmake/Version.cmake) next to the installed package
    from ._version import __version__
except ImportError:  # running from a source tree that was not built/installed
    __version__ = _source_tree_version()
