"""Native taskbar widget window — the Soda Music technique, in ctypes.

A WS_POPUP layered window, created on its own thread with per-monitor DPI
awareness (so it renders crisp at 125%/150% while the tkinter app stays
DPI-unaware), and OWNED by the taskbar (Shell_TrayWnd). Owned windows always
sit above their owner, so the widget stays on the taskbar band without any
topmost bump loop.

Content is a premultiplied BGRA bitmap pushed with UpdateLayeredWindow: real
per-pixel alpha (anti-aliased shapes, no colour-key halos). Pixels drawn with
alpha 0 are click-through, so the renderer paints an alpha-1 backdrop.

Thread model: every Win32 call on the widget window happens on the widget
thread. The tk thread talks to it only through `update()/show()/hide()/
set_side()/close()` (store state + PostMessage) and reads input back from the
thread-safe `events` queue: ("click",), ("menu",), ("hover", bool),
("rerender",).
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import winreg
from ctypes import wintypes

from PIL import Image, ImageChops

log = logging.getLogger(__name__)

u32 = ctypes.WinDLL("user32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwm = ctypes.WinDLL("dwmapi")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD,
                                  wintypes.HWND, wintypes.LONG, wintypes.LONG,
                                  wintypes.DWORD, wintypes.DWORD)


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


def _sig(fn, res, *args):
    fn.restype = res
    fn.argtypes = list(args)


H, U, W, L = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
_sig(u32.DefWindowProcW, LRESULT, H, U, W, L)
_sig(u32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(u32.UnregisterClassW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.HINSTANCE)
_sig(u32.CreateWindowExW, H, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
     wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     H, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(u32.DestroyWindow, wintypes.BOOL, H)
_sig(u32.PostMessageW, wintypes.BOOL, H, U, W, L)
_sig(u32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), H, U, U)
_sig(u32.TranslateMessage, wintypes.BOOL, ctypes.POINTER(wintypes.MSG))
_sig(u32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
_sig(u32.PostQuitMessage, None, ctypes.c_int)
_sig(u32.FindWindowW, H, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(u32.FindWindowExW, H, H, H, wintypes.LPCWSTR, wintypes.LPCWSTR)
_sig(u32.GetWindowRect, wintypes.BOOL, H, ctypes.POINTER(wintypes.RECT))
_sig(u32.IsWindow, wintypes.BOOL, H)
_sig(u32.IsWindowVisible, wintypes.BOOL, H)
_sig(u32.GetForegroundWindow, H)
_sig(u32.GetDesktopWindow, H)
_sig(u32.GetAncestor, H, H, U)
_sig(u32.IsIconic, wintypes.BOOL, H)
_sig(u32.GetClassNameW, ctypes.c_int, H, wintypes.LPWSTR, ctypes.c_int)
_sig(dwm.DwmGetWindowAttribute, wintypes.LONG, H, wintypes.DWORD,
     ctypes.c_void_p, wintypes.DWORD)
_sig(u32.MonitorFromPoint, wintypes.HMONITOR, wintypes.POINT, wintypes.DWORD)
_sig(u32.GetMonitorInfoW, wintypes.BOOL, wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
_sig(u32.GetWindow, H, H, U)
_sig(u32.GetWindowThreadProcessId, wintypes.DWORD, H, ctypes.POINTER(wintypes.DWORD))
_sig(u32.SetWinEventHook, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
     wintypes.HMODULE, WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD)
_sig(u32.UnhookWinEvent, wintypes.BOOL, wintypes.HANDLE)
_sig(u32.SetWindowPos, wintypes.BOOL, H, H, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, U)
_sig(u32.RegisterWindowMessageW, U, wintypes.LPCWSTR)
_sig(u32.TrackMouseEvent, wintypes.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
_sig(u32.GetDC, wintypes.HDC, H)
_sig(u32.ReleaseDC, ctypes.c_int, H, wintypes.HDC)
_sig(u32.UpdateLayeredWindow, wintypes.BOOL, H, wintypes.HDC,
     ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
     ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
     ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
_sig(u32.LoadCursorW, wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPVOID)
_sig(u32.GetDpiForWindow, U, H)
_sig(u32.SetThreadDpiAwarenessContext, ctypes.c_void_p, ctypes.c_void_p)
_sig(g32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_sig(g32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(g32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.c_void_p, U,
     ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
_sig(g32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
_sig(g32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
_sig(k32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
GW_HWNDPREV, GW_OWNER, GA_ROOT = 3, 4, 2
DWMWA_EXTENDED_FRAME_BOUNDS, DWMWA_CLOAKED = 9, 14
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x1, 0x2, 0x4
SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_HIDEWINDOW, SWP_NOOWNERZORDER = 0x10, 0x40, 0x80, 0x200
# Every visibility/position change preserves both windows' Z order, even if
# the foreground changes between our coverage check and the Win32 call.
PLACEMENT_FLAGS = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND = 0x16, 0x17
EVENT_OBJECT_DESTROY, EVENT_OBJECT_SHOW = 0x8001, 0x8002
EVENT_OBJECT_HIDE, EVENT_OBJECT_REORDER = 0x8003, 0x8004
EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_LOCATIONCHANGE = 0x800A, 0x800B
EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED = 0x8017, 0x8018
OBJID_WINDOW, OBJID_CLIENT = 0, -4
WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNTHREAD = 0, 1
WM_DESTROY, WM_MOUSEMOVE = 0x2, 0x200
WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE = 0x202, 0x205, 0x2A3
WM_MOUSEACTIVATE, WM_SETTINGCHANGE, WM_DPICHANGED = 0x21, 0x1A, 0x2E0
WM_DISPLAYCHANGE, WM_CLOSE = 0x7E, 0x10
MA_NOACTIVATE = 3
TME_LEAVE = 0x2
WM_APP_UPDATE = 0x8001
DPI_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
IDC_HAND = 32649

_EXPLORER_ADV = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def _reg_dword(path: str, name: str, default: int) -> int:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return int(winreg.QueryValueEx(k, name)[0])
    except OSError:
        return default


def taskbar_is_light() -> bool:
    """Taskbar follows the *system* theme (not the app theme)."""
    return _reg_dword(_PERSONALIZE, "SystemUsesLightTheme", 0) == 1


def taskbar_left_aligned() -> bool:
    """Win11 TaskbarAl: 0 = left-aligned (Start button sits at the far left)."""
    return _reg_dword(_EXPLORER_ADV, "TaskbarAl", 1) == 0


def _premultiplied_bgra(img: Image.Image) -> bytes:
    r, g, b, a = img.convert("RGBA").split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
    return Image.merge("RGBA", (r, g, b, a)).tobytes("raw", "BGRA")


def _contains(outer: wintypes.RECT, inner: wintypes.RECT) -> bool:
    return (outer.left <= inner.left and outer.top <= inner.top
            and outer.right >= inner.right and outer.bottom >= inner.bottom)


def _covers_monitor(hwnd: int, monitor: wintypes.RECT) -> bool:
    r = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(r)) or not _contains(r, monitor):
        return False
    # Only monitor-sized candidates warrant DWM queries. The visible frame
    # excludes invisible resize borders. Retain the window rect as fallback.
    frame_status = dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                             ctypes.byref(r), ctypes.sizeof(r))
    if frame_status == 0 and not _contains(r, monitor):
        return False
    cloaked = wintypes.DWORD()
    if dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked),
                                 ctypes.sizeof(cloaked)) == 0 and cloaked.value:
        return False
    cls = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(hwnd, cls, len(cls))
    return cls.value not in ("Progman", "WorkerW", "#32769")  # desktop windows


class NativeTaskbarWidget:
    MARGIN = 12  # logical px from the taskbar edge / tray area

    def __init__(self, side: str = "left") -> None:
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.side = side
        self.scale = 1.0
        self.hwnd = 0
        self._lock = threading.Lock()
        self._image: Image.Image | None = None
        self._image_version = 0
        self._visible = True
        self._hover = False
        self._taskbar = 0
        self._tray = 0
        self._watched_windows: set[int] = set()
        self._hooks: dict[tuple[int, int, int], int] = {}
        self._missing_hooks: set[tuple[int, int, int]] = set()
        self._sync_pending = False
        self._walk_interrupted = False
        self._closing = False
        self._msg_taskbar_created = -1
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="taskbar-widget",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(5)

    # ------------------------------------------------ tk-thread API
    def update(self, img: Image.Image) -> None:
        with self._lock:
            self._image = img
            self._image_version += 1
        self._post(WM_APP_UPDATE)

    def show(self) -> None:
        self._visible = True
        self._post(WM_APP_UPDATE)

    def hide(self) -> None:
        self._visible = False
        self._post(WM_APP_UPDATE)

    def set_side(self, side: str) -> None:
        self.side = side
        self._post(WM_APP_UPDATE)

    def close(self) -> None:
        self._closing = True
        self._post(WM_CLOSE)

    def _post(self, msg: int) -> None:
        hwnd = self.hwnd
        if hwnd:
            u32.PostMessageW(hwnd, msg, 0, 0)

    # ------------------------------------------------ widget thread
    def _run(self) -> None:
        try:
            u32.SetThreadDpiAwarenessContext(DPI_PER_MONITOR_AWARE_V2)
        except (AttributeError, OSError):
            pass  # pre-1703 Windows: falls back to unaware (blurrier, works)
        hinst = k32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)  # keep a reference!
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.hCursor = u32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        wc.lpszClassName = "ClaudeUsageTaskbarWidget"
        self._event_proc = WINEVENTPROC(self._on_win_event)
        try:
            # A previous instance may still be closing after a style switch.
            if not u32.RegisterClassExW(ctypes.byref(wc)):
                if ctypes.get_last_error() != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                    raise ctypes.WinError(ctypes.get_last_error())
                wc.lpszClassName += f"_{id(self):x}"
                if not u32.RegisterClassExW(ctypes.byref(wc)):
                    raise ctypes.WinError(ctypes.get_last_error())
            self._msg_taskbar_created = u32.RegisterWindowMessageW("TaskbarCreated")
            # Explorer can destroy its owned popup along with the taskbar.
            # Keep the thread, desired visibility and last image across recreation.
            while not self._closing:
                self._taskbar = u32.FindWindowW("Shell_TrayWnd", None) or 0
                self.hwnd = u32.CreateWindowExW(
                    WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                    wc.lpszClassName, "Claude Usage", WS_POPUP, 0, 0, 1, 1,
                    self._taskbar, None, hinst, None) or 0
                if not self.hwnd:
                    error = ctypes.get_last_error()
                    if self._taskbar != (u32.FindWindowW("Shell_TrayWnd", None) or 0):
                        continue  # owner disappeared during creation
                    raise ctypes.WinError(error)
                self._uploaded_version = -1
                self._watched_windows.clear()
                self._sync_pending = False
                self._walk_interrupted = False
                self._left_aligned = taskbar_left_aligned()
                self._refresh_scale()
                self._post(WM_CLOSE if self._closing else WM_APP_UPDATE)
                self._ready.set()
                msg = wintypes.MSG()
                while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                if self.hwnd:  # message loop ended independently of WM_DESTROY
                    u32.DestroyWindow(self.hwnd)
                    break
        except OSError:
            log.exception("taskbar widget thread failed")
        finally:
            for hook in self._hooks.values():
                u32.UnhookWinEvent(hook)
            u32.UnregisterClassW(wc.lpszClassName, hinst)
            self._ready.set()

    def _refresh_scale(self) -> None:
        tb = self._taskbar or self.hwnd
        dpi = u32.GetDpiForWindow(tb) if tb else 96
        new = (dpi or 96) / 96
        if abs(new - self.scale) > 1e-3:
            self.scale = new
            self.events.put(("rerender",))

    def _queue_sync(self) -> None:
        if self.hwnd and not self._sync_pending:
            self._sync_pending = bool(u32.PostMessageW(self.hwnd, WM_APP_UPDATE, 0, 0))

    def _on_win_event(self, hook, event, hwnd, obj, child, tid, timestamp):
        if child or self._sync_pending or hwnd == self.hwnd:
            return
        if event == EVENT_OBJECT_REORDER:
            # A top-level restack can be reported against the desktop's
            # client object. Ignore reorders within controls and documents.
            if obj not in (OBJID_WINDOW, OBJID_CLIENT):
                return
            if hwnd and hwnd != u32.GetDesktopWindow() and (
                    obj == OBJID_CLIENT or u32.GetAncestor(hwnd, GA_ROOT) != hwnd):
                return
        elif obj != OBJID_WINDOW:
            return
        elif event in (EVENT_OBJECT_SHOW, EVENT_OBJECT_UNCLOAKED, EVENT_SYSTEM_MINIMIZEEND):
            # Newly visible top-levels and replacement tray children need
            # discovery even when they have never received focus.
            if not hwnd or u32.GetAncestor(hwnd, GA_ROOT) not in (hwnd, self._taskbar):
                return
        elif event != EVENT_SYSTEM_FOREGROUND and hwnd not in self._watched_windows:
            return  # includes DESTROY; never query a destroyed HWND here
        self._queue_sync()

    def _install_hook(self, key: tuple[int, int, int]) -> None:
        first, last, tid = key
        handle = u32.SetWinEventHook(first, last, None, self._event_proc,
                                     0, tid, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNTHREAD)
        if handle:
            self._hooks[key] = handle

    def _watch_windows(self, windows: set[int]) -> None:
        if windows == self._watched_windows and self._hooks and not self._missing_hooks:
            return
        new_windows = windows - self._watched_windows
        self._watched_windows = windows
        wanted = {(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0),
                  (EVENT_SYSTEM_MINIMIZESTART, EVENT_SYSTEM_MINIMIZEEND, 0),
                  (EVENT_OBJECT_DESTROY, EVENT_OBJECT_REORDER, 0),
                  (EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED, 0)}
        for hwnd in windows:
            tid = u32.GetWindowThreadProcessId(hwnd, None)
            if tid:
                wanted.add((EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_LOCATIONCHANGE, tid))
            elif hwnd in new_windows:
                self._queue_sync()  # window disappeared between discovery and subscription
        previous = set(self._hooks)
        for key in wanted - previous:
            self._install_hook(key)
        if any(tid for _, _, tid in wanted - self._hooks.keys()):
            # Windows can refuse a thread-specific hook on system windows.
            # Subscribe to the same events globally and filter HWNDs in the
            # callback until the watched set changes. Never start a timer.
            fallback = (EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_LOCATIONCHANGE, 0)
            if fallback not in self._hooks:
                self._install_hook(fallback)
            if fallback in self._hooks:
                wanted = {key for key in wanted if key[2] == 0} | {fallback}
        for key in self._hooks.keys() - wanted:
            u32.UnhookWinEvent(self._hooks.pop(key))
        if self._hooks.keys() - previous:
            self._queue_sync()  # close the sample/subscribe race
        missing = wanted - self._hooks.keys()
        if missing - self._missing_hooks:
            log.warning("taskbar widget: WinEvent hook unavailable; retry on next update")
        self._missing_hooks = missing

    def _uncovered_screen(self) -> wintypes.RECT | None:
        windows = {hwnd for hwnd in (self._taskbar, self._tray, u32.GetForegroundWindow()) if hwnd}
        screen = None
        # Shell_TrayWnd is the primary bar. Its auto-hide rectangle may slide
        # onto a vertically adjacent display; (0, 0) stays on the primary.
        if self._visible and self._taskbar and u32.IsWindowVisible(self._taskbar):
            monitor = u32.MonitorFromPoint(wintypes.POINT(0, 0), 1)
            info = MONITORINFO(ctypes.sizeof(MONITORINFO))
            if u32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                screen = info.rcMonitor

        interrupted = False
        if screen is not None:
            # Walk upwards from the bar: nothing below it can cover it. One
            # blocker suffices; its next change will rediscover any others.
            hwnd = self._taskbar
            seen = {hwnd}
            while True:
                above = u32.GetWindow(hwnd, GW_HWNDPREV)
                if not above:
                    interrupted = not u32.IsWindow(hwnd)  # stale link vs. top of stack
                    break
                hwnd = above
                if hwnd in seen:  # windows were restacked during the walk
                    interrupted = True
                    break
                seen.add(hwnd)
                if hwnd == self.hwnd or not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
                    continue
                windows.add(hwnd)  # also observe small windows that may grow in place
                if _covers_monitor(hwnd, screen):
                    screen = None
                    break
        if interrupted:
            screen = None
            if not self._walk_interrupted:
                self._queue_sync()  # one resample; further retries need an external event
        self._walk_interrupted = interrupted
        self._watch_windows(windows)
        return screen

    def _target_pos(self, w: int, h: int, screen: wintypes.RECT) -> tuple[int, int] | None:
        r = wintypes.RECT()
        if not u32.GetWindowRect(self._taskbar, ctypes.byref(r)):
            return None
        tw, th = r.right - r.left, r.bottom - r.top
        if th >= tw:  # vertical taskbar (via mods) — not supported, park off
            return None
        m = round(self.MARGIN * self.scale)
        side = self.side
        # A left-aligned taskbar puts Start at the far left; don't cover it.
        if side == "left" and self._left_aligned:
            side = "right"
        if side == "left":
            x = r.left + m
        else:
            tr = wintypes.RECT()
            if self._tray and u32.GetWindowRect(self._tray, ctypes.byref(tr)):
                x = tr.left - w - m
            else:
                x = r.right - w - m
        y = r.top + (th - h) // 2
        if y < screen.top or y + h > screen.bottom:
            return None
        return x, y

    def _apply(self) -> None:
        tb = u32.FindWindowW("Shell_TrayWnd", None) or 0
        if tb != self._taskbar or tb != (u32.GetWindow(self.hwnd, GW_OWNER) or 0):
            u32.DestroyWindow(self.hwnd)  # _run recreates hidden with the new owner
            return
        self._tray = u32.FindWindowExW(tb, None, "TrayNotifyWnd", None) if tb else 0
        screen = self._uncovered_screen()
        with self._lock:
            img, version = self._image, self._image_version
        pos = self._target_pos(*img.size, screen) if screen is not None and img is not None else None
        visible = u32.IsWindowVisible(self.hwnd)
        if pos is None:
            if visible:
                u32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                                 PLACEMENT_FLAGS | SWP_NOMOVE | SWP_HIDEWINDOW)
            return
        flags = PLACEMENT_FLAGS
        if version != self._uploaded_version:
            if not self._upload(img, pos):
                return
            self._uploaded_version = version
            flags |= SWP_NOMOVE  # UpdateLayeredWindow already positioned it
        else:
            r = wintypes.RECT()
            if u32.GetWindowRect(self.hwnd, ctypes.byref(r)) and pos == (r.left, r.top):
                flags |= SWP_NOMOVE
        if not visible:
            flags |= SWP_SHOWWINDOW
        if not visible or not (flags & SWP_NOMOVE):
            u32.SetWindowPos(self.hwnd, None, *pos, 0, 0, flags)

    def _upload(self, img: Image.Image, pos: tuple[int, int]) -> bool:
        w, h = img.size
        bits = _premultiplied_bgra(img)
        screen = u32.GetDC(None)
        mem = g32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0,
                               0, 0, 0, 0, 0)
        ptr = ctypes.c_void_p()
        dib = g32.CreateDIBSection(screen, ctypes.byref(bmi), 0,
                                   ctypes.byref(ptr), None, 0)
        try:
            if not dib or not ptr.value:
                return False
            ctypes.memmove(ptr.value, bits, len(bits))
            old = g32.SelectObject(mem, dib)
            blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
            ok = u32.UpdateLayeredWindow(
                self.hwnd, screen, ctypes.byref(wintypes.POINT(*pos)),
                ctypes.byref(wintypes.SIZE(w, h)), mem,
                ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)
            g32.SelectObject(mem, old)
            if not ok:
                log.warning("UpdateLayeredWindow failed (%d)", ctypes.get_last_error())
            return bool(ok)
        finally:
            if dib:
                g32.DeleteObject(dib)
            g32.DeleteDC(mem)
            u32.ReleaseDC(None, screen)

    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_APP_UPDATE:
                self._sync_pending = False
                self._apply()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_MOUSEMOVE:
                if not self._hover:
                    self._hover = True
                    tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT),
                                          TME_LEAVE, hwnd, 0)
                    u32.TrackMouseEvent(ctypes.byref(tme))
                    self.events.put(("hover", True))
                return 0
            if msg == WM_MOUSELEAVE:
                self._hover = False
                self.events.put(("hover", False))
                return 0
            if msg == WM_LBUTTONUP:
                self.events.put(("click",))
                return 0
            if msg == WM_RBUTTONUP:
                self.events.put(("menu",))
                return 0
            if msg in (WM_SETTINGCHANGE, WM_DPICHANGED, WM_DISPLAYCHANGE,
                       self._msg_taskbar_created):
                self._left_aligned = taskbar_left_aligned()
                self._refresh_scale()
                self.events.put(("rerender",))  # theme may have flipped
                self._post(WM_APP_UPDATE)
            if msg == WM_CLOSE:
                u32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self.hwnd = 0
                if self._hover:
                    self._hover = False
                    self.events.put(("hover", False))
                u32.PostQuitMessage(0)
                return 0
        except Exception:  # never let an exception unwind into user32
            log.exception("taskbar widget wndproc failed (msg=%#x)", msg)
        return u32.DefWindowProcW(hwnd, msg, wp, lp)
