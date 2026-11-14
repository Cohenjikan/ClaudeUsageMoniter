"""Filesystem locations shared by every module.

Runtime state (config.json, usage_cache.json, the rotating log) lives next to
the program so it is easy to find, delete, or reset:

  - running from source  -> next to app.py
  - frozen (PyInstaller) -> next to the .exe. `__file__` would point into the
    temporary _MEIxxxx extraction folder, which is deleted on exit, so it must
    never be used for anything that has to persist.
"""
from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory that holds persistent per-user runtime files."""
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# Claude Code's per-user directory (same location on Windows / macOS / Linux).
CLAUDE_DIR = Path.home() / ".claude"
