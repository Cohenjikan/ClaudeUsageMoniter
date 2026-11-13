"""Claude Usage Tray + Floating Window.

Launch with `python app.py`. Tray icon is always present; the window can be shown,
hidden, or set to always-on-top via the tray menu (or by closing/showing it).
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from ctypes import wintypes
from pathlib import Path

# winotify spawns powershell.exe to deliver toast notifications. If we were launched
# from a shell whose PATH doesn't include the System32 PowerShell dir (happens with
# Git Bash, MSYS, and our dev harness), the spawn fails with WinError 2. Prepend the
# standard location so toasts work regardless of how we were launched.
_PS_DIR = r"C:\Windows\System32\WindowsPowerShell\v1.0"
if os.path.isdir(_PS_DIR) and _PS_DIR not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _PS_DIR + os.pathsep + os.environ.get("PATH", "")

import pystray
from PIL import Image, ImageDraw, ImageFont
from winotify import Notification, audio

from jsonl_costs import Aggregate
from state import Orchestrator
from usage_api import UsageSnapshot
import queue
import strip_render
from taskbar_widget import NativeTaskbarWidget, taskbar_is_light

__version__ = "0.4.0"

# Project home page — shown/opened from the Settings → General tab.
PROJECT_URL = "https://github.com/Cohenjikan/ClaudeUsageMoniter"

# ---- i18n ----
# Module-level current-language state. set_app_language() rebinds it; t() looks
# it up. Menu items use callable `text=` so they re-evaluate t() every time the
# menu is shown, meaning a language switch takes effect on next menu open with
# no menu rebuild. Strip text picks up the new language on its next render tick.
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {
        # ---- Tray + strip menus ----
        "show_window": "Show window",
        "refresh_now": "Refresh now",
        "settings": "Settings…",
        "show_strip": "Show taskbar strip",
        "hide_strip": "Hide taskbar strip",
        "display_mode": "Display mode",
        "mode_1": "Compact (quota only)",
        "mode_2": "+ Time remaining",
        "mode_3": "+ Time-remaining %",
        "mode_4": "+ Time-elapsed %",
        "quit": "Quit",
        # ---- Short labels (shared strip + window; intentionally untranslated) ----
        "5h": "5h",
        "7d": "7d",
        "today": "today",
        # ---- Floating window section headers / labels ----
        "win_title": "Claude Usage",
        "lbl_5h_window": "5h window",
        "lbl_weekly": "Weekly",
        "lbl_session": "Session",
        "lbl_today": "Today",
        "lbl_this_month": "This month",
        "projects_header": "Top projects (this month)",
        "cost_caption": "Equivalent API cost — Claude Code only",
        "loading": "loading…",
        "idle_full": "idle (full quota)",
        # Model-scoped weekly buckets (Fable, …) — {name} is the server-provided
        # model display name, deliberately untranslated.
        "lbl_model_weekly": "{name} weekly",
        # Captions with runtime values are built via helpers, but the static
        # fragments live here so they translate.
        "resets_at": "resets at {time} · {left} left",  # {time}=HH:MM, {left}=2h 13m
        "updated_ago": "updated {age} ago",
        # ---- Staleness / token-expired state ----
        "stale_mark": "stale",                            # short strip marker
        "stale_token": "Sign-in refresh failed - open Claude Code if this persists",
        "stale_generic": "Quota data stale · {age} old",
        # ---- Settings window ----
        "settings_title": "Settings",
        "tab_general": "General",
        "tab_strip": "Strip",
        "tab_about": "About",
        "language": "Language",
        "lang_en": "English",
        "lang_zh": "中文",
        "run_at_startup": "Run at startup",
        "version_label": "Version",
        "project_page": "Project page",
        "strip_show": "Show taskbar strip",
        "strip_opaque_bg": "Opaque background",
        "strip_screen_pos": "Screen position",
        "pos_left": "Left",
        "pos_right": "Right",
        "strip_display_mode": "Display mode",
        "strip_drag_toggle": "Drag mode",
        "strip_drag_on": "Drag mode: ON (drag the strip)",
        "strip_drag_off": "Drag mode: OFF",
        "strip_reset_pos": "Reset position",
        "strip_style": "Style",
        "style_bars": "Progress bars",
        "style_rings": "Rings",
        "style_classic": "Classic text",
        "classic_only": "Classic style only:",
        "resets_in_short": "resets in {left}",
        "idle_short": "idle",
        "about_blurb": (
            "5h / 7d % are server-side and include ALL usage — chat plus code.\n\n"
            "$ values come only from local Claude Code transcripts: an equivalent "
            "API value, excluding chat.\n\n"
            "Polling cadence: 6 min for the API, 30 s for local JSONL.\n\n"
            "MIT license."
        ),
        # ---- Toast notifications ----
        "toast_5h_window": "5-hour window",
        "toast_weekly": "Weekly quota",
        "toast_model_weekly": "{name} weekly quota",
        "toast_title": "Claude {window} at {pct}%",
        "toast_body_default": "Heads up — you may want to slow down or switch projects.",
        "toast_body_90": "Approaching the limit. Plan accordingly.",
        "toast_body_95": "Very close to the limit. Stop or you'll get rate-limited.",
    },
    "zh": {
        # ---- Tray + strip menus ----
        "show_window": "显示窗口",
        "refresh_now": "立即刷新",
        "settings": "设置…",
        "show_strip": "显示状态条",
        "hide_strip": "隐藏状态条",
        "display_mode": "显示模式",
        "mode_1": "简洁 (仅额度)",
        "mode_2": "+ 剩余时间",
        "mode_3": "+ 剩余时间百分比",
        "mode_4": "+ 已用时间百分比",
        "quit": "退出",
        # ---- Short labels ----
        "5h": "5h",
        "7d": "7d",
        "today": "今日",
        # ---- Floating window section headers / labels ----
        "win_title": "Claude 用量",
        "lbl_5h_window": "5h 窗口",
        "lbl_weekly": "周配额",
        "lbl_session": "本次会话",
        "lbl_today": "今日",
        "lbl_this_month": "本月",
        "projects_header": "本月项目 Top 6",
        "cost_caption": "等效 API 成本（仅 Claude Code）",
        "loading": "加载中…",
        "idle_full": "空闲 · 满额可用",
        "lbl_model_weekly": "{name} 周配额",
        "resets_at": "{time} 重置 · 剩 {left}",
        "updated_ago": "{age}前更新",
        # ---- Staleness / token-expired state ----
        "stale_mark": "数据旧",
        "stale_token": "登录刷新失败 · 若持续请打开 Claude Code",
        "stale_generic": "配额数据已过期 · {age}前",
        # ---- Settings window ----
        "settings_title": "设置",
        "tab_general": "常规",
        "tab_strip": "状态条",
        "tab_about": "关于",
        "language": "语言",
        "lang_en": "English",
        "lang_zh": "中文",
        "run_at_startup": "开机自启",
        "version_label": "版本",
        "project_page": "项目主页",
        "strip_show": "显示状态条",
        "strip_opaque_bg": "不透明背景",
        "strip_screen_pos": "屏幕位置",
        "pos_left": "左",
        "pos_right": "右",
        "strip_display_mode": "显示模式",
        "strip_drag_toggle": "拖动模式",
        "strip_drag_on": "拖动模式：开（拖动状态条）",
        "strip_drag_off": "拖动模式：关",
        "strip_reset_pos": "重置位置",
        "strip_style": "样式",
        "style_bars": "进度条",
        "style_rings": "圆环",
        "style_classic": "经典文字",
        "classic_only": "仅经典样式有效：",
        "resets_in_short": "{left}后重置",
        "idle_short": "空闲",
        "about_blurb": (
            "5h / 7d 百分比来自服务端，涵盖全部用量——chat 加 code。\n\n"
            "$ 金额仅来自本地 Claude Code 会话记录：等效 API 价值，不含 chat。\n\n"
            "轮询节奏：API 每 6 分钟，本地 JSONL 每 30 秒。\n\n"
            "MIT 许可证。"
        ),
        # ---- Toast notifications ----
        "toast_5h_window": "5 小时窗口",
        "toast_weekly": "每周配额",
        "toast_model_weekly": "{name} 周配额",
        "toast_title": "Claude {window} 已达 {pct}%",
        "toast_body_default": "提醒——你可能需要放慢节奏或切换项目。",
        "toast_body_90": "正在逼近上限，请合理安排。",
        "toast_body_95": "非常接近上限，再用就会被限流。",
    },
}

_current_lang = "en"


def t(key: str) -> str:
    return LANGUAGES.get(_current_lang, LANGUAGES["en"]).get(key, key)


def get_app_language() -> str:
    return _current_lang


# Sliding-window total lengths, in minutes — used to compute "time remaining as
# % of total window" for display mode 3.
TOTAL_5H_MIN = 5 * 60
TOTAL_7D_MIN = 7 * 24 * 60

# The quota snapshot is considered STALE once it's older than this. A healthy
# pipeline refreshes every API_INTERVAL_SEC (360s); 900s = ~2.5 missed polls, so
# a single failed-then-retried poll never trips it, but a genuinely stalled
# pipeline (auth refresh blocked, dead network) shows up clearly.
# When stale, the strip greys the quota and appends a marker so a future stall
# reads as "old data", never as a frozen/broken app.
STALE_AFTER_SEC = 900


def usage_age_seconds(last_api_fetch: float) -> int:
    """Seconds since the last successful quota fetch, or -1 if none yet."""
    if not last_api_fetch:
        return -1
    import time as _t
    return int(_t.time() - last_api_fetch)


def is_usage_stale(s) -> bool:
    """True when the displayed quota is old enough to flag — either the data has
    aged past STALE_AFTER_SEC, or token refresh is currently blocked/failing."""
    if getattr(s, "token_expired", False):
        return True
    age = usage_age_seconds(s.last_api_fetch)
    return age < 0 or age > STALE_AFTER_SEC


# ---- Visual constants (dark theme) ----
# Softer, less "industrial" palette: deep neutral background, gently raised
# cards with NO hard 1px outlines (the borderless look), and a slightly
# desaturated violet accent. Progress bars are rounded pills (see _draw_bar).
BG = "#15161b"          # window background
PANEL = "#1e2026"       # raised card background (elevation via tone, not borders)
PANEL_2 = "#282a33"     # hover / pressed surface
FG = "#edeef2"          # primary text
FG_DIM = "#9a9dab"      # secondary text
ACCENT = "#8b7cff"      # violet (matches Claude branding, softened)
WARN = "#f0a860"        # orange
DANGER = "#f7716f"      # red
OK = "#3ddc97"          # green
BORDER = "#2b2d37"      # progress-bar track + DWM hairline window border

WINDOW_W = 360          # fixed width; height follows content (FloatingWindow._fit_height)
TRAY_ICON_SIZE = 64     # internal render size; Windows downsamples to taskbar size

# Sentinel "transparent color" for the strip's window — any pixel matching this
# exact color becomes fully transparent via Toplevel's `-transparentcolor` attr.
# Picked to be unlikely to ever appear in our text/outline (near-black but not
# pure black, since pure black is used for outlines).
TRANSPARENT_KEY = "#010101"
# Outline color for strip text — drawn at 4 cardinal offsets to keep text
# legible against any underlying desktop/taskbar color.
TEXT_OUTLINE = "#000000"

# Taskbar strip — a borderless always-on-top window pinned just above the Windows
# taskbar, simulating the look of an embedded taskbar widget (without using the
# deprecated DeskBand COM API). Dimensions are in tkinter logical pixels.
STRIP_W, STRIP_H = 360, 26
STRIP_SIDE = "left"         # "left" or "right" — which side of the screen to pin to
STRIP_SIDE_MARGIN = 12      # gap from the chosen screen edge
# Strip look. "bars"/"rings" are drawn by strip_render into a native, DPI-aware,
# taskbar-owned layered window (taskbar_widget.py). "classic" is the original
# outlined-text tkinter strip, kept for anyone who prefers it.
STRIP_STYLES = ("bars", "rings", "classic")
STRIP_STYLE_DEFAULT = "bars"
# The strip pins ON the Windows taskbar by default (get_strip_default_y centers
# it in the taskbar band). This placement is deliberate and load-bearing:
# maximized windows fill only the work area (which ends at the taskbar's top
# edge), so a strip sitting in the taskbar band is structurally immune to being
# covered by them — the failure that kept biting us when the default was "just
# above the taskbar". Z-order there is still contended by the taskbar's own
# HWND_TOPMOST window, so we defend with three mechanisms:
#   (1) every tick (1s) we re-bump topmost via the tkinter "off→on + lift"
#       trick, plus a direct SetWindowPos(HWND_TOPMOST) for stubborn cases
#   (2) a burst of bumps in the first ~10 seconds after launch — autostart
#       races with the shell often push us behind the taskbar at boot
#   (3) WindowFromPoint sampling detects when something IS in front of us
#       (Quick Settings, Notification Center, the taskbar itself after an
#       autostart race) and escalates the bump immediately
# STRIP_GAP_FROM_TASKBAR is retained for users who prefer the strip floating
# above the taskbar — set it >0 and adjust get_strip_default_y if desired.
STRIP_GAP_FROM_TASKBAR = 0  # legacy: gap between strip bottom and taskbar top


# ---- Persistent config (just strip position for now) ----
# Lives next to the script so it's easy to find / delete / reset.
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        logging.getLogger(__name__).warning("failed to save config: %s", e)


def set_app_language(lang: str) -> None:
    """Switch the UI language and persist the choice."""
    global _current_lang
    if lang not in LANGUAGES:
        return
    _current_lang = lang
    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)


# ---- Run-at-startup (Windows Startup-folder shortcut) ----
# We manage a single canonical .lnk in the per-user Startup folder. Detection
# also recognizes any pre-existing user-made launcher (.lnk / .bat / .cmd / .vbs)
# that references this app.py, so the checkbox reflects an autostart the user set
# up manually and unchecking removes whichever file we found.
APP_PY = Path(__file__).resolve()
APP_DIR = APP_PY.parent
STARTUP_SHORTCUT_NAME = "ClaudeUsageMonitor.lnk"
# CreateProcess flag to suppress the brief console window when we shell out to
# PowerShell for shortcut COM operations.
_CREATE_NO_WINDOW = 0x08000000


def startup_dir() -> Path:
    """The per-user Windows Startup folder (where shortcuts auto-run at login)."""
    return (Path(os.environ.get("APPDATA", ""))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")


def pythonw_path() -> str:
    """Path to pythonw.exe next to the running interpreter (console-less), or
    sys.executable if pythonw isn't found alongside it."""
    cand = Path(sys.executable).with_name("pythonw.exe")
    return str(cand) if cand.is_file() else sys.executable


def _run_powershell(script: str) -> str | None:
    """Run a PowerShell snippet with no console flash; return stdout (stripped)
    or None on failure. Never raises."""
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logging.getLogger(__name__).warning("powershell call failed: %s", e)
        return None
    if proc.returncode != 0:
        logging.getLogger(__name__).warning(
            "powershell returned %d: %s", proc.returncode, proc.stderr.strip())
        return None
    return proc.stdout.strip()


def _read_shortcut_target(lnk: Path) -> str:
    """Read a .lnk's TargetPath + Arguments via WScript.Shell COM, returned as
    one lowercase string for substring matching. '' on any failure."""
    script = (
        "$s=New-Object -ComObject WScript.Shell;"
        f"$sc=$s.CreateShortcut('{lnk}');"
        "Write-Output $sc.TargetPath;Write-Output $sc.Arguments"
    )
    out = _run_powershell(script)
    return (out or "").lower()


def find_autostart_entry(directory: Path | None = None) -> Path | None:
    """Return the Startup file that launches this app, or None.

    Resolution order:
      1. Our canonical shortcut (STARTUP_SHORTCUT_NAME) if it exists.
      2. Any *.lnk whose target/arguments reference this app.py.
      3. Any *.bat / *.cmd / *.vbs whose text references this app.py.

    `directory` defaults to the real Startup folder; tests pass a temp dir.
    """
    directory = directory or startup_dir()
    canonical = directory / STARTUP_SHORTCUT_NAME
    if canonical.exists():
        return canonical
    if not directory.is_dir():
        return None
    needle = str(APP_PY).lower()
    # Also match a forward-slash spelling some launchers use on Windows.
    needle_alt = needle.replace("\\", "/")
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return None
    for f in entries:
        suffix = f.suffix.lower()
        if suffix == ".lnk":
            hay = _read_shortcut_target(f)
            if needle in hay or needle_alt in hay:
                return f
        elif suffix in (".bat", ".cmd", ".vbs"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace").lower()
            except OSError:
                continue
            if needle in text or needle_alt in text:
                return f
    return None


def is_autostart_enabled(directory: Path | None = None) -> bool:
    return find_autostart_entry(directory) is not None


def create_autostart_entry(directory: Path | None = None) -> Path | None:
    """Create the canonical Startup shortcut launching this app via pythonw.exe.
    Returns the shortcut Path on success, None on failure (logged, never raises)."""
    directory = directory or startup_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logging.getLogger(__name__).warning("cannot create startup dir: %s", e)
        return None
    lnk = directory / STARTUP_SHORTCUT_NAME
    target = pythonw_path()
    script = (
        "$s=New-Object -ComObject WScript.Shell;"
        f"$sc=$s.CreateShortcut('{lnk}');"
        f"$sc.TargetPath='{target}';"
        f"$sc.Arguments='\"{APP_PY}\"';"
        f"$sc.WorkingDirectory='{APP_DIR}';"
        "$sc.Save()"
    )
    out = _run_powershell(script)
    if out is None or not lnk.exists():
        logging.getLogger(__name__).warning("failed to create startup shortcut")
        return None
    return lnk


def remove_autostart_entry(directory: Path | None = None) -> bool:
    """Delete whichever Startup launcher references this app. True if removed (or
    none existed), False on a delete error."""
    entry = find_autostart_entry(directory)
    if entry is None:
        return True
    try:
        entry.unlink()
        return True
    except OSError as e:
        logging.getLogger(__name__).warning("failed to remove startup entry: %s", e)
        return False


# ---- Win32 helpers for taskbar position detection + z-order recovery ----
_SPI_GETWORKAREA = 0x0030
# GetAncestor(hwnd, GA_ROOT) → top-level root of the given window. Used to
# normalize WindowFromPoint hits to a single HWND we can compare against ours.
_GA_ROOT = 2
# SetWindowPos special-z-order constants — HWND_TOPMOST keeps the window above
# all non-topmost windows; pair with the SWP_* flags below to update z-order
# only (no move/resize, no activate, but make sure we're shown).
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040

# Declare argtypes/restype for every Win32 call we make below. Without this,
# ctypes defaults to c_int (32-bit) on both sides — on 64-bit Windows that
# truncates HWNDs to 32 bits, which silently breaks the `WindowFromPoint
# result == our_hwnd` equality check (different upper-32 bits) AND can pass
# a wrong sign-extended HWND_TOPMOST (-1) to SetWindowPos. Explicit typing
# costs nothing and avoids a class of impossible-to-debug heisenbugs.
_user32 = ctypes.windll.user32
_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
]
_user32.SystemParametersInfoW.restype = wintypes.BOOL

# ---- Taskbar-owned docking (the trick Soda Music's taskbar player uses) ----
# Instead of fighting the taskbar for HWND_TOPMOST every second, make the
# taskbar (Shell_TrayWnd) the strip's OWNER. Windows always keeps an owned
# window above its owner in z-order, so the strip sits on the taskbar band
# with no bump loop and no blink. The owner is set with
# SetWindowLongPtrW(GWLP_HWNDPARENT) — on a top-level window that changes the
# owner, not the parent, so the strip stays a normal top-level window in our
# own process (unlike SetParent-style embedding, no cross-process child).
# Explorer restarts give the taskbar a new HWND; the strip tick re-attaches.
_GWLP_HWNDPARENT = -8
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
_user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t


def find_taskbar_hwnd() -> int:
    """HWND of the primary taskbar (Shell_TrayWnd), or 0 if the shell isn't up."""
    return int(_user32.FindWindowW("Shell_TrayWnd", None) or 0)


def attach_to_taskbar(hwnd: int) -> int:
    """Make the primary taskbar the owner of top-level `hwnd` and mark it
    no-activate/tool-window (clicks don't steal focus, no Alt+Tab entry).
    Returns the taskbar HWND on success, 0 if there is no taskbar."""
    taskbar = find_taskbar_hwnd()
    if not taskbar or not hwnd:
        return 0
    ex = _user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    _user32.SetWindowLongPtrW(
        hwnd, _GWL_EXSTYLE, ex | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
    _user32.SetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT, taskbar)
    flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
    _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags)
    return taskbar


def taskbar_owner_of(hwnd: int) -> int:
    """Current owner HWND of `hwnd` (0 if none)."""
    return int(_user32.GetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT) or 0)

# kernel32 for the single-instance named mutex (see main()). CreateMutexW takes a
# LPSECURITY_ATTRIBUTES (NULL here), a BOOL initial-owner, and the name; the HANDLE
# return is irrelevant — we only care whether GetLastError() reports the name
# already existed. Typed so the HANDLE isn't truncated on 64-bit Windows.
_kernel32 = ctypes.windll.kernel32
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wintypes.DWORD

# Returned by GetLastError() after CreateMutexW when a mutex with the same name
# already exists (i.e. another instance of us is already running).
_ERROR_ALREADY_EXISTS = 183

# ---- Win11 DWM chrome for borderless windows ----
# Our top-level windows are overrideredirect (no native title bar — we draw our
# own). On Win11 the DWM can still round the corners and paint a hairline
# border for such windows, which is what gives the "frameless card" look
# instead of a hard rectangle.
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2
_DWMWA_BORDER_COLOR = 34
try:
    _dwmapi = ctypes.windll.dwmapi
    _dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
except OSError:  # pre-Vista or exotic environments — degrade to square corners
    _dwmapi = None


def _colorref(hex_color: str) -> int:
    """'#rrggbb' → Win32 COLORREF (0x00BBGGRR)."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (b << 16) | (g << 8) | r


def apply_frameless_chrome(win: tk.Misc) -> None:
    """Round the corners of a borderless window and give it a subtle DWM
    hairline border. Win11-only attributes; silently a no-op elsewhere.
    Must be called after the window is mapped (deiconify/update_idletasks) and
    re-called after every hide/show cycle — Tk can recreate the OS window."""
    if _dwmapi is None:
        return
    try:
        win.update_idletasks()
        hwnd = int(win.wm_frame(), 16)
    except (ValueError, tk.TclError):
        return
    corner = ctypes.c_int(_DWMWCP_ROUND)
    border = ctypes.c_int(_colorref(BORDER))
    try:
        _dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(corner), ctypes.sizeof(corner))
        _dwmapi.DwmSetWindowAttribute(
            hwnd, _DWMWA_BORDER_COLOR,
            ctypes.byref(border), ctypes.sizeof(border))
    except OSError:
        pass


def bind_window_drag(win: tk.Misc, *handles: tk.Widget) -> None:
    """Make `handles` act as the title-bar drag area for borderless `win`.

    Bind only on the specific handle widgets, never on the toplevel itself —
    a toplevel-level <Button-1> binding would fire for every child widget via
    bindtags and turn the whole window (buttons included) into a drag surface.
    """
    anchor: dict[str, int] = {}

    def press(e) -> None:
        anchor["dx"] = e.x_root - win.winfo_x()
        anchor["dy"] = e.y_root - win.winfo_y()

    def motion(e) -> None:
        if not anchor:
            return
        # Keep the window grabbable. Borderless windows have no taskbar button
        # and no WM move command, so a drag past a screen edge would strand it
        # for the rest of the session. The drag handles sit on the LEFT of our
        # title bar, so keeping the left edge on-screen is what guarantees
        # there is always something left to grab. Mirrors the strip's clamp.
        x = e.x_root - anchor["dx"]
        y = e.y_root - anchor["dy"]
        x = max(0, min(x, win.winfo_screenwidth() - 80))
        y = max(0, min(y, win.winfo_screenheight() - 40))
        win.geometry(f"+{x}+{y}")

    for h in handles:
        h.bind("<Button-1>", press)
        h.bind("<B1-Motion>", motion)


# Segoe MDL2 Assets glyphs (ships with Win10/11) — proper native-looking
# title-bar icons instead of emoji/ASCII stand-ins.
GLYPH_CLOSE = ""      # ChromeClose
GLYPH_PIN = ""        # Pin
GLYPH_REFRESH = ""    # Refresh


def titlebar_btn(parent: tk.Widget, glyph: str, command, *,
                 hover_fg: str = FG, hover_bg: str = PANEL_2) -> tk.Label:
    """A flat, hover-highlighting title-bar button (Label, not tk.Button — no
    3D press animation). The current resting color lives in ._normal_fg so
    toggle buttons (pin) can restyle themselves and hover restores correctly."""
    btn = tk.Label(parent, text=glyph, bg=BG, fg=FG_DIM,
                   font=("Segoe MDL2 Assets", 10), padx=11, pady=7,
                   cursor="hand2")
    btn._normal_fg = FG_DIM  # type: ignore[attr-defined]
    btn.bind("<Button-1>", lambda _e: command())
    btn.bind("<Enter>", lambda _e: btn.config(fg=hover_fg, bg=hover_bg))
    btn.bind("<Leave>", lambda _e: btn.config(fg=btn._normal_fg, bg=BG))
    return btn


def get_taskbar_top_logical(fallback_screen_h: int) -> int:
    """Return the Y coordinate (in this process's coord system) of the top edge of
    the primary monitor's Windows taskbar.

    Uses SystemParametersInfoW(SPI_GETWORKAREA) — unlike SHAppBarMessage, this
    one returns coords in the *calling process's* DPI-aware/unaware coord system,
    which matches tkinter automatically (whether or not we set DPI awareness).

    SHAppBarMessage by contrast returns physical pixels, requiring us to divide
    by the system DPI scale — but GetDpiForSystem() lies (returns 96) for non-
    DPI-aware processes, so the math comes out wrong. We hit exactly that on a
    125%-scaled, multi-monitor setup.

    Falls back to (screen_h - 48) if the SPI call fails.
    """
    rect = wintypes.RECT()
    ok = _user32.SystemParametersInfoW(
        _SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    if not ok:
        return fallback_screen_h - 48
    # For a bottom-docked taskbar (the common case), the work area's bottom edge
    # equals the top of the taskbar. If the taskbar is on top/left/right, this
    # value is just where the work area ends downward — still a reasonable spot
    # to pin a strip on the primary monitor.
    return rect.bottom


def get_strip_default_y(screen_h: int) -> int:
    """Y coordinate that vertically centers the strip *inside* the taskbar band.

    This is the crux of keeping the strip reliably visible. A maximized normal
    window can only ever fill the work area, whose bottom edge is the taskbar's
    top edge. So:

      * Placing the strip JUST ABOVE the taskbar (the old default, y = tb_top -
        STRIP_H) puts it in the bottom sliver of the work area — exactly where
        a maximized app (Chrome, the Claude desktop app, an editor) covers it.
        That was the regression: a position-reset dropped the strip there and
        every maximized window hid it.

      * Placing the strip ON the taskbar (y >= tb_top) makes it structurally
        unreachable by maximized windows. The only thing that contends for
        z-order there is the taskbar's own HWND_TOPMOST window, and the
        per-tick force-topmost bump + _is_covered() escalation win that fight.

    Falls back to a standard 48 px taskbar height if the detected band looks
    implausible (e.g. an auto-hide or side-docked taskbar reporting a weird
    work-area bottom).
    """
    tb_top = get_taskbar_top_logical(screen_h)
    tb_height = screen_h - tb_top
    if tb_height <= 0 or tb_height > 200:
        tb_height = 48
    return tb_top + max(0, (tb_height - STRIP_H) // 2)


def color_for_pct(pct: float) -> str:
    if pct >= 90:
        return DANGER
    if pct >= 75:
        return WARN
    return ACCENT


# Neutral gray for the "unknown" tray badge — shown before the first successful
# fetch (and from any state where we genuinely have no percentage to display).
TRAY_UNKNOWN_BG = "#5a5d63"


def render_tray_icon(pct: float | None) -> Image.Image:
    """Render the tray icon: rounded rect with the 5h percentage as big text.

    pct=None renders a "?" on a neutral gray rect — the honest "no data yet"
    state, used until the first snapshot lands instead of a misleading "0".
    """
    size = TRAY_ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color = TRAY_UNKNOWN_BG if pct is None else color_for_pct(pct)
    # Background rounded rect
    draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=14, fill=color)

    # Text: e.g. "33" or "100", or "?" when unknown.
    text = "?" if pct is None else f"{int(pct)}"
    # pick a font size that fits — 3-digit numbers get smaller text
    font_size = 30 if len(text) <= 2 else 24
    try:
        font = ImageFont.truetype("seguisb.ttf", font_size)  # Segoe UI Semibold
    except (OSError, IOError):
        try:
            font = ImageFont.truetype("arial.ttf", font_size)
        except (OSError, IOError):
            font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 2),
              text, fill="white", font=font)
    return img


def fmt_minutes(m: int) -> str:
    """Format a number of minutes as either '47m' or '2h 13m'."""
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h {mm}m"
    d, hh = divmod(h, 24)
    return f"{d}d {hh}h"


def fmt_age(seconds: int) -> str:
    """Format a freshness age (seconds since last fetch) compactly:
    '12s' / '3m' / '2h 05m'. Used by the floating window footer."""
    if seconds < 60:
        return f"{seconds}s"
    m, _s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    return f"{h}h {mm:02d}m"


def fmt_reset_clock(iso_ts: str) -> str:
    """Return the local wall-clock 'HH:MM' for an ISO 8601 reset timestamp, or
    '' if it can't be parsed. Used for the absolute-time reset caption."""
    from datetime import datetime
    if not iso_ts:
        return ""
    try:
        target = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    # fromisoformat yields an aware datetime; astimezone() with no arg converts
    # to the system local timezone for display.
    return target.astimezone().strftime("%H:%M")


def _truncate_project(cwd: str, max_len: int = 40) -> tuple[str, str]:
    """Split a project working dir into (basename, dimmed-parent) for display.

    The basename (last path component) is shown at full strength; the parent
    path is dimmed and middle-truncated so long paths stay one line without
    losing the meaningful head and tail. Returns ("", "") for an empty cwd.
    """
    if not cwd:
        return ("", "")
    norm = cwd.replace("\\", "/").rstrip("/")
    base = norm.rsplit("/", 1)[-1] if "/" in norm else norm
    parent = norm[: len(norm) - len(base)].rstrip("/")
    if parent and len(parent) > max_len:
        head = max_len // 2 - 1
        tail = max_len - head - 1
        parent = parent[:head] + "…" + parent[-tail:]
    return (base, parent)


class FloatingWindow:
    """The tkinter floating-card window. Hidden by default; tray toggles visibility.

    Layout (top→bottom): header, quota card, cost card, projects card, footer.
    All per-tick updates go through .config() on PRE-CREATED widgets so the
    window never flickers; the small Canvas bars cache their last drawn value
    and skip redrawing when it's unchanged.
    """

    PROJ_ROWS_MAX = 6
    # Max simultaneously-displayed model-scoped weekly buckets (Fable, …).
    SCOPED_ROWS_MAX = 4

    def __init__(self, orch: Orchestrator, on_close: callable) -> None:
        self.orch = orch
        self.on_close = on_close
        self.root = tk.Tk()
        self.root.title(t("win_title"))
        self.root.configure(bg=BG)
        # Borderless: no native title bar — we draw our own (draggable) header
        # and let Win11 DWM round the corners (apply_frameless_chrome in show()).
        self.root.overrideredirect(True)
        # Position only; height packs to content (the width-anchor frame in
        # _build pins the width to WINDOW_W).
        self.root.geometry("+200+200")
        self.root.attributes("-topmost", True)     # always on top by default
        self.root.resizable(False, False)
        # Closing just hides — quitting is via tray. (No WM decorations exist,
        # but keep the protocol handler for completeness/AltF4-like paths.)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _e: self.hide())
        # Start hidden until the user explicitly shows from tray.
        self.root.withdraw()

        # Per-bar last-drawn cache: (value_pct_rounded, width) → skip identical
        # redraws. Keyed by a bar id ("5h", "7d", "scoped0".."scoped3",
        # "proj0".."proj5").
        self._bar_cache: dict[str, tuple] = {}
        # Last height pushed through geometry() — see _fit_height.
        self._applied_h: int | None = None
        # Count of currently-packed scoped rows; drives the repack in _render.
        self._scoped_shown = -1

        self._build()
        # Periodically repaint with the latest snapshot from orchestrator.
        self._tick()

    # ---- construction helpers ----

    def _card(self, pad: int) -> tk.Frame:
        """A raised PANEL card — no outline; elevation comes from tone alone
        (the borderless look)."""
        card = tk.Frame(self.root, bg=PANEL, highlightthickness=0, bd=0)
        card.pack(fill="x", padx=pad, pady=5)
        return card

    def _build(self) -> None:
        pad = 16

        # Width anchor: pins the auto-sized window to WINDOW_W while height
        # stays free to pack to content (scoped-model rows come and go).
        tk.Frame(self.root, bg=BG, width=WINDOW_W, height=1).pack()

        # --- Custom title bar (the window is borderless) ---
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=(pad, 6), pady=(8, 4))
        self.title_dot = tk.Canvas(header, width=8, height=8, bg=BG,
                                   highlightthickness=0, bd=0)
        self.title_dot.create_oval(0, 0, 7, 7, fill=ACCENT, outline="")
        self.title_dot.pack(side="left")
        self.title_lbl = tk.Label(header, text=t("win_title"), bg=BG, fg=FG,
                                  font=("Segoe UI Semibold", 12))
        self.title_lbl.pack(side="left", padx=(8, 0))
        close_btn = titlebar_btn(header, GLYPH_CLOSE, self.hide,
                                 hover_fg="#ffffff", hover_bg="#b3404a")
        close_btn.pack(side="right")
        self.pin_on = True
        self.pin_btn = titlebar_btn(header, GLYPH_PIN, self._toggle_topmost)
        self.pin_btn._normal_fg = ACCENT      # pinned by default
        self.pin_btn.config(fg=ACCENT)
        self.pin_btn.pack(side="right")
        self.refresh_btn = titlebar_btn(header, GLYPH_REFRESH,
                                        self._refresh_clicked)
        self.refresh_btn.pack(side="right")
        # Dragging the header (not its buttons) moves the window.
        bind_window_drag(self.root, header, self.title_lbl, self.title_dot)

        # --- Quota card (5h + 7d + model-scoped weekly buckets) ---
        quota = self._card(pad)
        self.quota_widgets: dict[str, tuple] = {}
        for key, label_key in (("5h", "lbl_5h_window"), ("7d", "lbl_weekly")):
            # Each main quota row lives in its own frame so the 5h row can be
            # hidden on plans without a 5h limit (Ultra) and re-shown in place.
            holder = tk.Frame(quota, bg=PANEL)
            holder.pack(fill="x")
            row = tk.Frame(holder, bg=PANEL)
            row.pack(fill="x", padx=12, pady=(10, 0))
            lbl = tk.Label(row, text=t(label_key), bg=PANEL, fg=FG_DIM,
                           font=("Segoe UI", 9), anchor="w")
            lbl.pack(side="left")
            pct_lbl = tk.Label(row, text="—", bg=PANEL, fg=FG,
                               font=("Segoe UI Semibold", 16), anchor="e")
            pct_lbl.pack(side="right")
            bar_canvas = tk.Canvas(holder, height=6, bg=PANEL,
                                   highlightthickness=0, bd=0)
            bar_canvas.pack(fill="x", padx=12, pady=(3, 0))
            caption = tk.Label(holder, text="", bg=PANEL, fg=FG_DIM,
                               font=("Segoe UI", 8), anchor="w")
            caption.pack(fill="x", padx=12, pady=(2, 8))
            # The label is pre-created and updated in _render so a language
            # switch reflects on the next tick.
            self.quota_widgets[key] = {
                "holder": holder, "shown": True,
                "label": lbl, "label_key": label_key,
                "pct": pct_lbl, "bar": bar_canvas, "caption": caption,
            }

        # Model-scoped weekly rows (Fable, …) — pre-created, shown only when the
        # snapshot carries scoped buckets. Slightly slimmer than the main rows;
        # each has its own reset caption because scoped buckets can reset at a
        # different time than the overall weekly window.
        self.scoped_rows: list[dict] = []
        for _ in range(self.SCOPED_ROWS_MAX):
            frame = tk.Frame(quota, bg=PANEL)
            row = tk.Frame(frame, bg=PANEL)
            row.pack(fill="x", padx=12)
            lbl = tk.Label(row, text="", bg=PANEL, fg=FG_DIM,
                           font=("Segoe UI", 9), anchor="w")
            lbl.pack(side="left")
            pct_lbl = tk.Label(row, text="", bg=PANEL, fg=FG,
                               font=("Segoe UI Semibold", 13), anchor="e")
            pct_lbl.pack(side="right")
            bar = tk.Canvas(frame, height=5, bg=PANEL, highlightthickness=0, bd=0)
            bar.pack(fill="x", padx=12, pady=(3, 0))
            caption = tk.Label(frame, text="", bg=PANEL, fg=FG_DIM,
                               font=("Segoe UI", 8), anchor="w")
            caption.pack(fill="x", padx=12, pady=(2, 8))
            self.scoped_rows.append({
                "frame": frame, "label": lbl, "pct": pct_lbl,
                "bar": bar, "caption": caption,
            })

        # --- Cost card ---
        cost = self._card(pad)
        self.cost_caption = tk.Label(cost, text=t("cost_caption"), bg=PANEL,
                                     fg=FG_DIM, font=("Segoe UI", 8), anchor="w")
        self.cost_caption.pack(fill="x", padx=12, pady=(8, 2))
        self.cost_labels: dict[str, tk.Label] = {}
        for key, label_key, emphasized in (
            ("session", "lbl_session", False),
            ("today", "lbl_today", True),
            ("month", "lbl_this_month", False),
        ):
            row = tk.Frame(cost, bg=PANEL)
            row.pack(fill="x", padx=12, pady=2)
            name_font = ("Segoe UI Semibold", 11) if emphasized else ("Segoe UI", 10)
            name_fg = FG if emphasized else FG_DIM
            nm = tk.Label(row, text=t(label_key), bg=PANEL, fg=name_fg,
                          font=name_font, anchor="w")
            nm.pack(side="left")
            val_font = ("Segoe UI Semibold", 11) if emphasized else ("Segoe UI", 10)
            val = tk.Label(row, text="—", bg=PANEL, fg=FG, font=val_font, anchor="e")
            val.pack(side="right")
            self.cost_labels[key] = val
            # Keep a handle to the name label for i18n refresh.
            self.cost_labels[key + "_name"] = nm
            self.cost_labels[key + "_name_key"] = label_key  # type: ignore[assignment]
        # bottom pad
        tk.Frame(cost, bg=PANEL, height=4).pack()

        # --- Projects card ---
        proj_header = tk.Frame(self.root, bg=BG)
        proj_header.pack(fill="x", padx=pad, pady=(8, 2))
        self.proj_header_lbl = tk.Label(proj_header, text=t("projects_header"),
                                        bg=BG, fg=FG_DIM, font=("Segoe UI", 9))
        self.proj_header_lbl.pack(side="left")

        proj_card = self._card(pad)
        # Pre-create N fixed project rows (label row + proportional bar). _render
        # only updates text/bars and pack_forgets unused rows — no destroy/create.
        self.proj_rows: list[dict] = []
        for _ in range(self.PROJ_ROWS_MAX):
            container = tk.Frame(proj_card, bg=PANEL)
            row = tk.Frame(container, bg=PANEL)
            row.pack(fill="x", padx=12, pady=(4, 0))
            base_lbl = tk.Label(row, text="", bg=PANEL, fg=FG,
                                font=("Segoe UI", 9), anchor="w")
            base_lbl.pack(side="left")
            parent_lbl = tk.Label(row, text="", bg=PANEL, fg=FG_DIM,
                                  font=("Segoe UI", 8), anchor="w")
            parent_lbl.pack(side="left", padx=(4, 0))
            cost_lbl = tk.Label(row, text="", bg=PANEL, fg=FG_DIM,
                                font=("Segoe UI", 9), anchor="e")
            cost_lbl.pack(side="right")
            bar = tk.Canvas(container, height=2, bg=PANEL, highlightthickness=0, bd=0)
            bar.pack(fill="x", padx=12, pady=(1, 3))
            self.proj_rows.append({
                "container": container, "base": base_lbl,
                "parent": parent_lbl, "cost": cost_lbl, "bar": bar,
            })

        # --- Footer (freshness / error) ---
        self.footer = tk.Label(self.root, text="", bg=BG, fg=FG_DIM,
                               font=("Segoe UI", 8))
        self.footer.pack(side="bottom", anchor="e", padx=pad, pady=(2, 8))

    def _refresh_clicked(self) -> None:
        self.orch.refresh_now()

    def _toggle_topmost(self) -> None:
        self.pin_on = not self.pin_on
        self.root.attributes("-topmost", self.pin_on)
        # Restyle the pin as a toggle: accent when pinned, dim when not.
        self.pin_btn._normal_fg = ACCENT if self.pin_on else FG_DIM
        self.pin_btn.config(fg=self.pin_btn._normal_fg)

    def show(self) -> None:
        # Refresh the title here (cheap, and the only moment it's visible) so a
        # language switch made while hidden is reflected when the window opens.
        self.root.title(t("win_title"))
        self.title_lbl.config(text=t("win_title"))
        # Re-assert borderless before mapping: some Tk builds re-decorate an
        # overrideredirect window across a withdraw/deiconify cycle.
        self.root.overrideredirect(True)
        self.root.deiconify()
        # Content may have changed while hidden (ticks keep running); re-fit
        # before the user sees the window rather than one tick later.
        self._fit_height()
        apply_frameless_chrome(self.root)
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()
        if self.on_close:
            self.on_close()

    def _fit_height(self) -> None:
        """Pin the window to WINDOW_W and let only the HEIGHT follow content.

        Auto-sizing on both axes lets a long project path widen the window, so
        its width would jump around as the project list changes. Setting an
        explicit geometry turns tk's auto-sizing off; winfo_reqheight() still
        reports what the content wants (every row is single-line, so it doesn't
        depend on the forced width), which is what makes the scoped Fable-style
        rows able to grow and shrink the window without touching its width.
        """
        # Flush pending layout first: a scoped row packed/forgotten moments ago
        # has only queued its geometry request, so winfo_reqheight() would still
        # report the PREVIOUS frame's height and the window would lag one tick
        # behind a row appearing or disappearing.
        self.root.update_idletasks()
        req_h = self.root.winfo_reqheight()
        if req_h != self._applied_h:
            self._applied_h = req_h
            self.root.geometry(f"{WINDOW_W}x{req_h}")

    def _tick(self) -> None:
        # Reschedule in `finally` so a render exception can never break the chain
        # (same hardening as the strip — see TaskbarStrip._tick).
        try:
            self._render(self.orch.snapshot())
            self._fit_height()
        except Exception:
            logging.getLogger(__name__).exception("window render failed")
        finally:
            # Repaint every second so the "resets in Xm" countdown updates live.
            self.root.after(1000, self._tick)

    # ---- per-tick bar drawing with last-value cache ----

    def _draw_bar(self, bar_id: str, canvas: tk.Canvas, pct: float, color: str,
                  height: int) -> None:
        """Draw a rounded pill progress bar (track + fill) via wide round-capped
        lines, skipping the redraw when neither the rounded percent nor the
        canvas width changed since the last call for this bar_id. Avoids
        per-tick flicker on static values."""
        # Tk reports width 1 — NOT 0 — for a widget that has never been laid
        # out, so a truthiness test here would silently accept 1 and both draw
        # a stub blob AND poison the cache with it until the width changes.
        # Reachable on the first render (the root is withdrawn until show())
        # and on the tick a scoped/project row is packed for the first time.
        w = canvas.winfo_width()
        if w <= 1:
            w = WINDOW_W - 2 * 16 - 24     # card padx 16 + canvas padx 12
        key = (round(pct, 1), w, color)
        if self._bar_cache.get(bar_id) == key:
            return
        self._bar_cache[bar_id] = key
        canvas.delete("all")
        # A line of width=height with round caps IS a pill; endpoints are inset
        # by the cap radius so the caps stay inside the canvas.
        r = height / 2
        y = height / 2
        canvas.create_line(r, y, w - r, y, fill=BORDER, width=height,
                           capstyle="round")
        fill_w = w * min(max(pct, 0), 100) / 100
        if fill_w > 0:
            canvas.create_line(r, y, max(r + 1, fill_w - r), y, fill=color,
                               width=height, capstyle="round")

    def _render(self, s) -> None:
        u: UsageSnapshot | None = s.usage
        rpt = s.report
        stale = u is not None and is_usage_stale(s)

        # --- Quota rows ---
        for key, getters in (
            ("5h", (lambda: u.five_hour_pct, lambda: u.five_hour_minutes_to_reset,
                    lambda: u.five_hour_active, lambda: u.five_hour_reset)),
            ("7d", (lambda: u.seven_day_pct, lambda: u.seven_day_minutes_to_reset,
                    lambda: u.seven_day_active, lambda: u.seven_day_reset)),
        ):
            w = self.quota_widgets[key]
            if key == "5h":
                # Plans without a 5h limit (Ultra): hide the row entirely.
                want = u is None or u.has_five_hour
                if want != w["shown"]:
                    w["shown"] = want
                    if want:
                        w["holder"].pack(fill="x",
                                         before=self.quota_widgets["7d"]["holder"])
                    else:
                        w["holder"].pack_forget()
                if not want:
                    continue
            w["label"].config(text=t(w["label_key"]))  # i18n refresh
            if u is None:
                w["pct"].config(text="—", fg=FG_DIM)
                w["caption"].config(
                    text=(s.usage_error[:60] if s.usage_error else t("loading")),
                    fg=(DANGER if s.usage_error else FG_DIM))
                self._draw_bar(key, w["bar"], 0, BORDER, 6)
                continue
            pct = getters[0]()
            minutes = getters[1]()
            active = getters[2]()
            reset_iso = getters[3]()
            # Stale data: grey the percentage and show a stale caption rather than
            # a reset countdown computed from a stale resets_at (which would be
            # past and misleading). The footer carries the actionable hint.
            color = FG_DIM if stale else color_for_pct(pct)
            w["pct"].config(text=f"{pct:.0f}%", fg=color)
            if stale:
                w["caption"].config(text=t("stale_mark"), fg=WARN)
            elif active:
                clock = fmt_reset_clock(reset_iso)
                if clock:
                    cap = t("resets_at").format(time=clock, left=fmt_minutes(minutes))
                else:
                    # No parseable absolute time — fall back to relative only.
                    cap = fmt_minutes(minutes)
                w["caption"].config(text=cap, fg=FG_DIM)
            else:
                w["caption"].config(text=t("idle_full"), fg=FG_DIM)
            self._draw_bar(key, w["bar"], pct, color if not stale else BORDER, 6)

        # Model-scoped weekly rows (Fable, …) — shown only when present.
        scoped = u.scoped_limits if u is not None else ()
        needed = min(len(scoped), len(self.scoped_rows))
        # Re-pack the visible prefix in index order whenever the count changes —
        # incremental pack/forget could leave rows visually out of order after a
        # bucket disappears and later returns. Tracked in a variable, not
        # winfo_ismapped(), which lags within a tick.
        if self._scoped_shown != needed:
            self._scoped_shown = needed
            for i, sr in enumerate(self.scoped_rows):
                sr["frame"].pack_forget()
                self._bar_cache.pop(f"scoped{i}", None)
            for sr in self.scoped_rows[:needed]:
                sr["frame"].pack(fill="x")
        for i, sr in enumerate(self.scoped_rows[:needed]):
            sl = scoped[i]
            sr["label"].config(text=t("lbl_model_weekly").format(name=sl.name))
            color = FG_DIM if stale else color_for_pct(sl.pct)
            sr["pct"].config(text=f"{sl.pct:.0f}%", fg=color)
            if stale:
                sr["caption"].config(text=t("stale_mark"), fg=WARN)
            elif sl.active:
                clock = fmt_reset_clock(sl.reset)
                cap = (t("resets_at").format(
                           time=clock, left=fmt_minutes(sl.minutes_to_reset))
                       if clock else fmt_minutes(sl.minutes_to_reset))
                sr["caption"].config(text=cap, fg=FG_DIM)
            else:
                # No resets_at. For the 5h/7d rows that reliably means "no live
                # window", but a scoped bucket can report a real utilization
                # with no reset time — printing "idle (full quota)" under a big
                # red 90% would be a flat contradiction. Only claim idle when
                # the number actually agrees.
                sr["caption"].config(
                    text=(t("idle_full") if sl.pct <= 0 else ""), fg=FG_DIM)
            self._draw_bar(f"scoped{i}", sr["bar"], sl.pct,
                           BORDER if stale else color, 5)

        # --- Cost rows ---
        # Refresh names for i18n.
        for key in ("session", "today", "month"):
            self.cost_labels[key + "_name"].config(
                text=t(self.cost_labels[key + "_name_key"]))
        self.cost_caption.config(text=t("cost_caption"))
        if rpt is None:
            for key in ("session", "today", "month"):
                self.cost_labels[key].config(text=t("loading"))
        else:
            session_cost = rpt.by_session.get(rpt.last_session_id, Aggregate()).cost_usd
            self.cost_labels["session"].config(text=f"${session_cost:,.4f}")
            self.cost_labels["today"].config(text=f"${rpt.today.cost_usd:,.2f}")
            self.cost_labels["month"].config(text=f"${rpt.this_month.cost_usd:,.2f}")

        # --- Projects (month-scoped) with proportional bars ---
        self.proj_header_lbl.config(text=t("projects_header"))
        top: list[tuple[str, Aggregate]] = []
        if rpt is not None:
            top = sorted(rpt.by_project_month.items(),
                         key=lambda kv: kv[1].cost_usd, reverse=True)[:self.PROJ_ROWS_MAX]
        max_cost = top[0][1].cost_usd if top else 0.0
        for i, pr in enumerate(self.proj_rows):
            if i < len(top):
                cwd, agg = top[i]
                base, parent = _truncate_project(cwd)
                pr["base"].config(text=base)
                pr["parent"].config(text=parent)
                pr["cost"].config(text=f"${agg.cost_usd:,.2f}")
                # Bar width proportional to the top project's cost.
                frac = (agg.cost_usd / max_cost * 100) if max_cost > 0 else 0
                self._draw_bar(f"proj{i}", pr["bar"], frac, ACCENT, 2)
                # Unconditional: while the panel is withdrawn (its normal
                # state) every child reports ismapped()==0 even though it is
                # still pack-managed, so a mapped-state test would never drop
                # a row that fell out of the top-N — it would keep showing the
                # previous month's project until the first tick after the user
                # opens the panel. Re-packing an already-managed slave keeps
                # its position, so ordering is unaffected.
                pr["container"].pack(fill="x")
            else:
                pr["container"].pack_forget()
                self._bar_cache.pop(f"proj{i}", None)

        # --- Footer ---
        age = usage_age_seconds(s.last_api_fetch)
        if stale:
            # Actionable: auth-stale means automatic refresh could not currently
            # produce live data; other stale causes (network, 429) show age only.
            if getattr(s, "token_expired", False):
                self.footer.config(text=t("stale_token"), fg=WARN)
            else:
                self.footer.config(
                    text=t("stale_generic").format(age=fmt_age(age) if age >= 0 else "—"),
                    fg=WARN)
        elif s.usage_error and u is None:
            self.footer.config(text=s.usage_error[:60], fg=DANGER)
        else:
            self.footer.config(
                text=(t("updated_ago").format(age=fmt_age(age)) if age >= 0 else ""),
                fg=FG_DIM)


class TaskbarStrip:
    """Always-visible compact strip pinned just above the Windows taskbar.

    Visually mimics a taskbar widget, but is actually a borderless Toplevel
    positioned with Win32 taskbar-rect detection — no DeskBand COM hackery.
    Left-click expands the main floating window; right-click opens a menu.
    """

    def __init__(self, parent_root: tk.Tk, orch: Orchestrator,
                 on_left_click: callable, on_settings: callable | None = None) -> None:
        self.orch = orch
        self.on_left_click_cb = on_left_click
        self.on_settings_cb = on_settings or (lambda: None)
        self.visible = True
        self.drag_mode = False
        self._drag_anchor: tuple[int, int, int, int] | None = None
        # Custom position saved from a previous drag — overrides STRIP_SIDE rules.
        # Loaded once at init; updated when user finishes a drag.
        cfg = load_config().get("strip") or {}
        self._custom_pos: tuple[int, int] | None = (
            (int(cfg["x"]), int(cfg["y"]))
            if isinstance(cfg.get("x"), (int, float)) and isinstance(cfg.get("y"), (int, float))
            else None
        )
        # Optional opaque dark backdrop — off by default (transparent looks
        # cleaner over the taskbar). Useful when the strip is dragged onto a
        # light desktop area where outlined text alone is hard to read.
        self.show_background = bool(cfg.get("show_background", False))
        # Display mode (see _append_quota_parts for the layout per mode):
        #   1 = compact: just quota%
        #   2 = quota% + (remaining time as h/m/d string)
        #   3 = quota% / remaining-time-as-% (counts DOWN as window expires)
        #   4 = quota% / elapsed-time-as-% (counts UP — same direction as quota%)
        # Cap to the valid range so a hand-edited config can't put us in a weird state.
        raw_mode = cfg.get("display_mode", 1)
        self.display_mode = raw_mode if raw_mode in (1, 2, 3, 4) else 1
        # Which screen edge the strip pins to when there's no saved drag position.
        # Configurable via Settings → Strip; defaults to the module-level STRIP_SIDE.
        raw_side = cfg.get("side", STRIP_SIDE)
        self.side = raw_side if raw_side in ("left", "right") else STRIP_SIDE
        raw_style = cfg.get("style", STRIP_STYLE_DEFAULT)
        self.style = raw_style if raw_style in STRIP_STYLES else STRIP_STYLE_DEFAULT
        self._native: NativeTaskbarWidget | None = None
        self._native_hover = False
        self.win = None  # classic-style Toplevel; None in the modern styles

        # Font instances (not just tuples) so we can call .measure() during
        # layout. They're interpreter-level and survive a window teardown, so
        # create them once here rather than per-window.
        self.font_main = tkfont.Font(family="Segoe UI Semibold", size=9)
        self.font_dim = tkfont.Font(family="Segoe UI", size=9)

        # Keep the parent so the tick loop can REBUILD the strip window if an
        # external event destroys it out from under us (see _create_window).
        self._parent_root = parent_root
        self._render_sig = None
        if self._classic:
            self._create_window()
            # Validate saved drag position before first paint: only catches
            # *truly off-screen* positions (a now-disconnected monitor, or a
            # shrunk resolution). Taskbar overlap is fine — _is_covered() + the
            # topmost burst handle z-order contention.
            self._validate_custom_pos()
            self._reposition()
        else:
            self._start_native()
        self._tick()
        self._poll_native()

    # ---- modern styles (native taskbar widget) ----

    @property
    def _classic(self) -> bool:
        return self.style == "classic"

    def _start_native(self) -> None:
        """Create the native taskbar widget; fall back to classic on failure."""
        self._native = NativeTaskbarWidget(side=self.side)
        if not self._native.hwnd:
            logging.getLogger(__name__).warning(
                "native taskbar widget unavailable; falling back to classic strip")
            self._native = None
            self.style = "classic"
            self._create_window()
            self._reposition()
            return
        self._render_sig = None
        self._menu = tk.Menu(self._parent_root, tearoff=0, bg=PANEL, fg=FG,
                             activebackground=ACCENT, activeforeground="white",
                             borderwidth=0)
        if not self.visible:
            self._native.hide()

    def _stop_native(self) -> None:
        if self._native is not None:
            self._native.close()
            self._native = None

    def _poll_native(self) -> None:
        """Drain input events from the widget thread (60 ms cadence so clicks
        feel instant; the 1 s tick is too slow for that)."""
        try:
            nat = self._native
            while nat is not None:
                try:
                    ev = nat.events.get_nowait()
                except queue.Empty:
                    break
                kind = ev[0]
                if kind == "click":
                    self.on_left_click_cb()
                elif kind == "menu":
                    self._popup_menu(*self._parent_root.winfo_pointerxy())
                elif kind in ("hover", "rerender"):
                    if kind == "hover":
                        self._native_hover = bool(ev[1])
                    self._render_sig = None
                    self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("native widget poll failed")
        finally:
            try:
                self._parent_root.after(60, self._poll_native)
            except tk.TclError:
                pass

    def set_style(self, style: str) -> None:
        """Switch between bars / rings / classic and persist."""
        if style not in STRIP_STYLES or style == self.style:
            return
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["style"] = style
        save_config(cfg)
        was_classic = self._classic
        self.style = style
        if was_classic and not self._classic:
            if self._win_alive():
                self.win.destroy()
            self.win = None
            self.drag_mode = False
            self._start_native()
        elif not was_classic and self._classic:
            self._stop_native()
            self._create_window()
            self._validate_custom_pos()
            self._reposition()
        self._render_sig = None
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip redraw on style change failed")

    def _modern_items(self, s) -> list:
        u = s.usage
        rpt = s.report
        stale = u is not None and is_usage_stale(s)
        stale_txt = t("stale_mark")
        if stale:
            age = usage_age_seconds(s.last_api_fetch)
            if age >= 0:
                stale_txt = f"{stale_txt} {fmt_age(age)}"
        items: list[strip_render.Item] = []

        def quota(label: str, pct: float, mins: int, active: bool,
                  total_min: int) -> None:
            time_pct = None
            if stale:
                level, sub = "stale", stale_txt
            else:
                level = "danger" if pct >= 90 else "warn" if pct >= 75 else "ok"
                sub = (t("resets_in_short").format(left=fmt_minutes(mins))
                       if active else t("idle_short"))
                if active and total_min > 0:
                    # Same quantity as classic display mode 4: how much of the
                    # window's time has elapsed, to compare against usage.
                    time_pct = max(0.0, min(100.0,
                                            (total_min - mins) / total_min * 100))
            items.append(strip_render.Item(label, f"{pct:.0f}%", pct, sub,
                                           level, sub_warn=stale,
                                           time_pct=time_pct))

        if u is not None:
            if u.has_five_hour:  # Ultra has no 5h limit
                quota(t("lbl_5h_window"), u.five_hour_pct,
                      u.five_hour_minutes_to_reset, u.five_hour_active,
                      TOTAL_5H_MIN)
            quota(t("lbl_weekly"), u.seven_day_pct,
                  u.seven_day_minutes_to_reset, u.seven_day_active,
                  TOTAL_7D_MIN)
            for sl in u.scoped_limits[:3]:
                quota(sl.name, sl.pct, sl.minutes_to_reset, sl.active,
                      TOTAL_7D_MIN)
        if rpt is not None:
            items.append(strip_render.Item(
                t("today"), f"${rpt.today.cost_usd:,.2f}", None))
        if not items:
            items.append(strip_render.Item("Claude", t("loading"), None))
        return items

    def _render_modern(self, s) -> None:
        nat = self._native
        if nat is None:
            return
        items = self._modern_items(s)
        light = taskbar_is_light()
        sig = (tuple(items), self.style, light, round(nat.scale, 3),
               self._native_hover)
        if sig == self._render_sig:
            return
        self._render_sig = sig
        theme = strip_render.LIGHT if light else strip_render.DARK
        nat.update(strip_render.render(self.style, items, theme, nat.scale,
                                       hover=self._native_hover))

    def _popup_menu(self, x: int, y: int) -> None:
        self._rebuild_menu()
        try:
            self._menu.tk_popup(x, y)
        finally:
            self._menu.grab_release()

    def _create_window(self) -> None:
        """Build — or REBUILD — the strip's Toplevel + canvas + bindings + menu.

        Called at init, and again by the tick self-heal when the window has been
        destroyed by an external shell/session event. A borderless, topmost,
        transparent (overrideredirect + -transparentcolor) Toplevel is exactly
        the kind of window Windows tears down on a remote-desktop session switch
        (RDP / RustDesk), a display/DPI change, or an explorer.exe restart —
        leaving self.win dangling ("bad window path name"). Rebuilding restores
        the strip within ~1 s, no restart needed.
        """
        self.win = tk.Toplevel(self._parent_root)
        self.win.overrideredirect(True)          # no title bar / borders
        self.win.attributes("-topmost", True)    # above normal app windows
        self.win.attributes("-toolwindow", True)  # hide from Alt+Tab
        # Transparent backdrop: any pixel matching TRANSPARENT_KEY becomes fully
        # see-through; only the drawn text + outline remain.
        self.win.configure(bg=TRANSPARENT_KEY)
        self.win.attributes("-transparentcolor", TRANSPARENT_KEY)

        # Per-window render/layout state — reset for the fresh window.
        self.strip_w = STRIP_W
        self._render_sig: tuple | None = None
        self._applied_geom: tuple[int, int, int] | None = None
        self._tick_count = 0

        # Single Canvas lets us draw outlined text manually (tkinter Labels can't
        # do strokes); combined with -transparentcolor it gives the "floating
        # text" look.
        self.canvas = tk.Canvas(
            self.win, bg=TRANSPARENT_KEY,
            highlightthickness=0, borderwidth=0,
            width=STRIP_W, height=STRIP_H,
        )
        self.canvas.pack(fill="both", expand=True)

        # Click bindings on both canvas and window — outlined text covers most
        # pixels; clicks in transparent gaps fall through harmlessly.
        for widget in (self.canvas, self.win):
            widget.bind("<Button-1>", self._on_btn1_press)
            widget.bind("<B1-Motion>", self._on_btn1_motion)
            widget.bind("<ButtonRelease-1>", self._on_btn1_release)
            widget.bind("<Button-3>", self._on_right_click)

        # Right-click menu; entries (re)populated on each popup via _rebuild_menu
        # so labels follow the current UI language.
        self._menu = tk.Menu(self.win, tearoff=0, bg=PANEL, fg=FG,
                             activebackground=ACCENT, activeforeground="white",
                             borderwidth=0)

        # Preserve hidden state across a rebuild.
        if not self.visible:
            self.win.withdraw()

        # Dock under the taskbar as owner (Soda Music's approach) — after
        # this the taskbar can no longer bury us, so the bump loop goes quiet.
        self._owner_taskbar = 0
        self._ensure_taskbar_owner()

        # (Re)assert position + topmost. The burst counters post-create races:
        # the shell still settling at boot, or the taskbar reclaiming topmost
        # right after a session switch.
        self._reposition()
        for delay_ms in (300, 700, 1500, 3000, 6000, 10000):
            self.win.after(delay_ms, self._startup_bump)

    def _ensure_taskbar_owner(self) -> None:
        """(Re)attach the strip to the current taskbar as its owner.

        Cheap enough to run every tick: it only acts when the owner is missing
        or stale (explorer.exe restarted → new Shell_TrayWnd HWND)."""
        try:
            self.win.update_idletasks()
            hwnd = int(self.win.wm_frame(), 16)
        except (ValueError, tk.TclError):
            return
        taskbar = find_taskbar_hwnd()
        if taskbar and taskbar_owner_of(hwnd) == taskbar:
            self._owner_taskbar = taskbar
            return
        try:
            self._owner_taskbar = attach_to_taskbar(hwnd)
        except OSError:
            self._owner_taskbar = 0
        logging.getLogger(__name__).info(
            "strip: taskbar owner %s (hwnd=%#x)",
            f"{self._owner_taskbar:#x}" if self._owner_taskbar else "unavailable",
            hwnd)

    def _win_alive(self) -> bool:
        """True if the strip's Toplevel still exists (wasn't torn down externally)."""
        if self.win is None:
            return False
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _validate_custom_pos(self) -> None:
        """Snap a saved position back on-screen if it's fully off-screen (e.g.
        the previous run was on a now-disconnected monitor).

        Does NOT migrate off taskbar overlap — the user can intentionally pin
        the strip on top of the taskbar; _is_covered()-driven force-topmost
        keeps it visible there. Earlier builds aggressively moved positions
        above the taskbar, which we've now reverted because it broke a
        deliberate use case.
        """
        if self._custom_pos is None:
            return
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
        except tk.TclError:
            return
        x, y = self._custom_pos
        # Keep at least ~60 px visible horizontally, and don't fall off the
        # bottom of the screen entirely. Both bounds permit taskbar overlap.
        new_x = max(0, min(x, sw - 60))
        new_y = max(0, min(y, sh - STRIP_H))
        if (new_x, new_y) == (x, y):
            return
        log = logging.getLogger(__name__)
        log.info("strip: rescuing off-screen saved position (%d,%d) -> (%d,%d)",
                 x, y, new_x, new_y)
        self._custom_pos = (new_x, new_y)
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["x"] = new_x
        cfg["strip"]["y"] = new_y
        save_config(cfg)

    def _reposition(self) -> None:
        # Skip geometry update during a drag (mouse drives it). Topmost handling
        # is NOT done here anymore — _tick owns the topmost policy (A3), and
        # show() reasserts explicitly. Calling _force_topmost() every tick from
        # here was the source of the occasional blink: its off→on toggle could
        # momentarily drop the strip behind the taskbar.
        if not self._win_alive():
            return  # window torn down; the tick self-heal will rebuild it
        drag_active = self.drag_mode and self._drag_anchor is not None
        if drag_active:
            return
        if self._custom_pos is not None:
            x, y = self._custom_pos
        else:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            if self.side == "left":
                x = STRIP_SIDE_MARGIN
            else:
                x = sw - self.strip_w - STRIP_SIDE_MARGIN
            # Default sits ON the taskbar (centered in its band), NOT just
            # above it — see get_strip_default_y for why that's the only
            # placement maximized windows can't cover.
            y = get_strip_default_y(sh)
        # Only touch geometry when it actually changed — a no-op geometry() call
        # each tick is wasteful and can disturb the window.
        geom = (self.strip_w, x, y)
        if geom != self._applied_geom:
            self._applied_geom = geom
            self.win.geometry(f"{self.strip_w}x{STRIP_H}+{x}+{y}")

    def _force_topmost(self) -> None:
        """The standard tkinter-on-Windows 'bump trick': toggle -topmost off
        and back on, then lift(). This is the established Python community
        pattern for keeping a Toplevel reliably above other windows; raw
        SetWindowPos / ShowWindow / deiconify combinations are less reliable.
        """
        if not self.visible:
            return
        try:
            self.win.attributes("-topmost", False)
            self.win.attributes("-topmost", True)
            self.win.lift()
        except tk.TclError:
            pass

    def _force_topmost_winapi(self) -> None:
        """Backup to the tkinter bump trick: call SetWindowPos directly with
        HWND_TOPMOST. Sometimes wins z-order fights the tkinter -topmost
        toggle alone doesn't — notably right after the Win11 shell finishes
        initializing on a boot-time autostart and re-asserts the taskbar's
        topmost rank.

        Cheap (one kernel call), safe to invoke even when we're already
        on top, so we call it speculatively in the startup burst and
        defensively whenever _is_covered() returns True.
        """
        if not self.visible:
            return
        try:
            hwnd = int(self.win.wm_frame(), 16)
        except (ValueError, tk.TclError):
            return
        flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW
        try:
            _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags)
        except OSError:
            pass

    def _is_covered(self, only_by: int = 0) -> bool:
        """Detect whether another top-level window is in front of us.

        Strategy: sample 5 horizontally-spread points across our strip and ask
        WindowFromPoint who's on top at each. Walk each hit up to its root via
        GetAncestor(GA_ROOT), then compare against our own HWND.

        Why multi-point sampling: our backdrop uses `-transparentcolor`, so
        any pixel matching TRANSPARENT_KEY is click-through — WindowFromPoint
        at such a pixel returns whatever's behind us (a false negative for
        "are we visible"). Real text glyphs ARE opaque though, so as long as
        any sampled point lands on a glyph or its outline, we get a true hit.
        5 samples at 5%/25%/50%/75%/95% gives plenty of coverage for any
        non-trivial text layout.

        Returns True only if *no* sample point reports us — i.e., we're
        confidently covered. False if at least one point shows us on top.

        With `only_by` set, also return False as soon as some OTHER window
        (not `only_by`) is in front — e.g. a Start menu or flyout that is
        legitimately above the taskbar and that we shouldn't fight.
        """
        if not self.visible:
            return False
        try:
            our_hwnd = int(self.win.wm_frame(), 16)
            wx = self.win.winfo_x()
            wy = self.win.winfo_y()
            ww = self.strip_w
        except (ValueError, tk.TclError):
            return False
        cy = wy + STRIP_H // 2
        for frac in (0.05, 0.25, 0.5, 0.75, 0.95):
            cx = wx + int(ww * frac)
            pt = wintypes.POINT(cx, cy)
            top_hwnd = _user32.WindowFromPoint(pt)
            if not top_hwnd:
                continue
            root_hwnd = _user32.GetAncestor(top_hwnd, _GA_ROOT) or top_hwnd
            if int(root_hwnd) == our_hwnd:
                return False
            if only_by and int(root_hwnd) != only_by:
                return False
        return True

    def _startup_bump(self) -> None:
        """One-shot scheduled bump used by the post-launch burst (see __init__).

        Autostart from Windows' Startup folder fires us before the shell
        finishes initializing — the taskbar gets created AFTER us with
        HWND_TOPMOST, and silently buries us. The once-per-second tick
        bump eventually rescues us, but the user notices a missing strip
        for a couple of seconds. The burst calls this several times during
        the first ~10 s to win that race promptly.
        """
        if not self.visible:
            return
        # Retry the taskbar owner too: at boot the shell may not exist yet.
        self._ensure_taskbar_owner()
        if self._owner_taskbar:
            self._force_topmost_winapi()  # owned: no off→on toggle needed
            return
        self._force_topmost()
        self._force_topmost_winapi()

    def show(self) -> None:
        self.visible = True
        if not self._classic:
            if self._native is not None:
                self._native.show()
            return
        # Resurrect immediately if an external event destroyed the window while
        # it was hidden — otherwise show() would raise "bad window path name".
        if not self._win_alive():
            self._create_window()
        # Force geometry to be re-applied after a hide/show cycle.
        self._applied_geom = None
        self._reposition()
        try:
            self.win.deiconify()
        except tk.TclError:
            return
        # _reposition() no longer bumps topmost (A3) — do it explicitly here.
        self._force_topmost()

    def hide(self) -> None:
        self.visible = False
        if self._native is not None:
            self._native.hide()
        if self._win_alive():
            try:
                self.win.withdraw()
            except tk.TclError:
                pass

    def _on_show_window(self) -> None:
        """Menu command — always opens the main window regardless of drag mode."""
        self.on_left_click_cb()

    def _rebuild_menu(self) -> None:
        """Repopulate the right-click menu with current-language labels."""
        self._menu.delete(0, "end")
        self._menu.add_command(label=t("show_window"), command=self._on_show_window)
        self._menu.add_command(label=t("refresh_now"), command=self.orch.refresh_now)
        self._menu.add_command(label=t("settings"), command=self.on_settings_cb)
        self._menu.add_separator()
        self._menu.add_command(label=t("hide_strip"), command=self.hide)

    def _on_btn1_press(self, event) -> None:
        if self.drag_mode:
            # Capture both mouse origin and current window origin so motion
            # math is a simple delta.
            self._drag_anchor = (
                event.x_root, event.y_root,
                self.win.winfo_x(), self.win.winfo_y(),
            )
        else:
            self.on_left_click_cb()

    def _on_btn1_motion(self, event) -> None:
        if not (self.drag_mode and self._drag_anchor):
            return
        mx0, my0, wx0, wy0 = self._drag_anchor
        new_x = wx0 + (event.x_root - mx0)
        new_y = wy0 + (event.y_root - my0)
        # Keep at least ~60 px on-screen on each axis so the strip is always
        # grabbable. Y allowed all the way to the bottom — overlapping the
        # taskbar is supported; _tick()'s _is_covered() detection plus the
        # SetWindowPos backup keep us visible there.
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        new_x = max(-STRIP_W + 60, min(new_x, sw - 60))
        new_y = max(0, min(new_y, sh - STRIP_H))
        self.win.geometry(f"+{new_x}+{new_y}")

    def _on_btn1_release(self, _event) -> None:
        if self.drag_mode and self._drag_anchor is not None:
            new_x = self.win.winfo_x()
            new_y = self.win.winfo_y()
            self._custom_pos = (new_x, new_y)
            cfg = load_config()
            cfg.setdefault("strip", {})
            cfg["strip"]["x"] = new_x
            cfg["strip"]["y"] = new_y
            save_config(cfg)
        self._drag_anchor = None

    def set_drag_mode(self, enabled: bool) -> None:
        """Toggle drag-to-reposition mode. Visual cues: 4-way move cursor on
        hover, plus a thin ACCENT-colored outline rectangle drawn by _render."""
        if not self._classic:
            # Modern styles dock to the taskbar automatically; no free drag.
            self.drag_mode = False
            return
        self.drag_mode = enabled
        cursor = "fleur" if enabled else ""
        try:
            self.canvas.config(cursor=cursor)
            self.win.config(cursor=cursor)
        except tk.TclError:
            pass
        # Force a redraw so the outline rectangle (or its removal) shows up
        # immediately rather than waiting for the next tick.
        self._render_sig = None
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip redraw on drag toggle failed")

    def reset_position(self) -> None:
        """Clear the saved custom position and snap back to side defaults."""
        self._custom_pos = None
        cfg = load_config()
        if "strip" in cfg:
            cfg["strip"].pop("x", None)
            cfg["strip"].pop("y", None)
        save_config(cfg)
        self._reposition()

    def set_side(self, side: str) -> None:
        """Switch the default screen edge (left/right) and persist. Clears any
        saved drag position so the new side takes effect immediately (a saved
        x/y would otherwise override side placement)."""
        if side not in ("left", "right"):
            return
        self.side = side
        self._custom_pos = None
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["side"] = side
        cfg["strip"].pop("x", None)
        cfg["strip"].pop("y", None)
        save_config(cfg)
        if self._native is not None:
            self._native.set_side(side)
        # Force geometry recompute on the new side.
        self._applied_geom = None
        self._reposition()

    def set_show_background(self, enabled: bool) -> None:
        """Toggle the opaque dark backdrop behind the strip text. Persisted."""
        self.show_background = enabled
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["show_background"] = enabled
        save_config(cfg)
        self._render_sig = None
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip redraw on bg toggle failed")

    def set_display_mode(self, mode: int) -> None:
        """Switch display_mode (1/2/3/4) and persist. Triggers an immediate redraw."""
        if mode not in (1, 2, 3, 4):
            return
        self.display_mode = mode
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["display_mode"] = mode
        save_config(cfg)
        self._render_sig = None
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip redraw on mode change failed")

    def _on_right_click(self, event) -> None:
        self._popup_menu(event.x_root, event.y_root)

    def _tick(self) -> None:
        # The reschedule MUST be unconditional — it lives in `finally` AND fires
        # on the ROOT window (not self.win), so that NO exception, and not even a
        # destroyed strip window, can kill this self-rescheduling chain. A dead
        # chain = a permanently frozen/missing strip, which we make structurally
        # impossible.
        try:
            if not self._classic:
                # Modern styles: the widget thread owns placement + z-order;
                # we only refresh the picture (countdowns tick every minute).
                self._render(self.orch.snapshot())
                return  # `finally` still reschedules
            if not self._win_alive():
                # An external event (RDP/RustDesk session switch, display/DPI
                # change, explorer restart) tore the window down. Rebuild it —
                # this is the strip's self-heal and is why it comes back on its
                # own now instead of staying gone until a restart.
                logging.getLogger(__name__).warning(
                    "strip window vanished (external session/display event?); "
                    "recreating")
                self._create_window()
            self._render(self.orch.snapshot())
            if self.visible:
                self._tick_count += 1
                self._reposition()
                self._ensure_taskbar_owner()
                if self._owner_taskbar:
                    # Taskbar-owned: Windows keeps us above the taskbar by
                    # itself. No toggle bump (no blink), and we don't fight
                    # Start/flyouts that sit above the taskbar on purpose.
                    # Only nudge if the taskbar itself somehow ends up in
                    # front of us.
                    if self._is_covered(only_by=self._owner_taskbar):
                        self._force_topmost_winapi()
                # Legacy topmost policy (A3), used when no taskbar owner
                # could be attached (shell not up yet, exotic shells)
                elif self._is_covered():
                    self._force_topmost()
                    self._force_topmost_winapi()
                elif self._tick_count % 20 == 0:
                    self._force_topmost_winapi()
        except Exception:
            logging.getLogger(__name__).exception("strip tick failed")
        finally:
            # Reschedule on the ROOT, never on self.win — the strip window may be
            # destroyed (that's the bug we're healing); the root persists for the
            # life of the app, so the heal loop always survives.
            try:
                self._parent_root.after(1000, self._tick)
            except tk.TclError:
                pass

    # Outline offsets — 4 cardinal directions, 1px out. (Diagonals add 4 more
    # canvas items per glyph but visually almost no improvement; 4-way is the
    # right cost/quality tradeoff.)
    _OUTLINE_OFFSETS = ((-1, 0), (1, 0), (0, -1), (0, 1))

    def _draw_text(self, x: int, y: int, text: str, fg: str,
                   font: tkfont.Font) -> int:
        """Draw text with a 1px black outline at canvas pixel (x,y), anchored
        west (left-of-baseline-center). Returns the advance width in pixels so
        the caller can position the next piece."""
        for dx, dy in self._OUTLINE_OFFSETS:
            self.canvas.create_text(x + dx, y + dy, text=text, fill=TEXT_OUTLINE,
                                    font=font, anchor="w")
        self.canvas.create_text(x, y, text=text, fill=fg, font=font, anchor="w")
        return font.measure(text)

    def _append_quota_parts(self, parts: list, label: str, quota_pct: float,
                            mins_remaining: int, total_window_min: int,
                            active: bool = True, stale: bool = False) -> None:
        """Append the (text, color, font) pieces for one quota's display in the
        currently selected display_mode. `label` is the already-resolved display
        text, NOT an i18n key — model-scoped buckets pass a server-provided name
        through here, and a name that happened to equal a dictionary key (say a
        model called "today") would otherwise render as that key's translation.
        Caller is responsible for any leading separator. Pieces are appended in
        reading order (caller packs left-to-right OR right-to-left based on
        STRIP_SIDE).

        When `active` is False (no live window — the endpoint reports 0% with no
        resets_at), render just the quota% regardless of display_mode and skip
        the time suffix entirely: "(0m)" / "/0%" / "/100%" would all be lies.

        When `stale` is True the data is old (pipeline stalled / token expired):
        grey the quota out and drop the time suffix — the elapsed/remaining parts
        would be computed from a stale resets_at and are meaningless. The caller
        appends a visible "stale" marker so the greying isn't ambiguous.
        """
        parts.append((label + " ", FG_DIM, self.font_dim))
        quota_color = FG_DIM if stale else color_for_pct(quota_pct)
        if stale or not active or self.display_mode == 1:
            # Stale / inactive / compact mode 1: just the quota%
            parts.append((f"{quota_pct:.0f}%", quota_color, self.font_main))
        elif self.display_mode == 2:
            # Mode 2: quota% (time remaining)
            parts.append((f"{quota_pct:.0f}%", quota_color, self.font_main))
            parts.append((f" ({fmt_minutes(mins_remaining)})", FG_DIM, self.font_dim))
        elif self.display_mode == 3:
            # Mode 3: quota% / time-remaining-as-pct-of-total-window
            # (second number counts DOWN as window approaches reset)
            time_pct = 0
            if total_window_min > 0:
                time_pct = max(0, min(100, int(mins_remaining / total_window_min * 100)))
            parts.append((f"{quota_pct:.0f}%", quota_color, self.font_main))
            parts.append(("/", FG_DIM, self.font_dim))
            parts.append((f"{time_pct}%", FG, self.font_main))
        else:  # mode 4
            # Mode 4: quota% / elapsed-time-as-pct-of-total-window
            # (second number counts UP — same direction as quota%, so you can
            # eyeball "am I burning faster than time is passing": elapsed% <
            # quota% means yes.)
            elapsed_pct = 0
            if total_window_min > 0:
                mins_elapsed = total_window_min - mins_remaining
                elapsed_pct = max(0, min(100, int(mins_elapsed / total_window_min * 100)))
            parts.append((f"{quota_pct:.0f}%", quota_color, self.font_main))
            parts.append(("/", FG_DIM, self.font_dim))
            parts.append((f"{elapsed_pct}%", FG, self.font_main))

    def _render(self, s) -> None:
        if not self._classic:
            self._render_modern(s)
            return
        if not self._win_alive():
            return
        u = s.usage
        rpt = s.report

        # Build the ordered list of (text, color, font) pieces. Caption pieces
        # use FG_DIM; value pieces use the threshold-based color. Layout depends
        # on the current display_mode (selected from the tray Settings submenu).
        stale = u is not None and is_usage_stale(s)
        parts: list[tuple[str, str, tkfont.Font]] = []
        if u is not None:
            if u.has_five_hour:  # Ultra has no 5h limit — omit it entirely
                self._append_quota_parts(parts, t("5h"), u.five_hour_pct,
                                         u.five_hour_minutes_to_reset, TOTAL_5H_MIN,
                                         active=u.five_hour_active, stale=stale)
                parts.append(("   ·   ", FG_DIM, self.font_dim))
            self._append_quota_parts(parts, t("7d"), u.seven_day_pct,
                                     u.seven_day_minutes_to_reset, TOTAL_7D_MIN,
                                     active=u.seven_day_active, stale=stale)
            # Model-scoped weekly buckets (Fable, …) — the server-provided name
            # is the label verbatim. Capped so the strip stays glanceable.
            for sl in u.scoped_limits[:3]:
                parts.append(("   ·   ", FG_DIM, self.font_dim))
                self._append_quota_parts(parts, sl.name, sl.pct,
                                         sl.minutes_to_reset, TOTAL_7D_MIN,
                                         active=sl.active, stale=stale)
        if rpt is not None:
            if u is not None:
                parts.append(("   ·   ", FG_DIM, self.font_dim))
            parts.append((t("today") + " ", FG_DIM, self.font_dim))
            parts.append((f"${rpt.today.cost_usd:,.2f}", FG, self.font_main))
        if stale:
            # An unmistakable, glanceable marker (orange) so greyed quota numbers
            # read as "old data" — never as a frozen app. Include the age.
            age = usage_age_seconds(s.last_api_fetch)
            mark = t("stale_mark")
            if age >= 0:
                mark = f"{mark} {fmt_age(age)}"
            parts.append(("   ·   ", FG_DIM, self.font_dim))
            parts.append((mark, WARN, self.font_dim))
        if not parts:
            return  # nothing to draw yet (initial state before first data lands)

        # Anti-flicker: skip the whole redraw when nothing visible changed. The
        # signature captures every input to the canvas drawing — the text/color/
        # font of each piece plus the two boolean visual modes. _tick runs this
        # every 1s; without the short-circuit we'd clear and repaint identical
        # pixels each time, which is both wasteful and a visible flicker source.
        # Setters that must force a redraw reset self._render_sig to None.
        sig = (
            tuple((text, color, str(font)) for text, color, font in parts),
            self.drag_mode,
            self.show_background,
        )
        if sig == self._render_sig:
            return
        self._render_sig = sig

        # Clear previous frame and re-draw from scratch.
        self.canvas.delete("all")

        # Total content width: sum of all advances. Padding leaves room for the
        # 1px outline on both ends so the leftmost/rightmost glyph isn't clipped.
        pad_x = 4
        total_w = sum(font.measure(text) for text, _, font in parts) + pad_x * 2
        y_center = STRIP_H // 2

        # Optional opaque dark backdrop — drawn first so text + outlines layer on top.
        if self.show_background:
            self.canvas.create_rectangle(
                0, 0, total_w, STRIP_H,
                fill=BG, outline="")

        # Drag-mode visual cue: thin ACCENT outline around the content area.
        # Done before text so text draws over the corners.
        if self.drag_mode:
            self.canvas.create_rectangle(
                0, 0, total_w - 1, STRIP_H - 1,
                outline=ACCENT, width=1, fill="")

        x = pad_x
        for text, color, font in parts:
            x += self._draw_text(x, y_center, text, color, font)

        # Resize canvas + window to the actual content width. Only act if it
        # changed materially — avoids redundant geometry calls on every tick
        # when values are stable.
        if abs(total_w - self.strip_w) >= 2:
            self.strip_w = total_w
            self.canvas.config(width=total_w, height=STRIP_H)
            if not (self.drag_mode and self._drag_anchor is not None):
                self._reposition()


# Example strings shown under each display-mode radio in Settings → Strip, so
# the user can preview what each mode looks like before picking it.
_MODE_EXAMPLES = {
    1: "5h 44%",
    2: "5h 44% (2h 13m)",
    3: "5h 44%/56%",
    4: "5h 44%/44%",
}


class SettingsWindow:
    """A single real settings window (Toplevel) replacing the deep tray submenus.

    Singleton: open_settings() lifts/focuses an existing instance instead of
    making a second one. Constructed/opened ONLY on the tk main thread.
    Three tabs (General / Strip / About) driven by a custom segmented tab bar
    (flat tk.Buttons) — NOT ttk.Notebook, whose native tabs ignore the dark theme.
    """

    _instance: "SettingsWindow | None" = None

    @classmethod
    def open(cls, root: tk.Tk, orch: Orchestrator, strip,
             on_lang_change: callable) -> "SettingsWindow":
        """Open the settings window, or lift the existing one to the front."""
        inst = cls._instance
        if inst is not None and inst._alive():
            inst.win.deiconify()
            inst.win.lift()
            inst.win.focus_force()
            return inst
        inst = cls(root, orch, strip, on_lang_change)
        cls._instance = inst
        return inst

    def __init__(self, root: tk.Tk, orch: Orchestrator, strip,
                 on_lang_change: callable) -> None:
        self.root = root
        self.orch = orch
        self.strip = strip
        self.on_lang_change = on_lang_change
        self.active_tab = "general"

        self.win = tk.Toplevel(root)
        # Build withdrawn, then deiconify after layout to avoid a placement flash.
        self.win.withdraw()
        self.win.title(t("settings_title"))
        self.win.configure(bg=BG)
        # Borderless like the main window; +45px height for the custom title bar.
        self.win.overrideredirect(True)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"430x515+{(sw - 430) // 2}+{max(0, (sh - 515) // 2)}")
        self.win.resizable(False, False)
        # NOTE: no transient(root) — a transient + overrideredirect Toplevel
        # fails to map on Windows (stays 1x1 and never becomes viewable).
        # Losing transient also loses the owned-window z-order it used to
        # provide, and the main window is topmost by default — a non-topmost
        # Settings would render UNDER it with no way to raise it (no taskbar
        # button either, since we're borderless). So put Settings in the
        # topmost band itself; it's an explicitly-opened, Escape-closable
        # dialog, so staying on top while open is the expected behavior.
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda _e: self.close())

        # Tk variables backing the live controls. Recreated each rebuild so a
        # language switch (which rebuilds) starts from current persisted state.
        self.tab_buttons: dict[str, tk.Button] = {}
        self.tab_underlines: dict[str, tk.Frame] = {}
        self.content_frames: dict[str, tk.Frame] = {}

        self._build_shell()
        self.rebuild()
        self.show_tab(self.active_tab)

        self.win.update_idletasks()
        self.win.deiconify()
        apply_frameless_chrome(self.win)
        self.win.lift()
        self.win.focus_force()

    # ---- lifecycle ----

    def _alive(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def close(self) -> None:
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if SettingsWindow._instance is self:
            SettingsWindow._instance = None

    # ---- shell (tab bar + content host) — built once ----

    def _build_shell(self) -> None:
        # Custom title bar (the window is borderless) — draggable, close-only.
        tb = tk.Frame(self.win, bg=BG)
        tb.pack(fill="x", side="top", padx=(16, 6), pady=(8, 4))
        self.title_lbl = tk.Label(tb, text=t("settings_title"), bg=BG, fg=FG,
                                  font=("Segoe UI Semibold", 12))
        self.title_lbl.pack(side="left")
        titlebar_btn(tb, GLYPH_CLOSE, self.close,
                     hover_fg="#ffffff", hover_bg="#b3404a").pack(side="right")
        bind_window_drag(self.win, tb, self.title_lbl)

        bar = tk.Frame(self.win, bg=BG)
        bar.pack(fill="x", side="top")
        for key in ("general", "strip", "about"):
            col = tk.Frame(bar, bg=BG)
            col.pack(side="left", fill="x", expand=True)
            btn = tk.Button(
                col, text="", bg=BG, fg=FG_DIM, bd=0,
                font=("Segoe UI Semibold", 10), cursor="hand2",
                activebackground=PANEL, activeforeground=FG,
                command=lambda k=key: self.show_tab(k),
            )
            btn.pack(fill="x", ipady=8)
            underline = tk.Frame(col, bg=BG, height=2)
            underline.pack(fill="x")
            self.tab_buttons[key] = btn
            self.tab_underlines[key] = underline
        # Thin separator under the tab bar.
        tk.Frame(self.win, bg=BORDER, height=1).pack(fill="x")

        # Content host — the three tab frames are packed/forgotten inside it.
        self.host = tk.Frame(self.win, bg=BG)
        self.host.pack(fill="both", expand=True)

    # ---- content (rebuilt on language change) ----

    def rebuild(self) -> None:
        """(Re)build all tab content in place. Called on construction and after a
        language switch so every label reflects the current language immediately."""
        # Tab bar labels.
        for key, label_key in (("general", "tab_general"),
                               ("strip", "tab_strip"),
                               ("about", "tab_about")):
            self.tab_buttons[key].config(text=t(label_key))
        self.win.title(t("settings_title"))
        self.title_lbl.config(text=t("settings_title"))

        # Drop old content frames.
        for fr in self.content_frames.values():
            fr.destroy()
        self.content_frames.clear()

        self.content_frames["general"] = self._build_general()
        self.content_frames["strip"] = self._build_strip_tab()
        self.content_frames["about"] = self._build_about()
        # Re-apply the active tab so the rebuilt frame is shown + tab styled.
        self.show_tab(self.active_tab)

    def show_tab(self, key: str) -> None:
        self.active_tab = key
        for k, fr in self.content_frames.items():
            if k == key:
                fr.pack(fill="both", expand=True)
            else:
                # Unconditional — winfo_ismapped() lags until the next idle
                # pass, which could leave two tabs packed simultaneously.
                fr.pack_forget()
        # Tab styling: active = ACCENT fg + PANEL bg + ACCENT underline.
        for k, btn in self.tab_buttons.items():
            if k == key:
                btn.config(fg=ACCENT, bg=PANEL, activebackground=PANEL)
                self.tab_underlines[k].config(bg=ACCENT)
            else:
                btn.config(fg=FG_DIM, bg=BG, activebackground=PANEL)
                self.tab_underlines[k].config(bg=BG)

    # ---- small styled-control helpers ----

    def _section(self, parent: tk.Frame, text: str) -> None:
        tk.Label(parent, text=text, bg=BG, fg=FG_DIM,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(12, 4))

    def _checkbox(self, parent: tk.Frame, text: str, var: tk.BooleanVar,
                  cmd: callable) -> tk.Checkbutton:
        cb = tk.Checkbutton(
            parent, text=text, variable=var, command=cmd,
            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
            activeforeground=FG, font=("Segoe UI", 10), anchor="w",
            highlightthickness=0, bd=0, cursor="hand2",
        )
        cb.pack(anchor="w", pady=2)
        return cb

    def _radio(self, parent: tk.Frame, text: str, var: tk.Variable, value,
               cmd: callable) -> tk.Radiobutton:
        rb = tk.Radiobutton(
            parent, text=text, variable=var, value=value, command=cmd,
            bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
            activeforeground=FG, font=("Segoe UI", 10), anchor="w",
            highlightthickness=0, bd=0, cursor="hand2",
        )
        rb.pack(anchor="w", pady=1)
        return rb

    def _flat_button(self, parent: tk.Frame, text: str, cmd: callable) -> tk.Button:
        btn = tk.Button(
            parent, text=text, command=cmd, bg=PANEL, fg=FG, bd=0,
            font=("Segoe UI", 9), activebackground=BORDER, activeforeground=FG,
            cursor="hand2", padx=12, pady=5,
        )
        btn.pack(anchor="w", pady=4)
        return btn

    def _run_async(self, work: callable, on_done: callable) -> None:
        """Run blocking work() on a daemon thread, then deliver its result to
        on_done() on the tk main thread. Used so the autostart PowerShell calls
        (which can take seconds) never run on the main thread and freeze the UI.
        Safely no-ops if the settings window is gone by the time work finishes."""
        def runner() -> None:
            try:
                result = work()
            except Exception:
                logging.getLogger(__name__).exception("settings async work failed")
                result = None

            def deliver() -> None:
                if not self._alive():
                    return
                try:
                    on_done(result)
                except Exception:
                    logging.getLogger(__name__).exception("settings async on_done failed")
            try:
                self.win.after(0, deliver)
            except (tk.TclError, RuntimeError):
                pass
        threading.Thread(target=runner, name="settings-async", daemon=True).start()

    # ---- Tab 1: General ----

    def _build_general(self) -> tk.Frame:
        fr = tk.Frame(self.host, bg=BG)
        pad = 16

        inner = tk.Frame(fr, bg=BG)
        inner.pack(fill="both", expand=True, padx=pad, pady=4)

        # Language
        self._section(inner, t("language"))
        self.lang_var = tk.StringVar(value=get_app_language())
        self._radio(inner, t("lang_en"), self.lang_var, "en", self._on_lang)
        self._radio(inner, t("lang_zh"), self.lang_var, "zh", self._on_lang)

        # Run at startup. Both the detection and the toggle shell out to
        # PowerShell (per .lnk in the Startup folder), which can take seconds —
        # so they run OFF the main thread. The checkbox starts disabled and is
        # enabled once the async detection returns; opening Settings never blocks.
        self._section(inner, t("run_at_startup"))
        self.startup_var = tk.BooleanVar(value=False)
        self.startup_cb = self._checkbox(inner, t("run_at_startup"),
                                         self.startup_var, self._on_startup_toggle)
        self.startup_cb.config(state="disabled")
        self._run_async(is_autostart_enabled, self._apply_startup_detected)

        # Version + project page
        self._section(inner, t("version_label"))
        tk.Label(inner, text=f"v{__version__}", bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(anchor="w")
        self._flat_button(inner, t("project_page"), self._on_project_page)
        return fr

    def _on_lang(self) -> None:
        set_app_language(self.lang_var.get())
        # Rebuild our own content so labels update immediately, then notify the
        # rest of the app (strip relabel, etc).
        self.rebuild()
        try:
            self.on_lang_change()
        except Exception:
            logging.getLogger(__name__).exception("on_lang_change failed")

    def _apply_startup_detected(self, enabled) -> None:
        """Async callback: reflect the detected autostart state and re-enable the
        checkbox (it was disabled while the PowerShell scan ran off-thread)."""
        self.startup_var.set(bool(enabled))
        try:
            self.startup_cb.config(state="normal")
        except tk.TclError:
            pass

    def _on_startup_toggle(self) -> None:
        want = bool(self.startup_var.get())
        # create/remove also shell out to PowerShell — run off-thread, and lock
        # the checkbox until it completes so a fast double-click can't race.
        try:
            self.startup_cb.config(state="disabled")
        except tk.TclError:
            pass
        work = (lambda: create_autostart_entry() is not None) if want else remove_autostart_entry

        def done(ok) -> None:
            if not ok:
                self.startup_var.set(not want)  # revert to reflect reality
            try:
                self.startup_cb.config(state="normal")
            except tk.TclError:
                pass
        self._run_async(work, done)

    def _on_project_page(self) -> None:
        try:
            webbrowser.open(PROJECT_URL)
        except Exception:
            logging.getLogger(__name__).exception("failed to open project page")

    # ---- Tab 2: Strip ----

    def _build_strip_tab(self) -> tk.Frame:
        fr = tk.Frame(self.host, bg=BG)
        pad = 16
        inner = tk.Frame(fr, bg=BG)
        inner.pack(fill="both", expand=True, padx=pad, pady=4)

        strip = self.strip

        # Visibility
        self.strip_show_var = tk.BooleanVar(value=bool(strip and strip.visible))
        self._checkbox(inner, t("strip_show"), self.strip_show_var,
                       self._on_strip_show)

        # Style (bars / rings are the native taskbar widget; classic = old strip)
        self._section(inner, t("strip_style"))
        cur_style = strip.style if strip else load_config().get(
            "strip", {}).get("style", STRIP_STYLE_DEFAULT)
        self.strip_style_var = tk.StringVar(value=cur_style)
        for st in STRIP_STYLES:
            self._radio(inner, t(f"style_{st}"), self.strip_style_var, st,
                        self._on_strip_style)

        # Screen position
        self._section(inner, t("strip_screen_pos"))
        cur_side = strip.side if strip else load_config().get("strip", {}).get("side", "left")
        self.strip_side_var = tk.StringVar(value=cur_side)
        self._radio(inner, t("pos_left"), self.strip_side_var, "left", self._on_strip_side)
        self._radio(inner, t("pos_right"), self.strip_side_var, "right", self._on_strip_side)

        # Everything below only affects the classic text strip.
        self._section(inner, t("classic_only"))
        self.strip_bg_var = tk.BooleanVar(value=bool(strip and strip.show_background))
        self._checkbox(inner, t("strip_opaque_bg"), self.strip_bg_var,
                       self._on_strip_bg)

        # Display mode (radios with example strings)
        self._section(inner, t("strip_display_mode"))
        self.strip_mode_var = tk.IntVar(value=(strip.display_mode if strip else 1))
        for mode in (1, 2, 3, 4):
            self._radio(inner, t(f"mode_{mode}"), self.strip_mode_var, mode,
                        self._on_strip_mode)
            tk.Label(inner, text=_MODE_EXAMPLES[mode], bg=BG, fg=FG_DIM,
                     font=("Segoe UI", 8)).pack(anchor="w", padx=(24, 0))

        # Action buttons (drag toggle + reset)
        row = tk.Frame(inner, bg=BG)
        row.pack(fill="x", pady=(12, 0))
        self.drag_btn = tk.Button(
            row, text=self._drag_label(), command=self._on_strip_drag,
            bg=PANEL, fg=FG, bd=0, font=("Segoe UI", 9),
            activebackground=BORDER, activeforeground=FG, cursor="hand2",
            padx=12, pady=5,
        )
        self.drag_btn.pack(side="left", padx=(0, 8))
        tk.Button(
            row, text=t("strip_reset_pos"), command=self._on_strip_reset,
            bg=PANEL, fg=FG, bd=0, font=("Segoe UI", 9),
            activebackground=BORDER, activeforeground=FG, cursor="hand2",
            padx=12, pady=5,
        ).pack(side="left")
        return fr

    def _drag_label(self) -> str:
        on = bool(self.strip and self.strip.drag_mode)
        return t("strip_drag_on") if on else t("strip_drag_off")

    def _marshal(self, fn) -> None:
        """Strip mutations must run on the tk main thread; we're already on it
        here (Settings opens on main thread), but route through after(0,...) to
        match the established pattern and stay safe if called otherwise."""
        self.root.after(0, fn)

    def _on_strip_show(self) -> None:
        if not self.strip:
            return
        self._marshal(self.strip.show if self.strip_show_var.get() else self.strip.hide)

    def _on_strip_bg(self) -> None:
        if self.strip:
            self._marshal(lambda: self.strip.set_show_background(bool(self.strip_bg_var.get())))

    def _on_strip_style(self) -> None:
        if self.strip:
            self._marshal(lambda: self.strip.set_style(self.strip_style_var.get()))

    def _on_strip_side(self) -> None:
        if self.strip:
            self._marshal(lambda: self.strip.set_side(self.strip_side_var.get()))

    def _on_strip_mode(self) -> None:
        if self.strip:
            self._marshal(lambda: self.strip.set_display_mode(int(self.strip_mode_var.get())))

    def _on_strip_drag(self) -> None:
        if not self.strip:
            return
        new_val = not self.strip.drag_mode
        self._marshal(lambda: self.strip.set_drag_mode(new_val))
        # Relabel after the toggle is applied.
        self.root.after(0, lambda: self.drag_btn.config(text=self._drag_label()))

    def _on_strip_reset(self) -> None:
        if self.strip:
            self._marshal(self.strip.reset_position)

    # ---- Tab 3: About ----

    def _build_about(self) -> tk.Frame:
        fr = tk.Frame(self.host, bg=BG)
        pad = 16
        inner = tk.Frame(fr, bg=BG)
        inner.pack(fill="both", expand=True, padx=pad, pady=8)
        tk.Label(inner, text=f"{t('win_title')}  v{__version__}", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(0, 8))
        tk.Label(inner, text=t("about_blurb"), bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9), justify="left", anchor="w",
                 wraplength=390).pack(anchor="w", fill="x")
        return fr


class TrayApp:
    """pystray wrapper. Tray icon shows 5h %, menu toggles window."""

    def __init__(self, orch: Orchestrator, window: FloatingWindow,
                 strip: TaskbarStrip | None = None) -> None:
        self.orch = orch
        self.window = window
        self.strip = strip
        self._stop_callback: callable = lambda: None
        self.icon = pystray.Icon(
            "claude-usage",
            render_tray_icon(None),
            f"Claude Usage v{__version__}",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        # Slim top-level menu. Most configuration moved into the real Settings
        # window (opened by the Settings… item); only the daily-driver actions
        # and a quick strip toggle + display-mode picker stay in the tray.
        return pystray.Menu(
            pystray.MenuItem(lambda _i: t("show_window"), self._on_show, default=True),
            pystray.MenuItem(lambda _i: t("refresh_now"),
                             lambda _i: self.orch.refresh_now()),
            pystray.MenuItem(lambda _i: t("settings"),
                             lambda _i, _it: self._open_settings()),
            pystray.MenuItem(
                lambda _i: t("show_strip"),
                lambda _i, _it: self._on_strip_toggle(),
                checked=lambda _it: bool(self.strip and self.strip.visible),
            ),
            pystray.MenuItem(lambda _i: t("display_mode"), self._build_display_mode_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _i: t("quit"), self._on_quit),
        )

    def _build_display_mode_menu(self) -> pystray.Menu:
        """Radio-style picker for the four strip layouts (kept for quick access)."""
        def make(mode: int, label_key: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                lambda _i: t(label_key),
                lambda _i, _it: self._on_set_display_mode(mode),
                checked=lambda _it: bool(self.strip and self.strip.display_mode == mode),
                radio=True,
            )
        return pystray.Menu(
            make(1, "mode_1"), make(2, "mode_2"),
            make(3, "mode_3"), make(4, "mode_4"),
        )

    def _open_settings(self) -> None:
        """Open (or focus) the Settings window. Must run on the tk main thread."""
        self.window.root.after(0, lambda: SettingsWindow.open(
            self.window.root, self.orch, self.strip,
            on_lang_change=self._relabel_strip))

    def _relabel_strip(self) -> None:
        """Force an immediate strip redraw so its labels reflect a just-changed
        language without waiting for the next natural tick."""
        if self.strip is not None:
            self.strip._render_sig = None
            try:
                self.strip._render(self.orch.snapshot())
            except Exception:
                logging.getLogger(__name__).exception("strip relabel failed")

    def _on_strip_toggle(self) -> None:
        if self.strip is None:
            return
        if self.strip.visible:
            self.window.root.after(0, self.strip.hide)
        else:
            self.window.root.after(0, self.strip.show)

    def _on_set_display_mode(self, mode: int) -> None:
        if self.strip is None:
            return
        self.window.root.after(0, lambda: self.strip.set_display_mode(mode))

    def _on_show(self, _icon=None, _item=None) -> None:
        # tkinter calls must happen on the main thread.
        self.window.root.after(0, self.window.show)

    def _on_quit(self, _icon=None, _item=None) -> None:
        self.icon.stop()
        self._stop_callback()

    # Win32 NOTIFYICONDATA.szTip is a 128-WCHAR buffer including the NUL, so a
    # longer tooltip is rejected and the tray silently keeps showing the last
    # accepted one. Adding a line per model-scoped bucket made overrun reachable.
    TOOLTIP_MAX = 127

    def update_icon(self, pct: float, tooltip: str) -> None:
        try:
            self.icon.icon = render_tray_icon(pct)
            self.icon.title = tooltip[:self.TOOLTIP_MAX]
        except Exception:
            pass

    def run_detached(self) -> None:
        """Start the tray in its own thread so the main thread can run tk.mainloop."""
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()


def notify(kind: str, pct: float) -> None:
    """Pop a Windows toast notification for a threshold crossing.

    kind is "5h_<t>", "7d_<t>", or "scoped:<name>:<t>" for model-scoped weekly
    buckets (see Orchestrator._check_thresholds).
    """
    if kind.startswith("scoped:"):
        # Split from the RIGHT: the trailing threshold is a bare int and can
        # never contain a colon, but the bucket name is server-controlled free
        # text that might (e.g. "Claude: Fable").
        name = kind[len("scoped:"):].rsplit(":", 1)[0]
        window_label = t("toast_model_weekly").format(name=name)
    elif kind.startswith("5h"):
        window_label = t("toast_5h_window")
    else:
        window_label = t("toast_weekly")
    title = t("toast_title").format(window=window_label, pct=f"{pct:.0f}")
    body = t("toast_body_default")
    if pct >= 95:
        body = t("toast_body_95")
    elif pct >= 90:
        body = t("toast_body_90")
    toast = Notification(
        app_id="Claude Usage",
        title=title,
        msg=body,
        duration="short",
    )
    toast.set_audio(audio.Default, loop=False)
    try:
        toast.show()
    except Exception as e:
        logging.getLogger(__name__).warning("toast failed: %s", e)


def _setup_logging() -> None:
    """Console + rotating FILE logging. pythonw.exe has no stderr, so without a
    file handler every diagnostic (orchestrator warnings, tick-callback
    tracebacks routed via report_callback_exception) is silently lost — which is
    exactly why the last freeze had to be diagnosed externally with py-spy. Keep
    it small and self-rotating (3 x 512KB) next to the script."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    sh = logging.StreamHandler()          # no-op under pythonw; handy via python.exe
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        from logging.handlers import RotatingFileHandler
        log_path = Path(__file__).with_name("cc-usage-tray.log")
        fh = RotatingFileHandler(log_path, maxBytes=512 * 1024, backupCount=3,
                                 encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("file logging unavailable: %s", e)


def main() -> int:
    _setup_logging()
    logging.getLogger(__name__).info("cc-usage-tray v%s starting", __version__)

    # Single-instance guard. A named mutex in the Local namespace is per-session;
    # if it already exists another copy of us is running (common when autostart
    # and a manual launch race, or the user clicks the shortcut twice). Bail
    # quietly — no toast, because winotify spawns powershell and we want this
    # early exit to stay dependency-free and instant. The handle intentionally
    # leaks: the OS frees it on process exit, which is exactly when we want the
    # mutex released.
    _kernel32.CreateMutexW(None, False, "Local\\cc-usage-tray-singleton")
    if _kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        logging.getLogger(__name__).info(
            "another instance is already running; exiting")
        return 0

    # Restore the saved language before any UI is built — menu and strip both
    # read it at render time, so setting it here makes the first frame correct.
    global _current_lang
    saved_lang = load_config().get("language")
    if isinstance(saved_lang, str) and saved_lang in LANGUAGES:
        _current_lang = saved_lang

    orch = Orchestrator()
    # Wire callbacks AFTER constructing window+tray so we can reference them.
    window = FloatingWindow(orch, on_close=lambda: None)

    # Route tkinter callback exceptions to the log file. The periodic tick chains
    # already guard + reschedule in finally, but one-off event handlers (clicks,
    # binds, menu commands) surface their exceptions here — capture them so the
    # next incident is diagnosable from the log rather than needing py-spy.
    def _report_cb_exc(exc, val, tb) -> None:
        logging.getLogger("tkinter").error(
            "uncaught callback exception", exc_info=(exc, val, tb))
    window.root.report_callback_exception = _report_cb_exc

    # Strip's right-click "Settings…" opens the same singleton SettingsWindow as
    # the tray; relabel the strip on a language change made from there.
    def open_settings() -> None:
        def _o() -> None:
            sw = SettingsWindow.open(window.root, orch, strip,
                                     on_lang_change=relabel_strip)
            return sw
        window.root.after(0, _o)

    def relabel_strip() -> None:
        strip._render_sig = None
        try:
            strip._render(orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip relabel failed")

    # Strip is a Toplevel parented to window.root — shares the tk main loop.
    strip = TaskbarStrip(window.root, orch, on_left_click=window.show,
                         on_settings=open_settings)
    tray = TrayApp(orch, window, strip=strip)

    # When state changes, update tray badge with current 5h%.
    def on_change() -> None:
        s = orch.snapshot()
        if s.usage is not None:
            u = s.usage
            # Badge the 5h% when the plan has one; on Ultra (no 5h limit) the
            # weekly limit is the binding one, so badge that instead.
            pct = u.five_hour_pct if u.has_five_hour else u.seven_day_pct
            tooltip = f"{t('win_title')} v{__version__}\n"
            if u.has_five_hour:
                tooltip += (f"5h: {u.five_hour_pct:.0f}%  resets "
                            f"{fmt_minutes(u.five_hour_minutes_to_reset)}\n")
            tooltip += (f"7d: {u.seven_day_pct:.0f}%  resets "
                        f"{fmt_minutes(u.seven_day_minutes_to_reset)}")
            # Bounded so truncation (see TrayApp.TOOLTIP_MAX) drops whole
            # buckets rather than cutting a line mid-number.
            for sl in s.usage.scoped_limits[:FloatingWindow.SCOPED_ROWS_MAX]:
                tooltip += f"\n{sl.name[:24]}: {sl.pct:.0f}%"
            tray.update_icon(pct, tooltip)

    orch._on_change = on_change
    orch._on_alert = notify
    orch.start()
    tray._stop_callback = window.root.quit
    tray.run_detached()

    # Debug/verification aid: --show-window pops the floating window shortly after
    # startup so a launched instance is immediately visible.
    if "--show-window" in sys.argv:
        window.root.after(400, window.show)

    # Run tkinter main loop on main thread. Window is initially hidden;
    # user clicks tray → "Show window" to make it visible.
    try:
        window.root.mainloop()
    finally:
        orch.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
