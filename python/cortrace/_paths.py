"""Locate cortrace's helper binaries (installed package or source-tree build)."""

import os
import shutil

# <repo>/python/cortrace/_paths.py -> <repo>
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def find_binary(env_var, name, dev_dirs=("build-rel", "build")):
    """Resolve a helper binary: $ENV, then PATH (deb install), then the build
    directories of a source checkout. Returns the first existing candidate, or
    the bare name so the caller's error message names what is missing."""
    override = os.environ.get(env_var)
    if override:
        return override
    on_path = shutil.which(name)
    if on_path:
        return on_path
    for d in dev_dirs:
        cand = os.path.join(_REPO, d, name)
        if os.path.isfile(cand):
            return cand
    return name
