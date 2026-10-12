"""Platform descriptions: where the CoreSight blocks of a chip are and what the
chip needs before they can trace.

A platform is a TOML file (see platforms/stm32h743.toml). The part every Armv7-M
chip shares -- how the blocks are armed -- lives in trace_arm.py; the file only
holds addresses, ids, default clocks and the few chip-specific register steps
(`[[on_trace_start]]`).
"""

import os
import sys

from ._paths import _REPO

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - python 3.10 (the deb depends on python3-tomli)
    import tomli as tomllib

SEARCH_ENV = "CORTRACE_PLATFORM_PATH"
USER_DIR = os.path.join(os.path.expanduser("~"), ".config", "cortrace", "platforms")
SHARE_DIR = "/usr/share/cortrace/platforms"
SOURCE_DIR = os.path.join(_REPO, "platforms")

BLOCKS = ("etm", "dwt", "itm", "cstf", "etf", "tpiu", "tsgen")
OPS = {"write32": ("addr", "value"), "rmw32": ("addr",), "poll32": ("addr", "mask")}


class PlatformError(Exception):
    """The platform file is missing or malformed."""


def search_dirs():
    """Directories searched for platform files, first match wins."""
    extra = [d for d in os.environ.get(SEARCH_ENV, "").split(os.pathsep) if d]
    return extra + [USER_DIR, SHARE_DIR, SOURCE_DIR]


def available():
    """{name: path} of every platform file found."""
    found = {}
    for directory in reversed(search_dirs()):
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".toml"):
                found[entry[: -len(".toml")]] = os.path.join(directory, entry)
    return found


def _check_ops(path, ops):
    for i, step in enumerate(ops, start=1):
        op = step.get("op")
        if op not in OPS:
            raise PlatformError(
                f"{path}: on_trace_start #{i}: unknown op {op!r} "
                f"(use {', '.join(OPS)})"
            )
        missing = [k for k in OPS[op] if k not in step]
        if missing:
            raise PlatformError(
                f"{path}: on_trace_start #{i} ({op}) needs {', '.join(missing)}"
            )


def load(name_or_path):
    """Load a platform by name or by path to its .toml file."""
    path = name_or_path
    if not name_or_path.endswith(".toml"):
        path = available().get(name_or_path)
        if path is None:
            known = ", ".join(sorted(available())) or "none found"
            raise PlatformError(
                f"unknown platform '{name_or_path}' (available: {known})"
            )
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as exc:
        raise PlatformError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise PlatformError(f"{path}: {exc}") from exc
    blocks = data.get("coresight", {})
    missing = [b for b in BLOCKS if b not in blocks]
    if missing:
        raise PlatformError(f"{path}: [coresight] lacks {', '.join(missing)}")
    trace = data.setdefault("trace", {})
    trace.setdefault("etm_trace_id", 2)
    trace.setdefault("itm_trace_id", 1)
    trace.setdefault("max_width", 4)
    data.setdefault("clocks", {})
    data.setdefault("on_trace_start", [])
    _check_ops(path, data["on_trace_start"])
    data["path"] = path
    return data
