"""cortrace: Cortex-M parallel-trace (ETM/DWT/ITM) decode, fusion and capture tools."""

try:
    from ._version import __version__
except ImportError:  # running from a source tree that was not built/installed
    __version__ = "0.0.0+dev"
