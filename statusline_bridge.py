"""Claude Code statusLine command: capture rate_limits for the tray app.

Claude Code runs this on every status-line refresh and pipes a JSON payload to
stdin. We:

  1. save the payload's `rate_limits` block (and nothing sensitive) to
     ~/.claude/usage-monitor/statusline.json for the tray app to pick up;
  2. print a status line: the output of the user's previous statusLine command
     if they had one (we chain it, so installing us changes nothing visible),
     otherwise a compact "5h 33% · 7d 91%" readout.

It must never break Claude Code's status line: every failure is swallowed and
the script always exits 0.

Usage (written into ~/.claude/settings.json by the app's Settings window):
    "statusLine": {"type": "command",
                   "command": "\"C:/Python313/python.exe\" \"D:/Apps/cc-usage-tray/statusline_bridge.py\""}
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Make sibling modules importable regardless of the cwd Claude Code uses.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import statusline  # noqa: E402

CHAIN_TIMEOUT_SEC = 5


def _shell_argv(cmd: str) -> list[str] | None:
    """argv that runs `cmd` the way Claude Code would: through bash (Git Bash
    on Windows). None means 'fall back to the platform shell'."""
    if os.name != "nt":
        return ["/bin/sh", "-c", cmd]
    candidates = [
        os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    found = shutil.which("bash")
    # System32\bash.exe is the WSL launcher, not Git Bash — skip it.
    if found and "system32" not in found.lower():
        candidates.append(found)
    for c in candidates:
        if c and Path(c).is_file():
            return [c, "-c", cmd]
    return None


def run_chained(cmd: str, raw: bytes) -> str:
    argv = _shell_argv(cmd)
    kwargs = dict(input=raw, capture_output=True, timeout=CHAIN_TIMEOUT_SEC)
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        if argv is None:
            proc = subprocess.run(cmd, shell=True, **kwargs)
        else:
            proc = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", errors="replace").rstrip("\n")


def default_line(payload: dict) -> str:
    rl = statusline.extract_rate_limits(payload) or {}
    parts = []
    model = payload.get("model")
    if isinstance(model, dict) and isinstance(model.get("display_name"), str):
        parts.append(model["display_name"])
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        win = rl.get(key)
        pct = statusline._window_pct(win) if isinstance(win, dict) else None
        if pct is not None:
            parts.append(f"{label} {pct:.0f}%")
    return " · ".join(parts)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read() if sys.stdin else b""
    except (OSError, ValueError):
        raw = b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        record = statusline.make_capture(payload)
        if record is not None:
            statusline.write_capture(record)
    except Exception:  # noqa: BLE001 — never break the status line
        pass

    line = ""
    try:
        prev = statusline.load_previous()
        if prev is not None:
            line = run_chained(prev["command"], raw)
        else:
            line = default_line(payload)
    except Exception:  # noqa: BLE001
        line = ""
    try:
        if sys.stdout and line:
            # Raw UTF-8 bytes: Claude Code decodes UTF-8, while a Windows console
            # code page (cp1252/cp936) can't even encode the "·" separator.
            sys.stdout.buffer.write((line + "\n").encode("utf-8"))
            sys.stdout.flush()
    except (OSError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
