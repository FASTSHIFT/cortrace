"""Startup banner: the CORTRACE logo, version and tagline.

Printed to stderr (stdout stays clean for results that scripts read) and only
when stderr is an interactive terminal, so pipes, CI logs and the Perfetto
Record target's subprocesses never see it. Disable it with --no-banner or
CORTRACE_NO_BANNER=1.
"""

import os
import sys

from . import __version__

# figlet "rounded" font, pure ASCII.
LOGO = r"""
 _______ _______ ______ _______ ______  _______ _______ _______
(_______|_______|_____ (_______|_____ \(_______|_______|_______)
 _       _     _ _____) )  _    _____) )_______ _       _____
| |     | |   | |  __  /  | |  |  __  /|  ___  | |     |  ___)
| |_____| |___| | |  \ \  | |  | |  \ \| |   | | |_____| |_____
 \______)\_____/|_|   |_| |_|  |_|   |_|_|   |_|\______)_______)
"""

TAGLINE = "Cortex-M ETM/DWT/ITM trace -> Perfetto"


def banner_text():
    """The banner as a string (no trailing newline)."""
    return f"{LOGO.strip(chr(10))}\n\n  {TAGLINE}\n  cortrace v{__version__}"


def enabled(stream):
    """True if the banner may be shown on `stream`."""
    if os.environ.get("CORTRACE_NO_BANNER"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def show(stream=None):
    """Print the banner to `stream` (default stderr) if that is a terminal."""
    stream = stream or sys.stderr
    if enabled(stream):
        print(banner_text(), end="\n\n", file=stream)
