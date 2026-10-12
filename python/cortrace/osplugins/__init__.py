"""OS plugins: tell cortrace which variable holds the running task and what the
values written to it mean.

The decoder only sees writes of a pointer to a variable the DWT watches. An OS
plugin knows which variable that is (watch) and how to turn a captured pointer
into a thread (resolve). Everything else is OS independent.

A plugin is a module that defines `PLUGIN`, an instance of a subclass of
OsPlugin. Built-in plugins live in this package; more are picked up from the
directories in $CORTRACE_PLUGIN_PATH and from ~/.config/cortrace/plugins.
"""

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass

BUILTIN = ("nuttx", "freertos")
USER_DIR = os.path.join(os.path.expanduser("~"), ".config", "cortrace", "plugins")


class PluginError(Exception):
    """A plugin cannot be selected or cannot do what was asked."""


@dataclass(frozen=True)
class Watch:
    """A variable for the DWT to watch."""

    symbol: str
    addr: int
    size: int = 4


@dataclass(frozen=True)
class ThreadInfo:
    """What a pointer stands for: a small stable id and a display name."""

    tid: int
    name: str


class Memory:
    """Target memory for a plugin: live from the probe when there is one, else
    the initial image in the ELF (static data only)."""

    def __init__(self, elf, probe=None):
        self.elf = elf
        self.probe = probe

    def read(self, addr, size):
        """`size` bytes at `addr`, or None."""
        if self.probe is not None:
            data = self.probe.read_bytes(addr, size)
            if data is not None:
                return data
        return self.elf.read(addr, size)

    def u32(self, addr):
        """Little-endian 32-bit word at `addr`, or None."""
        data = self.read(addr, 4)
        return None if data is None else int.from_bytes(data, "little")

    def cstring(self, addr, size):
        """NUL-terminated text in a `size`-byte buffer at `addr`, or None."""
        data = self.read(addr, size)
        if data is None:
            return None
        return data.split(b"\0", 1)[0].decode("ascii", errors="replace")


class OsPlugin:
    """Base class of an OS plugin."""

    name = ""

    def detect(self, elf):
        """True if this ELF is built for the OS (checked by symbol)."""
        raise NotImplementedError

    def watch(self, elf):
        """The variables for the DWT to watch, as a list of Watch."""
        raise NotImplementedError

    def resolve(self, values, mem, elf):
        """Map each captured pointer in `values` to a ThreadInfo.

        Pointers that cannot be resolved are left out; the decoder then shows
        them as raw values.
        """
        raise NotImplementedError


def _load_file(path):
    name = "cortrace_plugin_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def available():
    """{name: plugin} of every plugin that can be found."""
    plugins = {}
    for name in BUILTIN:
        module = importlib.import_module(f"{__name__}.{name}")
        plugins[module.PLUGIN.name] = module.PLUGIN
    dirs = [
        d for d in os.environ.get("CORTRACE_PLUGIN_PATH", "").split(os.pathsep) if d
    ]
    for directory in dirs + [USER_DIR]:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if not entry.endswith(".py"):
                continue
            module = _load_file(os.path.join(directory, entry))
            plugin = getattr(module, "PLUGIN", None)
            if plugin is None:
                print(f"cortrace: {entry} defines no PLUGIN, skipped", file=sys.stderr)
                continue
            plugins[plugin.name] = plugin  # a user plugin overrides a built-in one
    return plugins


def select(name, elf):
    """The plugin for `name` ('auto' detects it from the ELF, 'none' selects no
    OS). Returns None when there is no plugin to use."""
    if name == "none":
        return None
    plugins = available()
    if name != "auto":
        if name not in plugins:
            known = ", ".join(sorted(plugins))
            raise PluginError(f"unknown OS plugin '{name}' (available: {known})")
        return plugins[name]
    found = [p for p in plugins.values() if p.detect(elf)]
    if len(found) > 1:
        names = ", ".join(sorted(p.name for p in found))
        raise PluginError(
            f"several OS plugins match this ELF ({names}); pass --os NAME"
        )
    return found[0] if found else None
