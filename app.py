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

        self.win = tk.Toplevel(parent_root)
        self.win.overrideredirect(True)         # no title bar / borders
        self.win.attributes("-topmost", True)   # above normal app windows
        self.win.attributes("-toolwindow", True)  # hide from Alt+Tab
        self.win.configure(bg=BG)

        # Horizontal row of labels — separate widgets so each value can carry its own color.
        row = tk.Frame(self.win, bg=BG)
        row.pack(fill="both", expand=True, padx=10, pady=2)

        font_main = ("Segoe UI Semibold", 9)
        font_dim = ("Segoe UI", 9)

        # Pack from the SAME side the strip is pinned to, so content sits flush
        # against the screen edge (left-pinned strip → text on the left).
        pack_side = "left" if STRIP_SIDE == "left" else "right"

        def mk(text: str, fg: str, font: tuple) -> tk.Label:
            lbl = tk.Label(row, text=text, bg=BG, fg=fg, font=font)
            lbl.pack(side=pack_side)
            return lbl

        # Build in reading order; if pack_side is "right" tkinter naturally
        # reverses the visual order so the read direction stays correct.
        if pack_side == "left":
            order = ["5h_cap", "5h_val", "sep1", "7d_cap", "7d_val", "sep2", "cost_cap", "cost_val"]
        else:
            order = ["cost_val", "cost_cap", "sep2", "7d_val", "7d_cap", "sep1", "5h_val", "5h_cap"]

        slots: dict[str, tk.Label] = {}
        for key in order:
            if key == "5h_cap":
                slots[key] = mk("5h ", FG_DIM, font_dim)
            elif key == "5h_val":
                slots[key] = mk("—", FG, font_main)
            elif key == "7d_cap":
                slots[key] = mk(" 7d ", FG_DIM, font_dim)
            elif key == "7d_val":
                slots[key] = mk("—", FG, font_main)
            elif key == "cost_cap":
                slots[key] = mk(" today ", FG_DIM, font_dim)
            elif key == "cost_val":
                slots[key] = mk("—", FG, font_main)
            elif key in ("sep1", "sep2"):
                slots[key] = mk("   ·   ", FG_DIM, font_dim)

        self.five_lbl = slots["5h_val"]
        self.seven_lbl = slots["7d_val"]
        self.cost_lbl = slots["cost_val"]
        self.cost_caption = slots["cost_cap"]

        # Track all click-receiving widgets so we can change cursor/bg in drag mode.
        self._all_widgets: list[tk.Widget] = [self.win, row, *slots.values()]
        # Click bindings on the window, row container, and every label inside —
        # otherwise clicks on a label fall through without firing.
        for widget in self._all_widgets:
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

        self._reposition()
        self._tick()

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
                    x = sw - STRIP_W - STRIP_SIDE_MARGIN
                y = get_taskbar_top_logical(sh) - STRIP_H - STRIP_GAP_FROM_TASKBAR
            self.win.geometry(f"{STRIP_W}x{STRIP_H}+{x}+{y}")
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
        # Clamp so at least ~60px of the strip stays on the primary monitor —
        # otherwise a wild drag can leave it un-grabbable off-screen.
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        new_x = max(-STRIP_W + 60, min(new_x, sw - 60))
        new_y = max(0, min(new_y, sh - 10))
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
        """Toggle drag-to-reposition mode. Visual change: purple background +
        4-way move cursor so the user knows clicking will drag, not open."""
        self.drag_mode = enabled
        new_bg = ACCENT if enabled else BG
        new_cursor = "fleur" if enabled else ""
        for w in self._all_widgets:
            try:
                w.configure(bg=new_bg, cursor=new_cursor)
            except tk.TclError:
                pass  # Some widget types don't accept all options — best-effort.

    def reset_position(self) -> None:
        """Clear the saved custom position and snap back to STRIP_SIDE defaults."""
        self._custom_pos = None
        cfg = load_config()
        if "strip" in cfg:
            cfg["strip"].pop("x", None)
            cfg["strip"].pop("y", None)
        save_config(cfg)
        self._reposition()

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

    def _render(self, s) -> None:
        u = s.usage
        rpt = s.report
        if u is not None:
            self.five_lbl.config(text=f"{u.five_hour_pct:.0f}%",
                                 fg=color_for_pct(u.five_hour_pct))
            self.seven_lbl.config(text=f"{u.seven_day_pct:.0f}%",
                                  fg=color_for_pct(u.seven_day_pct))
        if rpt is not None:
            self.cost_lbl.config(text=f"${rpt.today.cost_usd:,.2f}")


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
        items = [
            pystray.MenuItem("Show window", self._on_show, default=True),
            pystray.MenuItem("Always on top",
                             lambda _i, item: self._on_topmost_toggle(item),
                             checked=lambda item: bool(self.window.topmost_var.get())),
        ]
        if self.strip is not None:
            items.append(pystray.MenuItem(
                "Show taskbar strip",
                lambda _i, _item: self._on_strip_toggle(),
                checked=lambda item: bool(self.strip and self.strip.visible),
            ))
            items.append(pystray.MenuItem(
                "Move strip (drag with mouse)",
                lambda _i, _item: self._on_strip_drag_toggle(),
                checked=lambda item: bool(self.strip and self.strip.drag_mode),
            ))
            items.append(pystray.MenuItem(
                "Reset strip position",
                lambda _i, _item: self._on_strip_reset(),
            ))
        items.extend([
            pystray.MenuItem("Refresh now", lambda _i: self.orch.refresh_now()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        ])
        return pystray.Menu(*items)

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

    def _on_strip_reset(self) -> None:
        if self.strip is None:
            return
        self.window.root.after(0, self.strip.reset_position)

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
