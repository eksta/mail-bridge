"""Filesystem locations that work both from source and from a frozen exe."""
import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory holding user files (config.json, .body_cache, bridge.log).

    - Frozen (PyInstaller exe): the folder containing the .exe, so data
      survives across runs (onefile's __file__ points to a temp dir).
    - Source checkout: the repository root.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
