"""Claude Usage Tray + Floating Window.

Launch with `python app.py`. Tray icon is always present; the window can be shown,
hidden, or set to always-on-top via the tray menu (or by closing/showing it).
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
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

# ---- i18n ----
# Module-level current-language state. set_app_language() rebinds it; t() looks
# it up. Menu items use callable `text=` so they re-evaluate t() every time the
# menu is shown, meaning a language switch takes effect on next menu open with
# no menu rebuild. Strip text picks up the new language on its next render tick.
LANGUAGES: dict[str, dict[str, str]] = {
    "en": {
        "show_window": "Show window",
        "refresh_now": "Refresh now",
        "settings": "Settings",
        "show_strip": "Show taskbar strip",
        "always_on_top": "Always on top (window)",
        "opaque_bg": "Strip: opaque background",
        "move_strip": "Move strip (drag with mouse)",
        "reset_strip_position": "Reset strip position",
        "display_mode": "Display mode",
        "mode_1": "Compact (quota only)",
        "mode_2": "+ Time remaining",
        "mode_3": "+ Time-remaining %",
        "mode_4": "+ Time-elapsed %",
        "language": "Language",
        "quit": "Quit",
        # Strip labels
        "5h": "5h",
        "7d": "7d",
        "today": "today",
    },
    "zh": {
        "show_window": "显示窗口",
        "refresh_now": "立即刷新",
        "settings": "设置",
        "show_strip": "显示底部状态条",
        "always_on_top": "窗口置顶",
        "opaque_bg": "状态条不透明背景",
        "move_strip": "拖动状态条",
        "reset_strip_position": "重置状态条位置",
        "display_mode": "显示模式",
        "mode_1": "简洁 (仅额度)",
        "mode_2": "+ 剩余时间",
        "mode_3": "+ 剩余时间百分比",
        "mode_4": "+ 已用时间百分比",
        "language": "语言",
        "quit": "退出",
        "5h": "5h",
        "7d": "7d",
        "today": "今日",
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


# ---- Visual constants (dark theme) ----
BG = "#1e1f22"          # window background
PANEL = "#2b2d31"       # subdued panel background
FG = "#e3e5e8"          # primary text
FG_DIM = "#8a8d92"      # secondary text
ACCENT = "#7c5cff"      # purple (matches Claude branding)
WARN = "#ffa657"        # orange
DANGER = "#ff5e5e"      # red
OK = "#3ddc97"          # green

WINDOW_W, WINDOW_H = 340, 460
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
# NOTE: pinning the strip OVER the taskbar (sh - STRIP_H) is fundamentally
# fragile on Win11 — the shell actively hides topmost windows that overlap the
# taskbar when Settings / Notification Center / Quick Settings flyouts open,
# and no amount of SetWindowPos / ShowWindow / deiconify in user-space resists
# this reliably. Keep the strip JUST ABOVE the taskbar instead — that's where
# every actually-stable taskbar-widget tool (Rainmeter skins, etc) lives.
STRIP_GAP_FROM_TASKBAR = 0  # gap between strip bottom and taskbar top


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


# ---- Win32 helpers for taskbar position detection ----
_SPI_GETWORKAREA = 0x0030


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
    ok = ctypes.windll.user32.SystemParametersInfoW(
        _SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    if not ok:
        return fallback_screen_h - 48
    # For a bottom-docked taskbar (the common case), the work area's bottom edge
    # equals the top of the taskbar. If the taskbar is on top/left/right, this
    # value is just where the work area ends downward — still a reasonable spot
    # to pin a strip on the primary monitor.
    return rect.bottom


def color_for_pct(pct: float) -> str:
    if pct >= 90:
        return DANGER
    if pct >= 75:
        return WARN
    return ACCENT


def render_tray_icon(pct: float) -> Image.Image:
    """Render the tray icon: rounded rect with the 5h percentage as big text."""
    size = TRAY_ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color = color_for_pct(pct)
    # Background rounded rect
    draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=14, fill=color)

    # Text: e.g. "33" or "100"
    text = f"{int(pct)}"
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


class FloatingWindow:
    """The tkinter floating-card window. Hidden by default; tray toggles visibility."""

    def __init__(self, orch: Orchestrator, on_close: callable) -> None:
        self.orch = orch
        self.on_close = on_close
        self.root = tk.Tk()
        self.root.title("Claude Usage")
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_W}x{WINDOW_H}+200+200")
        self.root.attributes("-topmost", True)     # always on top by default
        self.root.minsize(WINDOW_W, 200)
        # Closing the X button just hides — quitting is via tray.
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        # Start hidden until the user explicitly shows from tray.
        self.root.withdraw()

        self._build()
        # Periodically repaint with the latest snapshot from orchestrator.
        self._tick()

    def _build(self) -> None:
        pad = 14
        # Header
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=pad, pady=(pad, 6))
        tk.Label(header, text="Claude Usage", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 13)).pack(side="left")
        self.refresh_btn = tk.Button(
            header, text="↻", bg=BG, fg=FG_DIM, bd=0, font=("Segoe UI", 11),
            activebackground=PANEL, activeforeground=FG,
            command=self._refresh_clicked, cursor="hand2",
        )
        self.refresh_btn.pack(side="right", padx=4)
        self.topmost_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            header, text="📌", variable=self.topmost_var, bg=BG, fg=FG_DIM,
            selectcolor=BG, activebackground=BG, activeforeground=FG,
            bd=0, font=("Segoe UI", 10), command=self._toggle_topmost,
        ).pack(side="right", padx=2)

        # Quota section (5h + 7d)
        self.quota_frame = tk.Frame(self.root, bg=PANEL)
        self.quota_frame.pack(fill="x", padx=pad, pady=4)
        self.quota_widgets = {}
        for key, label in (("5h", "5h window"), ("7d", "Weekly")):
            row = tk.Frame(self.quota_frame, bg=PANEL)
            row.pack(fill="x", padx=10, pady=8)
            tk.Label(row, text=label, bg=PANEL, fg=FG_DIM,
                     font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
            pct_lbl = tk.Label(row, text="—", bg=PANEL, fg=FG,
                               font=("Segoe UI Semibold", 11), width=6, anchor="e")
            pct_lbl.pack(side="right")
            reset_lbl = tk.Label(row, text="", bg=PANEL, fg=FG_DIM,
                                 font=("Segoe UI", 8), anchor="e")
            reset_lbl.pack(side="right", padx=(0, 8))
            bar_canvas = tk.Canvas(self.quota_frame, height=6, bg=PANEL,
                                   highlightthickness=0)
            bar_canvas.pack(fill="x", padx=10, pady=(0, 4))
            self.quota_widgets[key] = (pct_lbl, reset_lbl, bar_canvas)

        # Cost section
        self.cost_frame = tk.Frame(self.root, bg=PANEL)
        self.cost_frame.pack(fill="x", padx=pad, pady=4)
        self.cost_labels = {}
        for key, label in (("session", "Session"), ("today", "Today"), ("month", "This month")):
            row = tk.Frame(self.cost_frame, bg=PANEL)
            row.pack(fill="x", padx=10, pady=4)
            tk.Label(row, text=label, bg=PANEL, fg=FG_DIM,
                     font=("Segoe UI", 9), anchor="w").pack(side="left")
            val = tk.Label(row, text="—", bg=PANEL, fg=FG,
                           font=("Segoe UI Semibold", 10), anchor="e")
            val.pack(side="right")
            self.cost_labels[key] = val

        # Projects section
        proj_header = tk.Frame(self.root, bg=BG)
        proj_header.pack(fill="x", padx=pad, pady=(8, 2))
        tk.Label(proj_header, text="Top projects (this month)", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left")

        self.proj_frame = tk.Frame(self.root, bg=PANEL)
        self.proj_frame.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        # Pre-create N fixed project rows; _render only updates label text and
        # pack_forgets unused rows. Destroying+recreating widgets every tick
        # would make the window visibly flicker.
        self.PROJ_ROWS_MAX = 6
        self.proj_rows: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        for _ in range(self.PROJ_ROWS_MAX):
            row = tk.Frame(self.proj_frame, bg=PANEL)
            path_lbl = tk.Label(row, text="", bg=PANEL, fg=FG,
                                font=("Segoe UI", 9), anchor="w")
            path_lbl.pack(side="left", fill="x", expand=True)
            cost_lbl = tk.Label(row, text="", bg=PANEL, fg=FG_DIM,
                                font=("Segoe UI", 9), anchor="e")
            cost_lbl.pack(side="right")
            self.proj_rows.append((row, path_lbl, cost_lbl))

        # Footer (last-update timestamps + error indicator)
        self.footer = tk.Label(self.root, text="", bg=BG, fg=FG_DIM,
                               font=("Segoe UI", 8))
        self.footer.pack(side="bottom", anchor="e", padx=pad, pady=(0, 6))

    def _refresh_clicked(self) -> None:
        self.orch.refresh_now()

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.topmost_var.get()))

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        self.root.withdraw()
        if self.on_close:
            self.on_close()

    def _tick(self) -> None:
        try:
            self._render(self.orch.snapshot())
        except Exception:
            log = logging.getLogger(__name__)
            log.exception("render failed")
        # Repaint every second so the "resets in Xm" countdown updates live.
        self.root.after(1000, self._tick)

    def _render(self, s) -> None:
        # Quota bars
        u: UsageSnapshot | None = s.usage
        for key, getters in (
            ("5h", (lambda: u.five_hour_pct, lambda: u.five_hour_minutes_to_reset)),
            ("7d", (lambda: u.seven_day_pct, lambda: u.seven_day_minutes_to_reset)),
        ):
            pct_lbl, reset_lbl, bar_canvas = self.quota_widgets[key]
            if u is None:
                pct_lbl.config(text="—", fg=FG_DIM)
                reset_lbl.config(text=s.usage_error[:40] if s.usage_error else "loading…")
                bar_canvas.delete("all")
                continue
            pct = getters[0]()
            minutes = getters[1]()
            color = color_for_pct(pct)
            pct_lbl.config(text=f"{pct:.0f}%", fg=color)
            reset_lbl.config(text=f"resets {fmt_minutes(minutes)}")
            bar_canvas.delete("all")
            # NB: don't call update_idletasks here — that forces a synchronous
            # redraw mid-tick and contributes to visible flicker. winfo_width()
            # may be 1 on the very first tick before layout; the fallback covers it.
            w = bar_canvas.winfo_width() or (WINDOW_W - 32)
            # Background track
            bar_canvas.create_rectangle(0, 0, w, 6, fill="#3a3c41", outline="")
            # Fill
            fill_w = int(w * min(pct, 100) / 100)
            if fill_w > 0:
                bar_canvas.create_rectangle(0, 0, fill_w, 6, fill=color, outline="")

        # Costs
        rpt = s.report
        if rpt is None:
            for v in self.cost_labels.values():
                v.config(text="loading…")
        else:
            session_cost = rpt.by_session.get(rpt.last_session_id, Aggregate()).cost_usd
            self.cost_labels["session"].config(text=f"${session_cost:,.4f}")
            self.cost_labels["today"].config(text=f"${rpt.today.cost_usd:,.2f}")
            self.cost_labels["month"].config(text=f"${rpt.this_month.cost_usd:,.2f}")

        # Projects: update existing rows in place; pack/forget to handle row-count changes.
        top: list[tuple[str, Aggregate]] = []
        if rpt is not None:
            top = sorted(rpt.by_project.items(),
                         key=lambda kv: kv[1].cost_usd, reverse=True)[:self.PROJ_ROWS_MAX]
        for i, (row, path_lbl, cost_lbl) in enumerate(self.proj_rows):
            if i < len(top):
                cwd, agg = top[i]
                short = cwd if len(cwd) <= 32 else "…" + cwd[-31:]
                path_lbl.config(text=short)
                cost_lbl.config(text=f"${agg.cost_usd:,.2f}")
                if not row.winfo_ismapped():
                    row.pack(fill="x", padx=10, pady=2)
            elif row.winfo_ismapped():
                row.pack_forget()

        # Footer
        if s.usage_error:
            self.footer.config(text=f"⚠ {s.usage_error[:50]}", fg=DANGER)
        else:
            import time as _t
            age = int(_t.time() - s.last_api_fetch) if s.last_api_fetch else -1
            self.footer.config(text=f"updated {age}s ago" if age >= 0 else "", fg=FG_DIM)


class TaskbarStrip:
    """Always-visible compact strip pinned just above the Windows taskbar.

    Visually mimics a taskbar widget, but is actually a borderless Toplevel
    positioned with Win32 taskbar-rect detection — no DeskBand COM hackery.
    Left-click expands the main floating window; right-click opens a menu.
    """

    def __init__(self, parent_root: tk.Tk, orch: Orchestrator,
                 on_left_click: callable) -> None:
        self.orch = orch
        self.on_left_click_cb = on_left_click
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

        self.win = tk.Toplevel(parent_root)
        self.win.overrideredirect(True)         # no title bar / borders
        self.win.attributes("-topmost", True)   # above normal app windows
        self.win.attributes("-toolwindow", True)  # hide from Alt+Tab
        # Transparent backdrop: any pixel matching TRANSPARENT_KEY becomes fully
        # see-through on Windows. Both the Toplevel and the Canvas use this key
        # color as their background, so only the drawn text + outline remain.
        self.win.configure(bg=TRANSPARENT_KEY)
        self.win.attributes("-transparentcolor", TRANSPARENT_KEY)

        # Strip width is dynamic — grown/shrunk to fit the rendered text each tick.
        self.strip_w = STRIP_W

        # Font instances (not just tuples) so we can call .measure() during layout.
        self.font_main = tkfont.Font(family="Segoe UI Semibold", size=9)
        self.font_dim = tkfont.Font(family="Segoe UI", size=9)

        # Single Canvas replaces the old Frame+Labels arrangement. Canvas lets us
        # draw outlined text manually (tkinter Labels can't do strokes) and
        # combined with -transparentcolor produces a "floating text" look.
        self.canvas = tk.Canvas(
            self.win, bg=TRANSPARENT_KEY,
            highlightthickness=0, borderwidth=0,
            width=STRIP_W, height=STRIP_H,
        )
        self.canvas.pack(fill="both", expand=True)

        # Click bindings only need the canvas — outlined text covers most pixels,
        # and clicks on the transparent gaps fall through to whatever's behind
        # (usually the taskbar, where missing the strip is harmless).
        for widget in (self.canvas, self.win):
            widget.bind("<Button-1>", self._on_btn1_press)
            widget.bind("<B1-Motion>", self._on_btn1_motion)
            widget.bind("<ButtonRelease-1>", self._on_btn1_release)
            widget.bind("<Button-3>", self._on_right_click)

        # Right-click context menu
        self._menu = tk.Menu(self.win, tearoff=0, bg=PANEL, fg=FG,
                             activebackground=ACCENT, activeforeground="white",
                             borderwidth=0)
        self._menu.add_command(label="Show window", command=self._on_show_window)
        self._menu.add_command(label="Refresh now", command=self.orch.refresh_now)
        self._menu.add_separator()
        self._menu.add_command(label="Hide strip", command=self.hide)

        # Validate saved drag position before first paint: if it points into
        # the taskbar area (where Win11's TOPMOST taskbar would hide us — we
        # can't beat its z-order with a normal app window) or off-screen,
        # snap it to a visible spot and re-save. This migrates positions
        # left over from an earlier build that tolerated taskbar overlap.
        self._validate_custom_pos()

        self._reposition()
        self._tick()

    def _validate_custom_pos(self) -> None:
        if self._custom_pos is None:
            return
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            tb_top = get_taskbar_top_logical(sh)
        except tk.TclError:
            return
        x, y = self._custom_pos
        new_x, new_y = x, y
        if y + STRIP_H > tb_top:                 # would overlap taskbar
            new_y = tb_top - STRIP_H - STRIP_GAP_FROM_TASKBAR
        new_x = max(0, min(new_x, sw - 60))      # at least 60 px visible
        new_y = max(0, new_y)
        if (new_x, new_y) == (x, y):
            return
        log = logging.getLogger(__name__)
        log.info("strip: migrating saved position (%d,%d) -> (%d,%d) "
                 "(was overlapping taskbar or off-screen)",
                 x, y, new_x, new_y)
        self._custom_pos = (new_x, new_y)
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["x"] = new_x
        cfg["strip"]["y"] = new_y
        save_config(cfg)

    def _reposition(self) -> None:
        # Skip geometry update during a drag (mouse drives it); always still
        # call the topmost bump below so dragging doesn't lose z-order.
        drag_active = self.drag_mode and self._drag_anchor is not None
        if not drag_active:
            if self._custom_pos is not None:
                x, y = self._custom_pos
            else:
                sw = self.win.winfo_screenwidth()
                sh = self.win.winfo_screenheight()
                if STRIP_SIDE == "left":
                    x = STRIP_SIDE_MARGIN
                else:
                    x = sw - self.strip_w - STRIP_SIDE_MARGIN
                y = get_taskbar_top_logical(sh) - STRIP_H - STRIP_GAP_FROM_TASKBAR
            self.win.geometry(f"{self.strip_w}x{STRIP_H}+{x}+{y}")
        self._force_topmost()

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

    def show(self) -> None:
        self.visible = True
        self._reposition()
        self.win.deiconify()
        self.win.attributes("-topmost", True)   # re-assert in case Windows demoted it

    def hide(self) -> None:
        self.visible = False
        self.win.withdraw()

    def _on_show_window(self) -> None:
        """Menu command — always opens the main window regardless of drag mode."""
        self.on_left_click_cb()

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
        # Clamp X so at least ~60px stays on the primary monitor — otherwise
        # a wild drag can leave the strip un-grabbable off-screen.
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        new_x = max(-STRIP_W + 60, min(new_x, sw - 60))
        # Clamp Y to STOP at the top of the Windows taskbar. We can't reliably
        # render above the taskbar with a normal HWND_TOPMOST window — the
        # taskbar is also HWND_TOPMOST and the simple `lift+topmost` bump trick
        # we use doesn't outrank it. So we prevent the user from dragging into
        # a zone where the strip would silently disappear.
        tb_top = get_taskbar_top_logical(sh)
        max_y = tb_top - STRIP_H
        new_y = max(0, min(new_y, max_y))
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
        self.drag_mode = enabled
        cursor = "fleur" if enabled else ""
        try:
            self.canvas.config(cursor=cursor)
            self.win.config(cursor=cursor)
        except tk.TclError:
            pass
        # Force a redraw so the outline rectangle (or its removal) shows up
        # immediately rather than waiting for the next tick.
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip redraw on drag toggle failed")

    def reset_position(self) -> None:
        """Clear the saved custom position and snap back to STRIP_SIDE defaults."""
        self._custom_pos = None
        cfg = load_config()
        if "strip" in cfg:
            cfg["strip"].pop("x", None)
            cfg["strip"].pop("y", None)
        save_config(cfg)
        self._reposition()

    def set_show_background(self, enabled: bool) -> None:
        """Toggle the opaque dark backdrop behind the strip text. Persisted."""
        self.show_background = enabled
        cfg = load_config()
        cfg.setdefault("strip", {})
        cfg["strip"]["show_background"] = enabled
        save_config(cfg)
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
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip redraw on mode change failed")

    def _on_right_click(self, event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _tick(self) -> None:
        try:
            self._render(self.orch.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("strip render failed")
        if self.visible:
            self._reposition()
        self.win.after(1000, self._tick)

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

    def _append_quota_parts(self, parts: list, label_key: str, quota_pct: float,
                            mins_remaining: int, total_window_min: int) -> None:
        """Append the (text, color, font) pieces for one quota's display in the
        currently selected display_mode. Caller is responsible for any leading
        separator. Pieces are appended in reading order (caller packs left-to-right
        OR right-to-left based on STRIP_SIDE).
        """
        parts.append((t(label_key) + " ", FG_DIM, self.font_dim))
        quota_color = color_for_pct(quota_pct)
        if self.display_mode == 1:
            # Mode 1: just the quota%
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
        u = s.usage
        rpt = s.report

        # Build the ordered list of (text, color, font) pieces. Caption pieces
        # use FG_DIM; value pieces use the threshold-based color. Layout depends
        # on the current display_mode (selected from the tray Settings submenu).
        parts: list[tuple[str, str, tkfont.Font]] = []
        if u is not None:
            self._append_quota_parts(parts, "5h", u.five_hour_pct,
                                     u.five_hour_minutes_to_reset, TOTAL_5H_MIN)
            parts.append(("   ·   ", FG_DIM, self.font_dim))
            self._append_quota_parts(parts, "7d", u.seven_day_pct,
                                     u.seven_day_minutes_to_reset, TOTAL_7D_MIN)
        if rpt is not None:
            if u is not None:
                parts.append(("   ·   ", FG_DIM, self.font_dim))
            parts.append((t("today") + " ", FG_DIM, self.font_dim))
            parts.append((f"${rpt.today.cost_usd:,.2f}", FG, self.font_main))
        if not parts:
            return  # nothing to draw yet (initial state before first data lands)

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
            render_tray_icon(0),
            "Claude Usage",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        # Top level kept to 4 items. Everything configurable lives under
        # Settings → ... — nested submenus keep the right-click menu short and
        # the daily-driver actions (Show / Refresh / Quit) immediately visible.
        return pystray.Menu(
            pystray.MenuItem(lambda _i: t("show_window"), self._on_show, default=True),
            pystray.MenuItem(lambda _i: t("refresh_now"),
                             lambda _i: self.orch.refresh_now()),
            pystray.MenuItem(lambda _i: t("settings"), self._build_settings_menu()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda _i: t("quit"), self._on_quit),
        )

    def _build_settings_menu(self) -> pystray.Menu:
        """Settings submenu — visibility/appearance/behavior, plus nested
        display-mode and language pickers."""
        return pystray.Menu(
            # Strip-related toggles
            pystray.MenuItem(
                lambda _i: t("show_strip"),
                lambda _i, _it: self._on_strip_toggle(),
                checked=lambda _it: bool(self.strip and self.strip.visible),
            ),
            pystray.MenuItem(
                lambda _i: t("opaque_bg"),
                lambda _i, _it: self._on_strip_bg_toggle(),
                checked=lambda _it: bool(self.strip and self.strip.show_background),
            ),
            pystray.MenuItem(
                lambda _i: t("move_strip"),
                lambda _i, _it: self._on_strip_drag_toggle(),
                checked=lambda _it: bool(self.strip and self.strip.drag_mode),
            ),
            pystray.MenuItem(
                lambda _i: t("reset_strip_position"),
                lambda _i, _it: self._on_strip_reset(),
            ),
            # Sub-sub-menus
            pystray.MenuItem(lambda _i: t("display_mode"), self._build_display_mode_menu()),
            pystray.MenuItem(lambda _i: t("language"), self._build_language_menu()),
            pystray.Menu.SEPARATOR,
            # Window-related
            pystray.MenuItem(
                lambda _i: t("always_on_top"),
                lambda _i, _it: self._on_topmost_toggle(_it),
                checked=lambda _it: bool(self.window.topmost_var.get()),
            ),
        )

    def _build_display_mode_menu(self) -> pystray.Menu:
        """Radio-style picker for the three strip layouts."""
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

    def _build_language_menu(self) -> pystray.Menu:
        """Radio-style picker for UI language. Currently English + 中文."""
        def make(code: str, label: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                label,  # static — language names are conventionally untranslated
                lambda _i, _it: self._on_set_language(code),
                checked=lambda _it: get_app_language() == code,
                radio=True,
            )
        return pystray.Menu(make("en", "English"), make("zh", "中文"))

    def _on_strip_toggle(self) -> None:
        if self.strip is None:
            return
        if self.strip.visible:
            self.window.root.after(0, self.strip.hide)
        else:
            self.window.root.after(0, self.strip.show)

    def _on_strip_drag_toggle(self) -> None:
        if self.strip is None:
            return
        new_val = not self.strip.drag_mode
        self.window.root.after(0, lambda: self.strip.set_drag_mode(new_val))

    def _on_strip_bg_toggle(self) -> None:
        if self.strip is None:
            return
        new_val = not self.strip.show_background
        self.window.root.after(0, lambda: self.strip.set_show_background(new_val))

    def _on_strip_reset(self) -> None:
        if self.strip is None:
            return
        self.window.root.after(0, self.strip.reset_position)

    def _on_set_display_mode(self, mode: int) -> None:
        if self.strip is None:
            return
        self.window.root.after(0, lambda: self.strip.set_display_mode(mode))

    def _on_set_language(self, lang: str) -> None:
        # Language is global; menu text is callable so it'll re-evaluate on
        # next menu open. The strip picks up the new language on its next
        # render tick automatically.
        set_app_language(lang)
        # Force an immediate strip redraw so labels update before the next
        # natural tick — feels more responsive than waiting 1s.
        if self.strip is not None:
            self.window.root.after(0, lambda: self.strip._render(self.orch.snapshot()))

    def _on_show(self, _icon=None, _item=None) -> None:
        # tkinter calls must happen on the main thread.
        self.window.root.after(0, self.window.show)

    def _on_topmost_toggle(self, _item) -> None:
        new_val = not self.window.topmost_var.get()
        self.window.topmost_var.set(new_val)
        self.window.root.after(0, self.window._toggle_topmost)

    def _on_quit(self, _icon=None, _item=None) -> None:
        self.icon.stop()
        self._stop_callback()

    def update_icon(self, pct: float, tooltip: str) -> None:
        try:
            self.icon.icon = render_tray_icon(pct)
            self.icon.title = tooltip
        except Exception:
            pass

    def run_detached(self) -> None:
        """Start the tray in its own thread so the main thread can run tk.mainloop."""
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()


def notify(kind: str, pct: float) -> None:
    """Pop a Windows toast notification for a threshold crossing."""
    window_label = "5-hour window" if kind.startswith("5h") else "Weekly quota"
    title = f"Claude {window_label} at {pct:.0f}%"
    body = "Heads up — you may want to slow down or switch projects."
    if pct >= 95:
        body = "Very close to the limit. Stop or you'll get rate-limited."
    elif pct >= 90:
        body = "Approaching the limit. Plan accordingly."
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


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Restore the saved language before any UI is built — menu and strip both
    # read it at render time, so setting it here makes the first frame correct.
    global _current_lang
    saved_lang = load_config().get("language")
    if isinstance(saved_lang, str) and saved_lang in LANGUAGES:
        _current_lang = saved_lang

    orch = Orchestrator()
    # Wire callbacks AFTER constructing window+tray so we can reference them.
    window = FloatingWindow(orch, on_close=lambda: None)
    # Strip is a Toplevel parented to window.root — shares the tk main loop.
    strip = TaskbarStrip(window.root, orch, on_left_click=window.show)
    tray = TrayApp(orch, window, strip=strip)

    # When state changes, update tray badge with current 5h%.
    def on_change() -> None:
        s = orch.snapshot()
        if s.usage is not None:
            pct = s.usage.five_hour_pct
            tooltip = (
                f"Claude Usage\n"
                f"5h: {pct:.0f}%  resets {fmt_minutes(s.usage.five_hour_minutes_to_reset)}\n"
                f"7d: {s.usage.seven_day_pct:.0f}%  resets {fmt_minutes(s.usage.seven_day_minutes_to_reset)}"
            )
            tray.update_icon(pct, tooltip)

    orch._on_change = on_change
    orch._on_alert = notify
    orch.start()
    tray._stop_callback = window.root.quit
    tray.run_detached()

    # Run tkinter main loop on main thread. Window is initially hidden;
    # user clicks tray → "Show window" to make it visible.
    try:
        window.root.mainloop()
    finally:
        orch.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
