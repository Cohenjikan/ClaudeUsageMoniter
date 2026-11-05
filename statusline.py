"""Claude Code statusLine bridge: the PRIMARY, token-free quota source.

Since v2.1.80, Claude Code pipes a JSON payload to the user's statusLine
command on every status refresh. On Pro/Max accounts it includes:

    "rate_limits": {
        "five_hour": {"used_percentage": 33, "resets_at": 1767225600},
        "seven_day": {"used_percentage": 91, "resets_at": 1767600000}
    }

`statusline_bridge.py` (registered as the statusLine command) captures that
block into CAPTURE_PATH; the tray app watches the file. That means:

  - no OAuth token is read, refreshed, or sent anywhere;
  - no network request, so no rate limit — the numbers update every time
    Claude Code refreshes its status line;
  - it is a documented Claude Code feature, not an undocumented endpoint.

Limitation: the file only updates while a Claude Code session is running. The
app can fall back to a READ-ONLY poll of /api/oauth/usage to cover idle
periods and model-scoped buckets (see state.py).

This module is pure stdlib with no UI imports, so the bridge script (which runs
on every status refresh and must start fast) can import it cheaply.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import CLAUDE_DIR, FROZEN, app_dir

# Where the bridge writes the latest capture and where we remember a statusLine
# command the user had before we installed ours (so we can chain + restore it).
MONITOR_DIR = CLAUDE_DIR / "usage-monitor"
CAPTURE_PATH = MONITOR_DIR / "statusline.json"
CHAIN_PATH = MONITOR_DIR / "previous-statusline.json"
CLAUDE_SETTINGS_PATH = CLAUDE_DIR / "settings.json"

# Substrings that identify our own bridge in a statusLine command.
_BRIDGE_MARKERS = ("statusline_bridge", "cc-usage-statusline")
# Name of the console helper exe built next to the tray exe (see the .spec).
BRIDGE_EXE_NAME = "cc-usage-statusline.exe"

# Windows we know how to map onto the usage-endpoint body shape.
_KNOWN_WINDOWS = ("five_hour", "seven_day")


# ---------------------------------------------------------------------------
# Payload -> capture record -> usage-endpoint-shaped body
# ---------------------------------------------------------------------------

def _num(val: Any) -> float | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _iso_reset(val: Any) -> str:
    """Normalize a resets_at value (Unix seconds, Unix ms, or ISO string) to an
    ISO-8601 UTC string. '' if absent/unparseable."""
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return ""
        n = _num(s)
        if n is None:
            try:
                datetime.fromisoformat(s.replace("Z", "+00:00"))
                return s
            except ValueError:
                return ""
        val = n
    n = _num(val)
    if n is None or n <= 0:
        return ""
    if n > 1e12:  # milliseconds
        n /= 1000.0
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _window_pct(win: dict[str, Any]) -> float | None:
    for key in ("used_percentage", "utilization", "percent", "used_percent"):
        if key in win:
            return _num(win.get(key))
    return None


def extract_rate_limits(payload: Any) -> dict[str, Any] | None:
    """Return the payload's rate_limits dict if it carries at least one usable
    window, else None (API-key accounts, older Claude Code, first render)."""
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    for win in rl.values():
        if isinstance(win, dict) and _window_pct(win) is not None:
            return rl
    return None


def make_capture(payload: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """Reduce a statusLine payload to the small record we persist. Only the
    quota block plus a few harmless labels are kept — never the transcript
    path, cwd, or anything else from the payload."""
    rl = extract_rate_limits(payload)
    if rl is None:
        return None
    model = payload.get("model")
    model_name = model.get("display_name") if isinstance(model, dict) else None
    return {
        "captured_at": float(now if now is not None else time.time()),
        "rate_limits": rl,
        "claude_code_version": payload.get("version") if isinstance(payload.get("version"), str) else None,
        "model": model_name if isinstance(model_name, str) else None,
    }


def body_from_capture(record: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a capture record into the /api/oauth/usage body shape so the
    single parser `usage_api.snapshot_from_body` (and the on-disk cache) serve
    both sources identically.

    five_hour / seven_day map directly. Any OTHER window that carries a
    percentage is treated generically as a model-scoped weekly bucket, so if
    Claude Code ever adds per-model windows they show up with no code change.
    """
    rl = record.get("rate_limits") if isinstance(record, dict) else None
    if not isinstance(rl, dict):
        return None
    body: dict[str, Any] = {}
    limits: list[dict[str, Any]] = []
    for key, win in rl.items():
        if not isinstance(win, dict):
            continue
        pct = _window_pct(win)
        if pct is None:
            continue
        reset = _iso_reset(win.get("resets_at", win.get("reset_at")))
        if key in _KNOWN_WINDOWS:
            body[key] = {"utilization": pct, "resets_at": reset or None}
        else:
            name = win.get("display_name") or win.get("name") or key
            limits.append({
                "kind": "weekly_scoped",
                "scope": {"model": {"display_name": str(name)}},
                "percent": pct,
                "resets_at": reset or None,
            })
    if not body and not limits:
        return None
    if limits:
        body["limits"] = limits
    return body


# ---------------------------------------------------------------------------
# Capture file IO
# ---------------------------------------------------------------------------

def _atomic_write_json(path: Path, data: Any, *, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def write_capture(record: dict[str, Any], path: Path = CAPTURE_PATH) -> None:
    """Atomically replace the capture file (tmp + os.replace). Several Claude
    Code sessions may run the bridge at once; each writes a whole file, last
    writer wins, and a reader never sees a half-written one."""
    _atomic_write_json(path, record)


def read_capture(path: Path = CAPTURE_PATH) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or _num(data.get("captured_at")) is None:
        return None
    return data


# ---------------------------------------------------------------------------
# Installing / removing the bridge in ~/.claude/settings.json
# ---------------------------------------------------------------------------

def _fwd(p: Path | str) -> str:
    # Claude Code on Windows runs statusLine commands through Git Bash, where
    # backslashes in a double-quoted path are fragile. Forward slashes work in
    # bash, cmd and PowerShell alike.
    return str(p).replace("\\", "/")


def _console_python() -> str:
    """A console python (not pythonw: a GUI-subsystem interpreter can't be
    relied on to have usable stdin/stdout pipes)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.is_file():
            return str(cand)
    return str(exe)


def bridge_command() -> str:
    """The statusLine command string that runs our bridge."""
    if FROZEN:
        return f'"{_fwd(app_dir() / BRIDGE_EXE_NAME)}"'
    script = Path(__file__).resolve().with_name("statusline_bridge.py")
    return f'"{_fwd(_console_python())}" "{_fwd(script)}"'


def is_bridge_command(cmd: Any) -> bool:
    return isinstance(cmd, str) and any(m in cmd for m in _BRIDGE_MARKERS)


class SettingsFileError(RuntimeError):
    """~/.claude/settings.json exists but isn't valid JSON — refuse to touch it."""


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise SettingsFileError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SettingsFileError(f"{path} does not contain a JSON object")
    return data


def is_installed(settings_path: Path = CLAUDE_SETTINGS_PATH) -> bool:
    try:
        settings = _load_settings(settings_path)
    except (OSError, SettingsFileError):
        return False
    sl = settings.get("statusLine")
    return isinstance(sl, dict) and is_bridge_command(sl.get("command"))


def install(settings_path: Path = CLAUDE_SETTINGS_PATH,
            chain_path: Path = CHAIN_PATH,
            command: str | None = None) -> None:
    """Register the bridge as Claude Code's statusLine, preserving every other
    setting. A statusLine the user already had is saved to `chain_path`; the
    bridge keeps running it and prints its output, so their status line looks
    the same as before. Idempotent: re-installing just refreshes the command
    (e.g. after the app was moved)."""
    settings = _load_settings(settings_path)
    current = settings.get("statusLine")
    if isinstance(current, dict) and current and not is_bridge_command(current.get("command")):
        _atomic_write_json(chain_path, {"previous": current}, indent=2)
    new_sl: dict[str, Any] = {"type": "command", "command": command or bridge_command()}
    if isinstance(current, dict) and "padding" in current:
        new_sl["padding"] = current["padding"]
    settings["statusLine"] = new_sl
    if settings_path.exists():
        backup = settings_path.with_name(settings_path.name + ".bak-usage-monitor")
        if not backup.exists():
            try:
                backup.write_bytes(settings_path.read_bytes())
            except OSError:
                pass
    _atomic_write_json(settings_path, settings, indent=2)


def load_previous(chain_path: Path = CHAIN_PATH) -> dict[str, Any] | None:
    try:
        with open(chain_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    prev = data.get("previous") if isinstance(data, dict) else None
    if isinstance(prev, dict) and isinstance(prev.get("command"), str) \
            and not is_bridge_command(prev.get("command")):
        return prev
    return None


def uninstall(settings_path: Path = CLAUDE_SETTINGS_PATH,
              chain_path: Path = CHAIN_PATH) -> None:
    """Remove our bridge, restoring the user's previous statusLine if there
    was one. Leaves a statusLine that isn't ours untouched."""
    settings = _load_settings(settings_path)
    current = settings.get("statusLine")
    if isinstance(current, dict) and is_bridge_command(current.get("command")):
        prev = load_previous(chain_path)
        if prev is not None:
            settings["statusLine"] = prev
        else:
            settings.pop("statusLine", None)
        _atomic_write_json(settings_path, settings, indent=2)
    try:
        chain_path.unlink()
    except OSError:
        pass
